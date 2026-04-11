"""Worker — Phase 4 of the Turbine lifecycle.

Each Worker receives a single Ticket, reads its assigned files from the VFS,
and enters a conversational loop with Mistral to produce complete replacement
file contents.  The Worker then computes a unified diff internally (using
``difflib``) against the VFS baseline, so the LLM never has to format a diff.

The "Ready to Proceed" handshake
---------------------------------
1. Worker asks the LLM to return the complete new content of each modified file.
2. Worker computes a unified diff from baseline → new content via difflib.
3. Manager runs ConflictDetector against the VFS.
4a. No conflicts → Manager stages the diff and signals approval.
4b. Conflicts found → Manager sends constraint feedback; Worker revises.

The loop repeats up to ``max_handshake_attempts`` times before giving up.

Retry policy
------------
Transient Mistral errors (rate-limit, 5xx) are retried with exponential
back-off up to ``max_api_retries`` times.  Context-window overflow is
detected before each call; if the conversation has grown too large the
oldest user/assistant turns are pruned (the system prompt is always kept).
"""

from __future__ import annotations

import asyncio
import difflib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from turbine.logger import TurbineLogger
from turbine.manager import Ticket, WorkerResult
from turbine.vfs import ConflictDetector, VirtualFileSystem

if TYPE_CHECKING:
    from turbine.manager import Manager
    from turbine.ui import TurbineUI


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_HANDSHAKE_ATTEMPTS = 3
MAX_API_RETRIES = 4
RETRY_BASE_DELAY = 1.0   # seconds; doubles each retry

# Mistral HTTP status codes that warrant a retry
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_WORKER_SYSTEM_BASE = """\
You are an expert software engineer working autonomously on a focused sub-task.
You have been given:
  • A task description, including a step-by-step pseudocode plan you MUST follow.
  • The full Manager diagnosis for context (do not act on it directly — your ticket
    is already scoped to your specific sub-task).
  • The complete current content of every file you are allowed to modify.
  • (Optionally) constraint feedback from the Manager if a previous proposal conflicted.

Your response MUST contain the complete new content for every file you modify,
using this exact format for each file — and nothing else (no prose outside the blocks):

<file path="relative/path/to/file.py">
<complete file content here>
</file>

Rules:
  - Only modify the files listed in your ticket. Omit files you are not changing.
  - Write the COMPLETE file content — not a diff, not a partial snippet.
  - The result must be syntactically valid and contain no duplicate definitions,
    unreachable code, or stray statements.
  - Follow the pseudocode plan step-by-step — do not skip steps or collapse them.
  - If you have nothing to change, output nothing (an empty response).
"""


def _build_worker_system(diagnosis: str) -> str:
    """Inject the Manager's full diagnosis into the worker system prompt."""
    if not diagnosis:
        return _WORKER_SYSTEM_BASE
    return (
        _WORKER_SYSTEM_BASE
        + f"\n## Manager Diagnosis (full context)\n{diagnosis}\n"
    )

WORKER_USER_TEMPLATE = """\
## Task
{description}

## Step-by-step implementation plan (follow exactly, in order)
{plan}

## Files
{file_contents}
"""

CONSTRAINT_TEMPLATE = """\
Your previous proposal was rejected because it would conflict with changes already
staged by another worker. The Manager has flagged the following overlapping regions:

{conflicts}

The files below show their CURRENT state after the other worker's changes have been
applied. You MUST base your new proposal on these updated contents — not the original
file you were given at the start.  Write your complete new file(s) using the same
<file path="..."> format, building on top of the current state shown here.

{current_files}
"""


# ---------------------------------------------------------------------------
# Response parser: extract per-file content blocks
# ---------------------------------------------------------------------------

_FILE_BLOCK = re.compile(
    r'<file\s+path="([^"]+)"\s*>(.*?)</file>',
    re.DOTALL,
)


def _extract_file_blocks(text: str) -> dict[str, str]:
    """Return {relative_path: new_content} from an LLM response."""
    return {
        m.group(1).strip(): m.group(2)
        for m in _FILE_BLOCK.finditer(text)
    }


# ---------------------------------------------------------------------------
# Lock helpers
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


@asynccontextmanager
async def _acquire_all(locks: list[asyncio.Lock]):
    """Acquire a list of asyncio.Locks in order, release in reverse."""
    acquired: list[asyncio.Lock] = []
    try:
        for lock in locks:
            await lock.acquire()
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

