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
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from turbine.logger import TurbineLogger
from turbine.manager import Ticket, WorkerResult
from turbine.scoped_edit import (
    LARGE_FILE_THRESHOLD,
    ScopedEditApplicator,
    build_file_outline,
    check_definition_integrity,
    parse_scoped_edits,
)
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

Your response MUST contain the complete content for every file you modify or create,
using this exact format for each file — and nothing else (no prose outside the blocks):

<file path="relative/path/to/file.py">
<complete file content here>
</file>

Rules:
  - Only touch files listed in your ticket (existing files to modify, new files to create).
  - For new files (marked "new file — currently empty"), provide their full initial content.
  - Omit files you are not changing or creating.
  - Write the COMPLETE file content — not a diff, not a partial snippet.
  - The result must be syntactically valid and contain no duplicate definitions,
    unreachable code, or stray statements.
  - Follow the pseudocode plan step-by-step — do not skip steps or collapse them.
  - If you have nothing to change or create, output nothing (an empty response).
  - If you are genuinely uncertain about your changes — e.g. you lack sufficient
    context, the request is ambiguous, or you cannot verify correctness — add the
    literal tag <uncertain/> anywhere in your response (outside a file block).
    The Manager will flag your result for manual review.
"""


def _build_worker_system(diagnosis: str, has_large_files: bool = False) -> str:
    """Inject the Manager's full diagnosis (and large-file instructions) into
    the worker system prompt."""
    prompt = _WORKER_SYSTEM_BASE
    if has_large_files:
        prompt += _SCOPED_EDIT_INSTRUCTIONS
    if diagnosis:
        prompt += f"\n## Manager Diagnosis (full context)\n{diagnosis}\n"
    return prompt

WORKER_USER_TEMPLATE = """\
## Task
{description}

## Step-by-step implementation plan (follow exactly, in order)
{plan}

## Files
{file_contents}
{context_section}"""

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

# Phase 12: targeted repair prompt — used on repair round 1 (constraint-only).
SCOPED_EDIT_FAILURE_TEMPLATE = """\
Your previous scoped edit(s) could not be applied to the following file(s):

{failures}

Common fixes:
  - "search" text not found: copy the exact lines from the file content below,
    including all indentation.  Include 2–4 lines of unchanged context above
    and below the changed section to make the match unique.
  - "function not found": verify the bare name matches exactly (case-sensitive,
    no signature, decorators, or punctuation — just the plain identifier).
  - JSON parse error: ensure the block is valid JSON with no trailing commas or
    unquoted keys.

Please resubmit your scoped edits.  The current file content(s) are shown below.

{current_files}
"""

# Phase 12: targeted repair prompt — used on repair round 1 (constraint-only).
# The worker is asked only to fix the specific test failures, not to re-generate
# the entire file from scratch.  This is cheaper and less likely to regress
# unrelated code.
REPAIR_CONSTRAINT_TEMPLATE = """\
The following test failures occurred after your last changes were committed.
Your task is to produce the MINIMAL fix to make these tests pass.

## Test failures
```
{failure_output}
```

Return ONLY the file(s) that need changing, using the same <file path="..."> format.
Do NOT rewrite files that are not implicated in the failures above.
The current file contents are shown below for reference.

## Current file contents
{current_files}
"""

# ---------------------------------------------------------------------------
# Phase 22.2: Pre-write reflection prompt
# ---------------------------------------------------------------------------

REFLECTION_SYSTEM = """\
You are performing a brief sanity check on a worker's proposed implementation \
before it is written to disk.  You will be shown the worker's task, plan, and \
a summary of the files it intends to change.

Decide whether the approach is sound, or whether there is a single blocking \
assumption that would make the changes incorrect or incomplete.

A "blocking assumption" is one where:
  - The worker assumes a function, type, or constant exists in a dependency but it may not.
  - The plan requires runtime information the worker could not have had access to.
  - The worker is about to overwrite something in a way that is clearly contradicted
    by the plan or the task description.

Return ONLY a JSON object:
{
  "proceed": true,
  "blocking_assumption": ""
}

Rules:
  - Set "proceed" to true in the VAST MAJORITY of cases.  Only block when you are
    confident there is a concrete, specific problem — not vague uncertainty.
  - "blocking_assumption" must be a single, specific sentence.  Non-empty only when
    proceed == false.
  - Do NOT block for stylistic disagreements, minor unknowns, or reasonable judgment calls.
  - Do NOT block just because the plan is complex or touches many files.
Return nothing but the JSON object — no prose, no markdown fences."""


# ---------------------------------------------------------------------------
# Scoped-edit mode (Phase 9) — used for large files
# ---------------------------------------------------------------------------

_SCOPED_EDIT_INSTRUCTIONS = """\

## Large-file mode — scoped edits required for some files

For every file marked [LARGE FILE — use scoped edits] below, do NOT return
the complete file content.  Instead return a JSON array of targeted edits
inside a <scoped_edits path="relative/path/to/file.py"> … </scoped_edits>
block.  Each edit is a JSON object with ONE of these three forms:

  **PREFERRED — Search/replace (use this for most changes):**
    { "search": "<exact lines to find>", "replacement": "<new text>" }

  Function/class scope (use when replacing an entire definition):
    { "function": "myMethodName", "replacement": "<complete new definition>" }

  Line range (use when you are certain of the exact line numbers):
    { "lines": [<start_1based>, <end_1based>], "replacement": "<new text>" }

IMPORTANT — "search" value rules (PREFERRED form):
  - "search" must be the EXACT text as it appears in the file, including all
    indentation and surrounding lines.  Include 2–4 lines of unchanged context
    above and below the part you are changing to make the match unique.
  - The search text must appear exactly ONCE in the file.  If a snippet repeats,
    add more context lines until it is unique.
  - "replacement" is the complete new text that will replace the matched region.
    Include the unchanged context lines you added to "search" verbatim.
  - This form never requires counting line numbers — always prefer it.

IMPORTANT — "function" value rules:
  - "function" must be the BARE identifier name only (e.g. "toString", "add",
    "Vector2"). Do NOT include docstrings, signatures, braces, or any other
    text — just the plain name string.
  - "replacement" is the COMPLETE new text for the entire definition, including
    its signature/header line, body, and closing brace (for JS) or all indented
    lines (for Python). Include everything from the first line of the definition
    to the last.
  - To add a NEW function that does not yet exist, use the "search" form to
    insert after a unique anchor line, or the "lines" form with the target line.
  - To delete a definition, use an empty "replacement": "".
  - Edits must not overlap each other.
  - Keep all existing functions/classes you are not changing — omit them from
    the edit list (they are preserved automatically).

For files NOT marked [LARGE FILE], continue using the normal
<file path="..."> complete-content format.
"""

_INTEGRITY_REJECTION_TEMPLATE = """\
Your previous proposal was rejected because it silently dropped existing
definitions that were not included as removals in the diff.

The following names exist in the original file but are absent from your
proposed version — you must either keep them intact or explicitly delete
them with an empty replacement:

{lost_names}

Please resubmit your complete proposal, preserving every definition that
should not be deleted.