@dataclass
class Worker:
    """Runs the LLM loop for a single Ticket.

    Parameters
    ----------
    ticket:
        The sub-task assigned to this worker.
    vfs:
        Shared VirtualFileSystem owned by the Manager.
    client:
        The Mistral async client (already initialised).
    model:
        Mistral model name.
    token_manager:
        Used to monitor context size before each API call.
    max_handshake_attempts:
        How many proposal/revision rounds to allow.
    max_api_retries:
        How many times to retry a failed Mistral call.
    """

    ticket: Ticket
    vfs: VirtualFileSystem
    client: object          # mistralai.client.Mistral — typed as object to avoid circular import
    model: str
    token_manager: object   # turbine.token_manager.TokenManager
    diagnosis: str = ""     # full Manager diagnosis injected into system prompt
    file_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    max_handshake_attempts: int = MAX_HANDSHAKE_ATTEMPTS
    max_api_retries: int = MAX_API_RETRIES
    ui: object | None = None   # turbine.ui.TurbineUI — optional, typed as object to avoid circular import
    verbose: bool = False

    def __post_init__(self) -> None:
        self.log = TurbineLogger()
        self._conflict_detector = ConflictDetector(self.vfs)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, repair_feedback: str = "") -> WorkerResult:
        """Run the handshake loop and return a WorkerResult.

        Parameters
        ----------
        repair_feedback:
            When non-empty, this is test-failure output from a previous verify
            cycle.  It is prepended to the initial user message so the LLM
            knows what broke and can fix it before re-proposing.
        """
        self.log.thinking(f"Worker [{self.ticket.id}] — starting: {self.ticket.description}")
        if self.ui:
            self.ui.on_worker_start(self.ticket.id, self.ticket.description)

        file_contents = self._build_file_contents()
        initial_user = WORKER_USER_TEMPLATE.format(
            description=self.ticket.description,
            plan=self.ticket.context or "(no plan provided — use your best judgement)",
            file_contents=file_contents,
        )

        if repair_feedback:
            initial_user = (
                "## Test failures from previous attempt (you must fix these)\n"
                f"```\n{repair_feedback}\n```\n\n"
            ) + initial_user

        # Conversation history: [system, user, assistant, user, ...]
        messages: list[dict] = [
            {"role": "system", "content": _build_worker_system(self.diagnosis)},
            {"role": "user",   "content": initial_user},
        ]

        conflict_retry = False  # True after the first conflict — diff against snapshot

        for attempt in range(1, self.max_handshake_attempts + 1):
            self.log.thinking(
                f"Worker [{self.ticket.id}] — handshake attempt {attempt}/{self.max_handshake_attempts}"
            )
            if self.ui:
                self.ui.on_worker_attempt(self.ticket.id, attempt)

            # Prune messages if approaching context limit
            messages = self._prune_messages(messages)

            # Call the LLM
            try:
                assistant_text = await self._call_with_retry(messages)
            except Exception as exc:
                self.log.error(f"Worker [{self.ticket.id}] — API error after retries: {exc}")
                if self.ui:
                    self.ui.on_worker_done(self.ticket.id, success=False, detail=str(exc)[:60])
                return WorkerResult(ticket_id=self.ticket.id, success=False, error=str(exc))

            messages.append({"role": "assistant", "content": assistant_text})

            if self.verbose:
                # Show a trimmed preview of the raw LLM response
                preview = assistant_text[:400].replace("\n", " ↵ ")
                suffix = "…" if len(assistant_text) > 400 else ""
                self.log.verbose(
                    f"Worker [{self.ticket.id}] raw response ({len(assistant_text)} chars): "
                    f"{preview}{suffix}"
                )

            # Parse the file blocks from the LLM response
            file_blocks = _extract_file_blocks(assistant_text)
            if not file_blocks:
                self.log.action(f"Worker [{self.ticket.id}] — no file blocks produced (no changes).")
                if self.ui:
                    self.ui.on_worker_done(self.ticket.id, success=True, detail="no changes")
                return WorkerResult(ticket_id=self.ticket.id, success=True, proposed_diff="")

            if self.verbose:
                for path, content in file_blocks.items():
                    self.log.verbose(
                        f"Worker [{self.ticket.id}] parsed block '{path}' "
                        f"({len(content.splitlines())} lines)"
                    )

            # Convert complete file contents → unified diff.
            # On conflict retries we diff against the current snapshot (which
            # already contains the winning worker's staged changes) so our hunks
            # land cleanly on top rather than re-conflicting with the baseline.
            diff = self._build_diff_from_blocks(file_blocks, use_snapshot=conflict_retry)

            # Handshake: check for conflicts (acquires per-file locks)
            approved, constraint_msg = await self._check_and_stage(diff)
            if approved:
                self.log.action(f"Worker [{self.ticket.id}] — diff approved and staged.")
                if self.ui:
                    self.ui.on_worker_done(self.ticket.id, success=True)
                return WorkerResult(
                    ticket_id=self.ticket.id,
                    success=True,
                    proposed_diff=diff,
                )

            # Rejected — send constraint feedback with CURRENT file contents so
            # the LLM can write its changes on top of the already-staged state.
            conflict_retry = True
            self.log.thinking(
                f"Worker [{self.ticket.id}] — conflicts detected, sending constraints."
            )
            if self.ui:
                self.ui.on_worker_conflict(self.ticket.id, detail=constraint_msg[:60])
            current_files = self._build_file_contents()   # reads current snapshot
            messages.append({
                "role": "user",
                "content": CONSTRAINT_TEMPLATE.format(
                    conflicts=constraint_msg,
                    current_files=current_files,
                ),
            })

        # Exhausted all attempts
        error = (
            f"Worker [{self.ticket.id}] failed to produce a conflict-free diff "
            f"after {self.max_handshake_attempts} attempt(s)."
        )
        self.log.error(error)
        if self.ui:
            self.ui.on_worker_done(self.ticket.id, success=False, detail="max attempts reached")
        return WorkerResult(ticket_id=self.ticket.id, success=False, error=error)

    # ------------------------------------------------------------------
    # Diff generation from complete file blocks
    # ------------------------------------------------------------------

    def _build_diff_from_blocks(
        self, file_blocks: dict[str, str], use_snapshot: bool = False
    ) -> str:
        """Compute a unified diff from LLM-supplied complete file contents.

        For each file in *file_blocks*, diffs against the VFS baseline (default)
        or the current snapshot (``use_snapshot=True``, used on conflict retries
        so the diff lands cleanly on top of already-staged changes).
        Returns a combined unified diff string covering all modified files.
        """
        parts: list[str] = []
        for rel, new_content in file_blocks.items():
            if use_snapshot:
                ref = self.vfs.get_snapshot(rel)
            else:
                ref = self.vfs.get_baseline(rel)
            if ref is None:
                self.log.error(f"Worker [{self.ticket.id}] — no VFS reference for '{rel}', skipping.")
                continue
            old_lines = [l + "\n" for l in ref]
            new_lines = [l + "\n" for l in new_content.splitlines()]
            # Ensure trailing newline is represented
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            diff_lines = list(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"a/{rel}", tofile=f"b/{rel}",
            ))
            if diff_lines:
                parts.append("".join(diff_lines))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Handshake: conflict detection and VFS staging
    # ------------------------------------------------------------------

    async def _check_and_stage(self, diff: str) -> tuple[bool, str]:
        """Acquire per-file locks, apply diff to VFS, detect conflicts, roll back if needed.

        Acquiring file locks before mutating the VFS means workers that share
        a file are serialised: the second worker will see the first worker's
        committed snapshot rather than racing against a stale baseline.

        Returns
        -------
        (approved, constraint_message)
            approved is True when the diff was clean and has been staged.
            constraint_message is non-empty when conflicts were found.
        """
        if not diff:
            return True, ""

        # Acquire all relevant file locks in sorted order to prevent deadlock.
        lock_keys = sorted(f for f in self.ticket.relevant_files if f in self.file_locks)
        locks = [self.file_locks[k] for k in lock_keys]

        async with _acquire_all(locks):
            try:
                applied_hunks = self.vfs.apply_diff(self.ticket.id, diff)
            except ValueError as exc:
                return False, str(exc)

            conflicts = self._conflict_detector.check()
            if not conflicts:
                return True, ""

            # Roll back: revert both the staged list and the snapshot mutation
            self.vfs.rollback_diff(applied_hunks)

        constraint_msg = "\n".join(str(c) for c in conflicts)
        return False, constraint_msg

    # ------------------------------------------------------------------
    # Context helpers
    # ------------------------------------------------------------------

    def _build_file_contents(self) -> str:
        """Return a formatted string of all files this worker may touch."""
        parts: list[str] = []
        for rel in self.ticket.relevant_files:
            snapshot = self.vfs.get_snapshot(rel)
            if snapshot is None:
                self.log.error(f"Worker [{self.ticket.id}] — no VFS snapshot for '{rel}', skipping.")
                continue
            content = "\n".join(snapshot)
            parts.append(f"### {rel}\n```\n{content}\n```")
        return "\n\n".join(parts) if parts else "(no files provided)"

    def _prune_messages(self, messages: list[dict]) -> list[dict]:
        """Drop the oldest non-system turns if the conversation overflows the budget."""
        if len(messages) <= 2:
            return messages

        full_text = "\n".join(m["content"] for m in messages)
        while not self.token_manager.fits(full_text) and len(messages) > 2:
            # Remove the oldest user/assistant pair after the system message
            messages = [messages[0]] + messages[3:]
            full_text = "\n".join(m["content"] for m in messages)

        return messages

    # ------------------------------------------------------------------
    # API call with exponential back-off retry
    # ------------------------------------------------------------------

    async def _call_with_retry(self, messages: list[dict]) -> str:
        """Call Mistral chat with retries on transient errors."""
        delay = RETRY_BASE_DELAY
        last_exc: Exception | None = None

        for attempt in range(1, self.max_api_retries + 1):
            try:
                response = await self.client.chat.complete_async(
                    model=self.model,
                    messages=messages,
                )
                return response.choices[0].message.content or ""
            except Exception as exc:
                last_exc = exc
                status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
                if status not in _RETRYABLE_STATUS and attempt < self.max_api_retries:
                    # Non-retryable error — fail immediately
                    raise
                if attempt < self.max_api_retries:
                    self.log.error(
                        f"Worker [{self.ticket.id}] — API error (attempt {attempt}), "
                        f"retrying in {delay:.1f}s: {exc}"
                    )
                    await asyncio.sleep(delay)
                    delay *= 2

        raise last_exc  # type: ignore[misc]