{current_files}
"""


# ---------------------------------------------------------------------------
# Response parser: extract per-file content blocks
# ---------------------------------------------------------------------------

_FILE_BLOCK = re.compile(
    r"""<file\s+path=["']([^"']+)["']\s*>(.*?)</file>""",
    re.DOTALL,
)


def _normalize_path(raw: str) -> str:
    """Normalise a file path returned by the LLM.

    Strips whitespace, collapses ``./`` prefixes, and converts backslashes
    to forward slashes so the path matches VFS keys regardless of how the
    LLM chose to format it.
    """
    p = raw.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _strip_code_fence(content: str) -> str:
    """Remove a wrapping markdown code fence if the LLM added one."""
    content = content.strip("\n")
    if content.startswith("```"):
        lines = content.splitlines()
        # Drop the opening fence line (e.g. "```python" or "```")
        start = 1
        # Drop the closing fence if present
        end = len(lines)
        if lines[-1].strip() == "```":
            end -= 1
        content = "\n".join(lines[start:end])
    return content


def _extract_file_blocks(text: str) -> dict[str, str]:
    """Return {relative_path: new_content} from an LLM response."""
    return {
        _normalize_path(m.group(1)): _strip_code_fence(m.group(2))
        for m in _FILE_BLOCK.finditer(text)
    }


# Scoped-edit block: <scoped_edits path="..."> ... </scoped_edits>
_SCOPED_EDIT_BLOCK = re.compile(
    r"""<scoped_edits\s+path=["']([^"']+)["']\s*>(.*?)</scoped_edits>""",
    re.DOTALL,
)


def _extract_scoped_edit_blocks(text: str) -> dict[str, str]:
    """Return {relative_path: raw_json_text} from an LLM response."""
    return {
        _normalize_path(m.group(1)): _strip_code_fence(m.group(2).strip())
        for m in _SCOPED_EDIT_BLOCK.finditer(text)
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
    json_ui: object | None = None   # turbine.json_ui.JsonEventUI — forwarded to TurbineLogger
    cost_tracker: object | None = None  # turbine.cost_tracker.CostTracker — Phase 15

    def __post_init__(self) -> None:
        self.log = TurbineLogger(ui=self.json_ui)
        self._conflict_detector = ConflictDetector(self.vfs)
        # Phase 9: identify which of this worker's files exceed the threshold
        self._large_files: set[str] = {
            rel
            for rel in self.ticket.relevant_files
            if self._is_large_file(rel)
        }
        # Phase 10: set of paths that are read-only context (must not be written)
        self._context_file_set: frozenset[str] = frozenset(self.ticket.context_files)

    # ------------------------------------------------------------------
    # Large-file helpers (Phase 9)
    # ------------------------------------------------------------------

    def _is_large_file(self, rel: str) -> bool:
        """Return True if the VFS snapshot for *rel* is at or above the threshold."""
        snap = self.vfs.get_snapshot(rel)
        return snap is not None and len(snap) >= LARGE_FILE_THRESHOLD

    # ------------------------------------------------------------------
    # Phase 22.2: Pre-write reflection
    # ------------------------------------------------------------------

    async def _run_reflection(self, file_blocks: dict[str, str]) -> tuple[bool, str]:
        """Lightweight sanity-check called before the first VFS write.

        Asks the LLM whether the proposed file changes make sense given the
        ticket plan.  Returns ``(proceed, blocking_assumption)``.  On any
        API or parse error it returns ``(True, "")`` so the pipeline is
        never blocked by a reflection failure.
        """
        change_summary = "\n".join(
            f"  - {path} ({len(content.splitlines())} lines)"
            for path, content in file_blocks.items()
        )
        user_content = (
            f"Task description: {self.ticket.description}\n\n"
            f"Step-by-step plan:\n{self.ticket.context or '(none)'}\n\n"
            f"Proposed changes (files to be written):\n{change_summary}"
        )

        try:
            response = await self.client.chat.complete_async(
                model=self.model,
                messages=[
                    {"role": "system", "content": REFLECTION_SYSTEM},
                    {"role": "user", "content": user_content},
                ],
            )
            raw: str = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            if usage is not None and self.cost_tracker is not None:
                self.cost_tracker.record(
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                )
        except Exception as exc:
            self.log.error(
                f"Worker [{self.ticket.id}] — reflection API error: {exc}; skipping."
            )
            return True, ""

        # Strip markdown fences the LLM may have added
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(lines[1:])
            if raw.rstrip().endswith("```"):
                raw = raw.rstrip()[:-3]

        try:
            result = json.loads(raw.strip())
            proceed = bool(result.get("proceed", True))
            blocking = str(result.get("blocking_assumption", "")).strip()
            return proceed, blocking
        except (json.JSONDecodeError, ValueError) as exc:
            self.log.error(
                f"Worker [{self.ticket.id}] — reflection parse error: {exc}; skipping."
            )
            return True, ""

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        repair_feedback: str = "",
        constraint_only: bool = False,
    ) -> WorkerResult:
        """Run the handshake loop and return a WorkerResult.

        Parameters
        ----------
        repair_feedback:
            When non-empty, this is test-failure output from a previous verify
            cycle.  It is prepended to the initial user message so the LLM
            knows what broke and can fix it before re-proposing.
        constraint_only:
            Phase 12 — graduated repair strategy.  When ``True``, use a
            targeted prompt that asks for the MINIMAL fix to failing tests
            rather than regenerating the full file from scratch.  Used on
            repair round 1; round 2 falls back to the full re-run.
        """
        self.log.thinking(f"Worker [{self.ticket.id}] — starting: {self.ticket.description}")
        if self.ui:
            self.ui.on_worker_start(self.ticket.id, self.ticket.description)

        file_contents = self._build_file_contents()
        context_section = self._build_context_file_contents()

        # Phase 12: on constraint-only repair rounds, use a targeted prompt
        # instead of the full task template.
        if repair_feedback and constraint_only:
            initial_user = REPAIR_CONSTRAINT_TEMPLATE.format(
                failure_output=repair_feedback,
                current_files=file_contents,
            )
        else:
            initial_user = WORKER_USER_TEMPLATE.format(
                description=self.ticket.description,
                plan=self.ticket.context or "(no plan provided — use your best judgement)",
                file_contents=file_contents,
                context_section=context_section,
            )

        if repair_feedback and not constraint_only:
            initial_user = (
                "## Test failures from previous attempt (you must fix these)\n"
                f"```\n{repair_feedback}\n```\n\n"
            ) + initial_user

        # Conversation history: [system, user, assistant, user, ...]
        messages: list[dict] = [
            {
                "role": "system",
                "content": _build_worker_system(
                    self.diagnosis, has_large_files=bool(self._large_files)
                ),
            },
            {"role": "user", "content": initial_user},
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

            # -------------------------------------------------------
            # Phase 12: detect <uncertain/> confidence signal
            # -------------------------------------------------------
            is_uncertain = bool(re.search(r"<uncertain\s*/>", assistant_text))
            if is_uncertain:
                self.log.thinking(
                    f"Worker [{self.ticket.id}] — flagged output as uncertain."
                )

            # -------------------------------------------------------
            # Phase 9: parse scoped-edit blocks for large files, then
            # fall back to complete-content blocks for the rest.
            # -------------------------------------------------------
            scoped_blocks = _extract_scoped_edit_blocks(assistant_text)
            file_blocks = _extract_file_blocks(assistant_text)

            # Resolve scoped edits → complete file content (with fallback)
            scoped_failures: dict[str, str] = {}  # rel → error description
            for rel, raw_json in scoped_blocks.items():
                resolved, error = self._apply_scoped_edits(rel, raw_json, conflict_retry)
                if resolved is not None:
                    # Scoped edit applied — treat like a complete file block
                    file_blocks[rel] = resolved
                else:
                    self.log.action(
                        f"Worker [{self.ticket.id}] — scoped edit for '{rel}' "
                        "failed; falling back to complete-content mode."
                    )
                    scoped_failures[rel] = error

            # If any scoped edits failed and the LLM supplied no full-file
            # fallback, send targeted feedback and retry rather than silently
            # dropping the file.
            failed_without_fallback = {
                rel: err for rel, err in scoped_failures.items()
                if rel not in file_blocks
            }
            if failed_without_fallback:
                failure_text = "\n".join(
                    f"  {rel}: {err}" for rel, err in failed_without_fallback.items()
                )
                self.log.thinking(
                    f"Worker [{self.ticket.id}] — scoped edit failure(s), "
                    "requesting corrected edits."
                )
                current_files = self._build_file_contents()
                messages.append({
                    "role": "user",
                    "content": SCOPED_EDIT_FAILURE_TEMPLATE.format(
                        failures=failure_text,
                        current_files=current_files,
                    ),
                })
                continue  # next handshake attempt

            if not file_blocks:
                self.log.action(f"Worker [{self.ticket.id}] — no file blocks produced (no changes).")
                if self.ui:
                    self.ui.on_worker_done(self.ticket.id, success=True, detail="no changes")
                return WorkerResult(
                    ticket_id=self.ticket.id, success=True, proposed_diff="",
                    uncertain=is_uncertain,
                )

            # -------------------------------------------------------
            # Phase 22.2: pre-write reflection (first attempt only,
            # skipped on repair rounds and when ticket opts out).
            # -------------------------------------------------------
            if attempt == 1 and not repair_feedback and not self.ticket.reflection_skipped:
                self.log.thinking(
                    f"Worker [{self.ticket.id}] — running pre-write reflection…"
                )
                proceed, blocking = await self._run_reflection(file_blocks)
                if not proceed:
                    self.log.thinking(
                        f"Worker [{self.ticket.id}] — reflection blocked: {blocking}"
                    )
                    if self.ui:
                        self.ui.on_worker_blocked(self.ticket.id, assumption=blocking)
                    if self.json_ui is not None:
                        self.json_ui.on_worker_blocked(self.ticket.id, assumption=blocking)
                    return WorkerResult(
                        ticket_id=self.ticket.id,
                        success=False,
                        error=f"reflection blocked: {blocking}",
                    )
                self.log.thinking(
                    f"Worker [{self.ticket.id}] — reflection passed; proceeding to write."
                )

            if self.verbose:
                for path, content in file_blocks.items():
                    self.log.verbose(
                        f"Worker [{self.ticket.id}] parsed block '{path}' "
                        f"({len(content.splitlines())} lines)"
                    )

            # -------------------------------------------------------
            # Phase 9: definition integrity check (anti-hallucination)
            # -------------------------------------------------------
            integrity_ok, integrity_msg = self._check_integrity(file_blocks, conflict_retry)
            if not integrity_ok:
                self.log.thinking(
                    f"Worker [{self.ticket.id}] — integrity check failed; "
                    "sending rejection with lost definitions."
                )
                if self.ui:
                    self.ui.on_worker_conflict(
                        self.ticket.id, detail="integrity:definitions lost"
                    )
                current_files = self._build_file_contents()
                messages.append({
                    "role": "user",
                    "content": _INTEGRITY_REJECTION_TEMPLATE.format(
                        lost_names=integrity_msg,
                        current_files=current_files,
                    ),
                })
                continue  # next handshake attempt

            # Convert complete file contents → unified diff.
            # On conflict retries we diff against the current snapshot (which
            # already contains the winning worker's staged changes) so our hunks
            # land cleanly on top rather than re-conflicting with the baseline.
            diff = self._build_diff_from_blocks(file_blocks, use_snapshot=conflict_retry)

            # Handshake: check for conflicts (acquires per-file locks)
            approved, constraint_msg = await self._check_and_stage(diff)
            if approved:
                detail = "uncertain" if is_uncertain else None
                self.log.action(
                    f"Worker [{self.ticket.id}] — diff approved and staged"
                    + (" (uncertain)" if is_uncertain else "") + "."
                )
                if self.ui:
                    # Phase 18: pass the list of modified files so the extension
                    # can offer per-worker diff viewing and file navigation.
                    self.ui.on_worker_done(
                        self.ticket.id, success=True,
                        detail=detail,
                        files=list(file_blocks.keys()),
                    )
                return WorkerResult(
                    ticket_id=self.ticket.id,
                    success=True,
                    proposed_diff=diff,
                    uncertain=is_uncertain,
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
                # Try the normalised path as a fallback — the LLM may
                # have returned "./foo.js" while the VFS key is "foo.js".
                norm = _normalize_path(rel)
                if norm != rel:
                    ref = (self.vfs.get_snapshot(norm) if use_snapshot
                           else self.vfs.get_baseline(norm))
                    if ref is not None:
                        rel = norm  # use the normalised key downstream
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

        # Phase 10: reject before touching the VFS if the diff targets any
        # context-only file.  Parse the target paths from the diff headers
        # without a full apply so we can bail cheaply.
        if self._context_file_set:
            from turbine.vfs import _parse_unified_diff as _parse_diff
            illegal = {
                h.file_path
                for h in _parse_diff("_check", diff)
                if h.file_path in self._context_file_set
            }
            if illegal:
                return False, (
                    "The following files are read-only context and must not be modified: "
                    + ", ".join(sorted(illegal))
                )

        # Acquire all relevant file locks in sorted order to prevent deadlock.
        # Include new_files so two workers can't race on the same new path.
        all_ticket_files = self.ticket.relevant_files + self.ticket.new_files
        lock_keys = sorted(f for f in all_ticket_files if f in self.file_locks)
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
    # Phase 9 helpers: scoped edits & integrity
    # ------------------------------------------------------------------

    def _apply_scoped_edits(
        self, rel: str, raw_json: str, use_snapshot: bool
    ) -> tuple[str | None, str]:
        """Parse *raw_json* as a scoped-edit list, apply to the VFS snapshot,
        and return ``(complete_file_content, "")`` on success or
        ``(None, error_message)`` on any parse or application error.
        """
        import json as _json

        try:
            data = _json.loads(raw_json)
        except _json.JSONDecodeError as exc:
            msg = f"JSON parse error: {exc}"
            self.log.error(
                f"Worker [{self.ticket.id}] — scoped edit {msg} for '{rel}'"
            )
            return None, msg

        try:
            edits = parse_scoped_edits(data)
        except ValueError as exc:
            msg = f"schema error: {exc}"
            self.log.error(
                f"Worker [{self.ticket.id}] — scoped edit {msg} for '{rel}'"
            )
            return None, msg

        ref = (
            self.vfs.get_snapshot(rel) if use_snapshot else self.vfs.get_baseline(rel)
        )
        if ref is None:
            norm = _normalize_path(rel)
            ref = (
                self.vfs.get_snapshot(norm) if use_snapshot else self.vfs.get_baseline(norm)
            )
        if ref is None:
            msg = f"no VFS reference found for '{rel}'"
            self.log.error(
                f"Worker [{self.ticket.id}] — scoped edit: {msg}"
            )
            return None, msg

        result = ScopedEditApplicator(ref).apply(edits)
        if not result.success:
            self.log.error(
                f"Worker [{self.ticket.id}] — scoped edit application "
                f"failed for '{rel}': {result.error}"
            )
            return None, result.error

        return "\n".join(result.lines), ""

    def _check_integrity(
        self, file_blocks: dict[str, str], use_snapshot: bool
    ) -> tuple[bool, str]:
        """Run the definition integrity check against every file block.

        Returns ``(True, "")`` when all files pass.
        Returns ``(False, message)`` listing the lost definitions when any fail.
        """
        lost_lines: list[str] = []
        for rel, new_content in file_blocks.items():
            ref = (
                self.vfs.get_snapshot(rel) if use_snapshot
                else self.vfs.get_baseline(rel)
            )
            if ref is None:
                norm = _normalize_path(rel)
                ref = (
                    self.vfs.get_snapshot(norm) if use_snapshot
                    else self.vfs.get_baseline(norm)
                )
            if ref is None:
                continue  # new file — nothing to compare against
            if not ref:
                continue  # empty original — no definitions to lose

            lost = check_definition_integrity(ref, new_content.splitlines())
            if lost:
                lost_lines.append(
                    f"  {rel}: missing {', '.join(lost)}"
                )

        if lost_lines:
            return False, "\n".join(lost_lines)
        return True, ""

    # ------------------------------------------------------------------
    # Context helpers
    # ------------------------------------------------------------------

    def _build_file_contents(self) -> str:
        """Return a formatted string of all files this worker may touch or create.

        Phase 9: files at or above LARGE_FILE_THRESHOLD are labelled so the
        worker knows to use the scoped-edit format instead of returning the
        complete file.
        """
        parts: list[str] = []
        for rel in self.ticket.relevant_files:
            snapshot = self.vfs.get_snapshot(rel)
            if snapshot is None:
                self.log.error(f"Worker [{self.ticket.id}] — no VFS snapshot for '{rel}', skipping.")
                continue
            content = "\n".join(snapshot)
            if rel in self._large_files:
                # Show with line numbers so the LLM can reference them
                # precisely when producing scoped edits.  Prepend a structural
                # outline so the LLM can navigate without manually counting lines.
                n = len(snapshot)
                width = len(str(n))
                numbered = "\n".join(
                    f"{i + 1:>{width}} | {line}" for i, line in enumerate(snapshot)
                )
                outline = build_file_outline(snapshot)
                outline_section = f"\n{outline}\n\n" if outline else "\n"
                label = f"### {rel} [LARGE FILE — use scoped edits] ({n} lines)"
                parts.append(f"{label}{outline_section}```\n{numbered}\n```")
            else:
                label = f"### {rel}"
                parts.append(f"{label}\n```\n{content}\n```")
        # Phase 8: show new-file stubs so the worker knows what to populate
        for rel in self.ticket.new_files:
            parts.append(f"### {rel} *(new file — currently empty)*\n```\n```")
        return "\n\n".join(parts) if parts else "(no files provided)"

    def _build_context_file_contents(self) -> str:
        """Return a clearly labelled read-only context section, or empty string.

        Phase 10: context_files are presented under a distinct heading so the
        worker understands they provide reference information only and must not
        be modified.
        """
        if not self.ticket.context_files:
            return ""
        parts: list[str] = []
        for rel in self.ticket.context_files:
            snapshot = self.vfs.get_snapshot(rel)
            if snapshot is None:
                self.log.error(
                    f"Worker [{self.ticket.id}] — no VFS snapshot for context file '{rel}', skipping."
                )
                continue
            content = "\n".join(snapshot)
            parts.append(f"### {rel} *(read-only — do NOT modify)*\n```\n{content}\n```")
        if not parts:
            return ""
        return "\n## Read-only context (reference only — do not modify these files)\n" + "\n\n".join(parts) + "\n"

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
    # API call with exponential back-off retry — Phase 16: streaming
    # ------------------------------------------------------------------

    async def _call_with_retry(self, messages: list[dict]) -> str:
        """Call Mistral via the streaming API with retries on transient errors.

        Phase 16 changes
        ----------------
        * Uses ``stream_async`` instead of ``complete_async`` so tokens are
          yielded as they arrive.
        * Pipes each chunk to ``on_worker_token`` on the UI so the dashboard
          shows a live character count rather than a spinner.
        * Buffers the full text and records token usage from the final ``usage``
          object attached to the last stream event — no change to downstream
          parsing logic.
        """
        delay = RETRY_BASE_DELAY
        last_exc: Exception | None = None

        for attempt in range(1, self.max_api_retries + 1):
            try:
                buffer = await self._stream_response(messages)
                return buffer
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

    async def _stream_response(self, messages: list[dict]) -> str:
        """Open a streaming chat completion and return the fully buffered text.

        Emits ``on_worker_token`` UI events as each chunk arrives so the
        dashboard can show a live character count.  Records token usage from
        the stream's final ``usage`` datum for Phase 15 cost tracking.

        Falls back gracefully to a non-streaming call if the client does not
        expose ``chat.stream_async`` (e.g. in tests that mock the client).
        """
        # Prefer streaming if the client supports it
        stream_fn = getattr(getattr(self.client, "chat", None), "stream_async", None)
        if stream_fn is None:
            # Fallback: non-streaming (covers mocked clients in tests)
            response = await self.client.chat.complete_async(
                model=self.model,
                messages=messages,
            )
            content = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            if usage is not None and self.cost_tracker is not None:
                self.cost_tracker.record(
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                )
            return content

        # Streaming path
        parts: list[str] = []
        char_count = 0
        usage = None

        # stream_async is a coroutine function — await the call to get the
        # EventStreamAsync context manager, then iterate over it.
        async with await stream_fn(model=self.model, messages=messages) as stream:
            async for chunk in stream:
                # Extract delta text from chunk.
                # SDK shape: CompletionEvent.data → CompletionChunk
                #            .choices[0].delta.content (str | list | UNSET)
                delta = ""
                try:
                    raw = chunk.data.choices[0].delta.content
                    if isinstance(raw, str):
                        delta = raw
                except (AttributeError, IndexError):
                    try:
                        raw = chunk.choices[0].delta.content
                        if isinstance(raw, str):
                            delta = raw
                    except (AttributeError, IndexError):
                        pass

                if delta:
                    parts.append(delta)
                    char_count += len(delta)
                    # Notify the UI of the updated character count
                    if self.ui is not None and hasattr(self.ui, "on_worker_token"):
                        self.ui.on_worker_token(self.ticket.id, char_count)

                # Capture usage data from the last chunk (present on finish events)
                try:
                    chunk_usage = chunk.data.usage
                except AttributeError:
                    chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = chunk_usage

        content = "".join(parts)

        # Phase 15: record token usage from the stream's usage datum
        if usage is not None and self.cost_tracker is not None:
            self.cost_tracker.record(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            )

        return content

