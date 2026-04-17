"""Manager (Dynamo) — Steps 2–5 of the Turbine 6-step lifecycle.

Step 2: Preprocess  — Ask the LLM which files in the tree are relevant.
Step 3: Investigate — Ask the LLM to diagnose the issue and define sub-tasks.
Step 4: Delegate    — Decompose investigation into JSON tickets; spawn async workers.
Step 5: Commit & Verify — Write VFS to disk, run tests, feed failures back to workers.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TYPE_CHECKING

from mistralai.client import Mistral

from turbine.commit_engine import CommitEngine, CommitResult, GitAwareCommitEngine
from turbine.cost_tracker import BudgetExceededError, CostTracker
from turbine.logger import TurbineLogger
from turbine.static_checker import StaticCheckResult, StaticChecker
from turbine.test_runner import RepairTask, TestRunResult, TestRunner
from turbine.token_manager import TokenManager
from turbine.tree_mapper import ProjectTree
from turbine.ui import PipelineStep, TurbineUI
from turbine.vfs import ConflictDetector, VirtualFileSystem

if TYPE_CHECKING:
    from turbine.git_integration import GitIntegration


# ---------------------------------------------------------------------------
# Phase 20: PipelineMode — routing enum
# ---------------------------------------------------------------------------

class PipelineMode(str, Enum):
    WIDE = "wide"   # current parallel-workers path (multiple tickets)
    DEEP = "deep"   # sequential tool-calling agent (single tightly-coupled task)


# ---------------------------------------------------------------------------
# Ticket — a single unit of work for one worker
# ---------------------------------------------------------------------------

@dataclass
class Ticket:
    """A decomposed sub-task produced by Step 3 investigation."""
    id: str                          # e.g. "ticket-1"
    description: str                 # human-readable goal
    relevant_files: list[str]        # relative paths to existing files the worker may modify
    new_files: list[str] = field(default_factory=list)  # relative paths of brand-new files to create
    context: str = ""                # extra notes from the Manager for the worker
    context_files: list[str] = field(default_factory=list)  # files the worker may READ but NOT write

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "relevant_files": self.relevant_files,
            "new_files": self.new_files,
            "context": self.context,
            "context_files": self.context_files,
        }


# ---------------------------------------------------------------------------
# WorkerResult — placeholder returned by the (Phase 4) worker coroutine
# ---------------------------------------------------------------------------

@dataclass
class WorkerResult:
    ticket_id: str
    success: bool
    proposed_diff: str = ""      # unified diff string (empty if no changes)
    error: str = ""
    uncertain: bool = False      # Phase 12: worker flagged its own output as uncertain


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

PREPROCESS_SYSTEM = """\
You are Turbine's Preprocessor. Given a project file tree and a user request, \
return ONLY a JSON array of relative file paths that are relevant to the request. \
You MUST only include paths that appear verbatim in the provided file tree — \
do NOT invent or guess file names that are not listed. \
Include files that may need to be read or modified. \
Return nothing but the JSON array — no prose, no markdown fences."""

INVESTIGATE_SYSTEM = """\
You are Turbine's Investigator. Given a user request and the contents of relevant \
files, produce a structured diagnosis and task decomposition.

Critical decomposition rule:
  - NEVER assign the same file to more than one ticket.  If multiple logical
    concerns all touch the same file, combine them into a single ticket whose
    plan covers all concerns in sequence.  Parallel tickets that share a file
    WILL conflict and WILL fail.

Rules for the "context" field of each ticket:
  - It MUST contain a numbered, step-by-step pseudocode plan the worker will follow
    verbatim. Do not say "refactor X" — say exactly HOW: which data structures, which
    algorithm (e.g. "Kahn's topological sort"), which loop invariants to maintain.
  - Each step should be 1–2 sentences. Aim for 4–8 steps per ticket.
  - Name specific functions / classes / variables that must change and how.

Rules for new file paths in "new_files":
  - Use the EXACT filename the user requested.  If the user says "create foo.js",
    the path MUST be "foo.js" — never "src/utils/foo.js" or any other invented path.
  - Do NOT create or invent subdirectory structure unless the user explicitly specified
    a subdirectory in their request (e.g. "create src/utils/foo.js").
  - If the user gave no path prefix, place the file at the project root (no directory).

Rules for "context_files" (read-only references):
  - Use "context_files" for files the worker needs to read for type definitions, interfaces,
    shared constants, or other context — but must NOT modify.
  - A file must appear in EXACTLY ONE of "relevant_files", "new_files", or "context_files".
    Never list the same file in more than one of those fields.
  - Omit the field (or use []) when the worker needs no read-only reference files.

Return ONLY a JSON object with this exact shape:
{
  "diagnosis": "<thorough description of the root cause or goal — be specific>",
  "mode": "wide",
  "needs_more_files": ["<path of missing file you need>", ...],
  "clarification_hint": "<optional: short plain-English note for the user when you cannot proceed — omit or leave empty string otherwise>",
  "clarification": {
    "question": "<a single, concise question the user must answer before work can begin>",
    "options": ["<option A>", "<option B>", "..."]
  },
  "tickets": [
    {
      "id": "ticket-1",
      "description": "<what this sub-task achieves>",
      "relevant_files": ["<relative path of existing file to modify>", ...],
      "new_files": ["<relative path of brand-new file to CREATE>", ...],
      "context_files": ["<relative path of file to READ but not modify>", ...],
      "context": "1. <step>\\n2. <step>\\n3. <step>\\n..."
    }
  ]
}
Rules for "needs_more_files":
  - Use ONLY when context was truncated (you saw the ⚠️ CONTEXT TRUNCATED warning)
    AND you genuinely cannot produce a reliable diagnosis without those specific files.
  - List only paths that appeared in the truncation warning — do NOT invent new paths.
  - When this field is non-empty you MAY leave "tickets" as an empty array; the
    Manager will fetch the missing files and re-investigate.
  - Omit the field (or use []) when you have enough context to proceed.
Rules for "clarification_hint":
  - Populate ONLY when you are setting "needs_more_files" AND you have already reached the
    follow-up round limit (i.e., the Manager told you it cannot fetch more files).
  - Write a single short sentence suggesting what the user should do — e.g.,
    "Re-run with --context to include auth/middleware.py for a reliable diagnosis."
  - Omit the field (or use "") in all other cases.
Rules for "mode":
  - Set to "deep" when all sub-tasks are tightly coupled (share data structures,
    require reading the output of a previous step, or must be applied sequentially
    to a single logical unit of code). Typical examples: bug fix in one class,
    iterative refactor of a single algorithm, adding a feature to one module.
  - Set to "wide" when the sub-tasks are genuinely independent — touching completely
    separate files or modules with no shared state. Typical examples: rename across
    multiple files, add the same validation to N unrelated endpoints.
  - When uncertain, prefer "wide" — it is the safer default.
  - This is a soft signal; the Manager may override based on ticket count.
Rules for "clarification":
  - Emit ONLY when the request is genuinely ambiguous in a way that prevents you from
    choosing between fundamentally different implementation strategies.
  - DO NOT emit for uncertainty about files (use "needs_more_files" instead).
  - DO NOT emit for stylistic preferences or minor unknowns you can resolve yourself.
  - "options" MUST be a non-empty list of 2–4 short strings, each representing a valid
    interpretation of the user's request.
  - When "clarification" is present, you MUST still emit a best-guess "tickets" array
    so that headless (non-interactive) runs can proceed without blocking.
  - Omit the field entirely when the request is unambiguous.
Rules for "new_files":
  - Use "new_files" only when the sub-task must create a file that does not yet exist.
  - Omit the field (or use []) when the ticket only modifies existing files.
  - A new file path must not appear in any other ticket's "relevant_files" or "new_files".
  - Describe the new file's intended content and structure in the "context" field.
Return nothing but the JSON object — no prose, no markdown fences."""


def _merge_overlapping_tickets(tickets: list[Ticket]) -> list[Ticket]:
    """Merge any tickets that share at least one file into a single ticket.

    The LLM often splits single-file work across multiple tickets despite
    being told not to.  Multiple tickets touching the same file will always
    conflict at staging time, so we enforce the constraint here in code by
    union-finding all groups that overlap and combining them.

    Merged ticket keeps the id of the first ticket in the group.  Descriptions
    and context plans are concatenated in order so no intent is lost.
    """
    if not tickets:
        return tickets

    # Union-Find over ticket indices
    parent = list(range(len(tickets)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    # Build file → ticket-index map; union tickets that share any file (existing or new)
    file_to_idx: dict[str, int] = {}
    for i, ticket in enumerate(tickets):
        for f in ticket.relevant_files + ticket.new_files:
            if f in file_to_idx:
                union(i, file_to_idx[f])
            else:
                file_to_idx[f] = i

    # Group tickets by their root
    from collections import defaultdict
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(tickets)):
        groups[find(i)].append(i)

    merged: list[Ticket] = []
    for indices in groups.values():
        indices.sort()
        if len(indices) == 1:
            merged.append(tickets[indices[0]])
            continue
        # Merge the group into one ticket
        base = tickets[indices[0]]
        all_files: list[str] = []
        all_new_files: list[str] = []
        seen_files: set[str] = set()
        seen_new_files: set[str] = set()
        for idx in indices:
            for f in tickets[idx].relevant_files:
                if f not in seen_files:
                    all_files.append(f)
                    seen_files.add(f)
            for f in tickets[idx].new_files:
                if f not in seen_new_files:
                    all_new_files.append(f)
                    seen_new_files.add(f)
        all_context_files: list[str] = []
        seen_context_files: set[str] = set()
        for idx in indices:
            for f in tickets[idx].context_files:
                if f not in seen_context_files:
                    all_context_files.append(f)
                    seen_context_files.add(f)
        # A file that is now in relevant_files or new_files should not also
        # appear in context_files (the writable set takes precedence).
        writable = seen_files | seen_new_files
        all_context_files = [f for f in all_context_files if f not in writable]

        combined_desc = "; ".join(tickets[idx].description for idx in indices)
        combined_context = "\n\n".join(
            f"[Originally ticket-{tickets[idx].id}]\n{tickets[idx].context}"
            for idx in indices
            if tickets[idx].context
        )
        merged.append(Ticket(
            id=base.id,
            description=combined_desc,
            relevant_files=all_files,
            new_files=all_new_files,
            context_files=all_context_files,
            context=combined_context,
        ))

    # Restore stable order (by first-seen index of the root ticket)
    root_order = {find(i): i for i in range(len(tickets))}
    merged.sort(key=lambda t: root_order.get(
        next(i for i, tk in enumerate(tickets) if tk.id == t.id), 0
    ))
    return merged


class Manager:
    """Orchestrates Steps 2–4 of the Turbine lifecycle.

    Parameters
    ----------
    tree:
        The ``ProjectTree`` produced by Step 1 (TreeMapper).
    user_request:
        The natural-language task description supplied by the user.
    project_root:
        Absolute path to the project being modified.
    model:
        Mistral model name to use for all LLM calls.
    api_key:
        Mistral API key (defaults to ``MISTRAL_API_KEY`` env var).
    max_workers:
        Maximum number of worker coroutines to run concurrently.
    """

    def __init__(
        self,
        tree: ProjectTree,
        user_request: str,
        project_root: str | Path,
        model: str = "mistral-large-latest",
        api_key: str | None = None,
        max_workers: int = 4,
        test_commands: list[str] | None = None,
        dry_run: bool = False,
        review: bool = False,
        ui: TurbineUI | None = None,
        verbose: bool = False,
        git: "GitIntegration | None" = None,
        chat_id: str | None = None,
        json_ui: Any = None,
        static_check: str | None = None,
        budget: float | None = None,
        interactive: bool = False,
        mode_override: PipelineMode | None = None,
    ) -> None:
        self.tree = tree
        self.user_request = user_request
        self.project_root = Path(project_root)
        self.model = model
        self.max_workers = max_workers
        self.test_commands = test_commands or []
        self.dry_run = dry_run
        self.review = review
        self.verbose = verbose
        self.chat_id = chat_id
        self.interactive = interactive
        # Phase 20: pipeline mode — None means "auto-route after investigation"
        self.mode_override: PipelineMode | None = mode_override
        # Phase 14: static_check command — None means auto-detect from project files,
        # empty string "" disables the checker entirely.
        self.static_check = static_check
        # Phase 15: Cost & Token Tracking
        self._cost_tracker = CostTracker(
            model=model,
            budget_usd=budget,
            ui=json_ui,
        )
        self.ui = ui or TurbineUI(enabled=False)   # no-op by default
        self._json_ui = json_ui
        self.log = TurbineLogger(ui=json_ui)
        self.token_manager = TokenManager(model)
        self._client = Mistral(
            api_key=api_key or os.environ["MISTRAL_API_KEY"],
            timeout_ms=120_000,
        )
        self.vfs = VirtualFileSystem()
        self._git = git  # None when git integration is disabled

        # Per-file asyncio locks — workers that share a file are serialised
        # so only one holds the "write token" at a time.
        self._file_locks: dict[str, asyncio.Lock] = {}

        # Populated by each step
        self.relevant_files: list[str] = []
        self.diagnosis: str = ""
        self.tickets: list[Ticket] = []
        self.needs_more_files: list[str] = []       # Phase 11: paths requested by Investigator
        self.clarification_hint: str = ""            # Phase 11: hint logged when round cap hit
        # Phase 19: clarification gate
        self.clarification_question: str = ""        # question emitted by investigator
        self.clarification_options: list[str] = []   # answer options
        self.clarification_answer: str = ""          # user's chosen answer (injected before re-investigate)
        # Phase 20: resolved pipeline mode (set after investigate() + routing)
        self.mode: PipelineMode = PipelineMode.WIDE  # default until routing runs
        self._llm_mode_hint: PipelineMode = PipelineMode.WIDE  # soft signal from LLM
        self.worker_results: list[WorkerResult] = []
        self._original_snapshots: dict[str, list[str]] = {}

        # Populated by Step 5
        self.commit_result: CommitResult | None = None
        self.test_results: list[TestRunResult] = []
        self.repair_tasks: list[RepairTask] = []
        self.static_check_result: StaticCheckResult | None = None  # Phase 14

    # ------------------------------------------------------------------
    # Step 2: Preprocess
    # ------------------------------------------------------------------

    async def preprocess(self) -> list[str]:
        """Ask the LLM which files in the tree are relevant to the request.

        Returns
        -------
        list[str]
            Relative file paths selected by the LLM.
        """
        self.ui.on_step(PipelineStep.PREPROCESS, "pruning file tree…")
        self.log.thinking("Step 2 — Preprocess: pruning file tree…")

        tree_text = self.tree.summary()
        user_content = (
            f"User request:\n{self.user_request}\n\n"
            f"Project file tree:\n{tree_text}"
        )

        response = await self._chat(PREPROCESS_SYSTEM, user_content)
        raw = self._strip_fences(response)

        try:
            files = json.loads(raw)
            if not isinstance(files, list):
                raise ValueError("Expected a JSON array")
        except (json.JSONDecodeError, ValueError) as exc:
            self.log.error(f"Preprocess: could not parse LLM response — {exc}")
            # Fall back to returning all files so the pipeline can continue
            files = [f.relative for f in self.tree.files]

        self.relevant_files = [str(p) for p in files]

        # Guard: drop any path the LLM invented that is not actually in the
        # project tree.  The LLM sometimes hallucinates plausible-sounding
        # sibling files based on the user request rather than the tree.
        tree_paths: set[str] = {f.relative for f in self.tree.files}
        hallucinated = [p for p in self.relevant_files if p not in tree_paths]
        if hallucinated:
            self.relevant_files = [p for p in self.relevant_files if p in tree_paths]
            self.log.action(
                f"Preprocess: dropped {len(hallucinated)} path(s) not found in tree: "
                + ", ".join(hallucinated[:5])
                + (" …" if len(hallucinated) > 5 else "")
            )

        self.log.action(f"Preprocess complete — {len(self.relevant_files)} relevant file(s) selected.")
        return self.relevant_files

    # ------------------------------------------------------------------
    # Step 3: Investigate
    # ------------------------------------------------------------------

    async def investigate(self) -> list[Ticket]:
        """Read relevant files and ask the LLM to diagnose & decompose into tickets.

        Returns
        -------
        list[Ticket]
            Structured sub-tasks ready for delegation.
        """
        self.ui.on_step(PipelineStep.INVESTIGATE, "reading relevant files…")
        self.log.thinking("Step 3 — Investigate: reading relevant files…")

        # Phase 11: the investigation may loop up to MAX_INVESTIGATION_ROUNDS+1
        # times if the LLM signals it needs additional files.
        #
        # State that accumulates across rounds:
        #   loaded_rels  — paths already loaded into the VFS *and* in kept_chunks
        #   kept_chunks  — file-content chunks currently visible to the LLM
        loaded_rels: set[str] = set()
        kept_chunks: list[str] = []

        # ---- initial file load (always runs) ----
        for rel in self.relevant_files:
            abs_path = self.project_root / rel
            if not abs_path.is_file():
                self.log.error(f"Investigate: file not found — {rel}")
                continue
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            kept_chunks.append(f"### {rel}\n```\n{content}\n```")
            # Load into VFS for later diff staging
            self.vfs.load_from_disk(abs_path, relative_key=rel)
            # Capture the original snapshot before any diffs are applied
            snap = self.vfs.get_snapshot(rel)
            if snap is not None:
                self._original_snapshots[rel] = snap
            loaded_rels.add(rel)

        # Respect context window — drop files that overflow
        kept_chunks = self.token_manager.truncate_to_fit(kept_chunks)
        # Some files may have been dropped before VFS load (we loaded all then
        # trimmed the text), so loaded_rels stays as-is — what matters is what
        # text the LLM actually sees.
        dropped_rels: set[str] = set(self.relevant_files) - {
            self._chunk_names([c])[0] for c in kept_chunks
        }
        if dropped_rels:
            self.log.action(
                f"Investigate: dropped {len(dropped_rels)} file(s) — context budget exceeded: "
                + ", ".join(sorted(dropped_rels))
            )

        for follow_up_round in range(self.MAX_INVESTIGATION_ROUNDS + 1):
            files_text = "\n\n".join(kept_chunks)

            # Phase 11: if files were truncated, append a note so the LLM knows
            # the diagnosis may be incomplete and should flag uncertainty.
            truncation_note = ""
            if dropped_rels:
                truncation_note = (
                    "\n\n⚠️  CONTEXT TRUNCATED — the following file(s) could not fit in the "
                    "context window and were NOT provided: "
                    + ", ".join(sorted(dropped_rels))
                    + "\nIf diagnosing the request requires any of those files, set "
                    '"needs_more_files" to their paths in your response instead of '
                    "guessing.  Do not hallucinate content for files you have not seen."
                )

            # Phase 19: if the user answered a clarification question, inject it
            answer_note = ""
            if self.clarification_answer:
                answer_note = (
                    f"\n\nClarification from the user: {self.clarification_answer}\n"
                    "Use this answer to resolve the ambiguity and produce a final ticket plan. "
                    "Do NOT emit a 'clarification' block this time."
                )

            user_content = (
                f"User request:\n{self.user_request}\n\n"
                f"Relevant file contents:\n{files_text}"
                + truncation_note
                + answer_note
            )

            response = await self._chat(INVESTIGATE_SYSTEM, user_content)
            raw = self._strip_fences(response)

            try:
                data = json.loads(raw)
                self.diagnosis = data.get("diagnosis", "")
                self.needs_more_files = [
                    str(p) for p in data.get("needs_more_files", [])
                    if isinstance(p, str)
                ]
                hint = data.get("clarification_hint", "")
                self.clarification_hint = str(hint).strip() if hint else ""

                # Phase 19: parse clarification gate
                clarification_block = data.get("clarification")
                if isinstance(clarification_block, dict):
                    self.clarification_question = str(clarification_block.get("question", "")).strip()
                    opts = clarification_block.get("options", [])
                    self.clarification_options = [str(o) for o in opts if o] if isinstance(opts, list) else []
                else:
                    self.clarification_question = ""
                    self.clarification_options = []

                # Phase 20: parse LLM's mode recommendation (soft signal only)
                llm_mode_raw = str(data.get("mode", "wide")).strip().lower()
                self._llm_mode_hint = PipelineMode.DEEP if llm_mode_raw == "deep" else PipelineMode.WIDE

                raw_tickets = data.get("tickets", [])
                self.tickets = [
                    Ticket(
                        id=t.get("id", f"ticket-{i+1}"),
                        description=t.get("description", ""),
                        relevant_files=t.get("relevant_files", []),
                        new_files=t.get("new_files", []),
                        context_files=t.get("context_files", []),
                        context=t.get("context", ""),
                    )
                    for i, t in enumerate(raw_tickets)
                ]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                self.log.error(f"Investigate: could not parse LLM response — {exc}")
                self.tickets = []
                self.needs_more_files = []
                self.clarification_hint = ""
                self.clarification_question = ""
                self.clarification_options = []
                self._llm_mode_hint = PipelineMode.WIDE

            # Phase 11: follow-up read — load any files the LLM requested and
            # re-investigate, provided we have budget and rounds remaining.
            if not self.needs_more_files or follow_up_round >= self.MAX_INVESTIGATION_ROUNDS:
                if self.needs_more_files and follow_up_round >= self.MAX_INVESTIGATION_ROUNDS:
                    self.log.action(
                        "Investigate: follow-up limit reached — proceeding with partial context."
                    )
                    if self.clarification_hint:
                        self.log.error(
                            f"⚠️  INVESTIGATION HINT: {self.clarification_hint}"
                        )
                break  # done (either satisfied or limit hit)

            # Filter to paths that are actually on disk and not already loaded
            requested = [
                p for p in self.needs_more_files
                if p not in loaded_rels and (self.project_root / p).is_file()
            ]
            invalid = [p for p in self.needs_more_files if p not in requested and p not in loaded_rels]
            if invalid:
                self.log.error(
                    f"Investigate: follow-up requested unknown/missing path(s): "
                    + ", ".join(invalid)
                )

            if not requested:
                self.log.action("Investigate: no new files to load for follow-up — proceeding.")
                break

            self.log.thinking(
                f"Investigate: follow-up round {follow_up_round + 1}/{self.MAX_INVESTIGATION_ROUNDS} "
                f"— loading {len(requested)} additional file(s): "
                + ", ".join(requested)
            )
            new_chunks: list[str] = []
            for rel in requested:
                abs_path = self.project_root / rel
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                new_chunks.append(f"### {rel}\n```\n{content}\n```")
                if rel not in self.vfs.all_snapshots():
                    self.vfs.load_from_disk(abs_path, relative_key=rel)
                    snap = self.vfs.get_snapshot(rel)
                    if snap is not None:
                        self._original_snapshots[rel] = snap
                loaded_rels.add(rel)

            # Check how many of the new chunks fit in the remaining budget
            candidate_chunks = kept_chunks + new_chunks
            fitted = self.token_manager.truncate_to_fit(candidate_chunks)
            added = fitted[len(kept_chunks):]
            if not added:
                self.log.action(
                    "Investigate: follow-up files exceed context budget — proceeding without them."
                )
                break
            kept_chunks = fitted
            dropped_rels -= set(requested[:len(added)])

        # Phase 10: load context_files into the VFS (read-only — no baseline
        # staging, just snapshot so workers can read their contents).
        # Only load files not already in the VFS from the relevant-files pass.
        all_context_paths: set[str] = {
            f for t in self.tickets for f in t.context_files
        }
        for rel in all_context_paths:
            if rel in self.vfs.all_snapshots():
                continue  # already loaded as a relevant file
            abs_path = self.project_root / rel
            if not abs_path.is_file():
                self.log.error(f"Investigate: context file not found — {rel}")
                continue
            self.vfs.load_from_disk(abs_path, relative_key=rel)

        # Phase 8: if the LLM puts a non-existent path in relevant_files, move it
        # to new_files.  The VFS never loaded it (the file-read loop above skipped
        # it), so leaving it in relevant_files means the worker would see an empty
        # snapshot with no baseline and silently produce no diff.
        for ticket in self.tickets:
            on_disk, to_create = [], []
            for f in ticket.relevant_files:
                (on_disk if (self.project_root / f).is_file() else to_create).append(f)
            if to_create:
                ticket.relevant_files = on_disk
                seen_new = set(ticket.new_files)
                ticket.new_files = ticket.new_files + [
                    f for f in to_create if f not in seen_new
                ]
                self.log.action(
                    f"  [{ticket.id}] reclassified {len(to_create)} non-existent "
                    f"path(s) → new_files: {', '.join(to_create)}"
                )

        # Guard: if the user mentioned a bare filename (no path) and the LLM put
        # it inside an invented subdirectory, strip the invented prefix so the
        # file lands where the user actually expected it.
        import re as _re
        _req_basenames: set[str] = {
            Path(m).name.lower()
            for m in _re.findall(r'[\w.-]+\.\w+', self.user_request)
        }
        for ticket in self.tickets:
            normalised: list[str] = []
            for f in ticket.new_files:
                basename = Path(f).name.lower()
                if "/" in f and basename in _req_basenames:
                    corrected = Path(f).name
                    self.log.action(
                        f"  [{ticket.id}] new file path corrected: "
                        f"'{f}' → '{corrected}' (matches user-requested filename)"
                    )
                    normalised.append(corrected)
                else:
                    normalised.append(f)
            ticket.new_files = normalised

        # Enforce: no two tickets may share a file.  Merge any that do so the
        # LLM's tendency to split single-file work into multiple tickets doesn't
        # cause guaranteed conflicts at delegation time.
        before = len(self.tickets)
        self.tickets = _merge_overlapping_tickets(self.tickets)
        if len(self.tickets) < before:
            self.log.action(
                f"Investigate: merged {before} ticket(s) → {len(self.tickets)} "
                "(shared-file collision resolved)"
            )

        self.log.action(
            f"Investigate complete - {len(self.tickets)} ticket(s) created."
        )
        if self.diagnosis:
            self.log.thinking(f"Diagnosis: {self.diagnosis}")
        for ticket in self.tickets:
            files_label = ", ".join(ticket.relevant_files) if ticket.relevant_files else "-"
            new_files_label = (
                f" | new: {', '.join(ticket.new_files)}" if ticket.new_files else ""
            )
            ctx_files_label = (
                f" | ctx: {', '.join(ticket.context_files)}" if ticket.context_files else ""
            )
            self.log.action(
                f"  [{ticket.id}] {ticket.description}  |  files: {files_label}"
                f"{new_files_label}{ctx_files_label}"
            )
            if self.verbose and ticket.context:
                self.log.verbose(f"    context: {ticket.context}")
        return self.tickets

    # ------------------------------------------------------------------
    # Step 4: Delegate
    # ------------------------------------------------------------------

    async def delegate(self) -> list[WorkerResult]:
        """Spawn one worker coroutine per ticket, bounded by ``max_workers``.

        In Phase 3 the workers are stubs that acknowledge the ticket.
        Phase 4 will replace ``_run_worker`` with the real LLM worker loop.

        Returns
        -------
        list[WorkerResult]
            One result per ticket, in ticket order.
        """
        if not self.tickets:
            self.log.action("Delegate: no tickets to process.")
            return []

        self.ui.on_step(
            PipelineStep.DELEGATE,
            f"spawning {len(self.tickets)} worker(s)…",
        )
        self.log.thinking(
            f"Step 4 — Delegate: spawning {len(self.tickets)} worker(s) "
            f"(max concurrency: {self.max_workers})…"
        )

        semaphore = asyncio.Semaphore(self.max_workers)

        # Build one lock per unique file across all tickets (existing and new).
        # Workers that share a file will acquire its lock before proposing
        # a diff, serialising them so the second worker sees the first
        # worker's committed changes rather than racing against them.
        all_files = {
            f for t in self.tickets for f in t.relevant_files + t.new_files
        }
        for f in all_files:
            self._file_locks.setdefault(f, asyncio.Lock())

        # Phase 8: load empty stubs for new files into the VFS so workers can
        # propose diffs against them.  Store an empty baseline in
        # _original_snapshots so CommitEngine skips stubs that were never filled.
        for ticket in self.tickets:
            for new_file in ticket.new_files:
                if new_file not in self.vfs.all_snapshots():
                    self.vfs.load_text(new_file, "")
                    self._original_snapshots[new_file] = []

        async def bounded(ticket: Ticket) -> WorkerResult:
            async with semaphore:
                return await self._run_worker(ticket)

        results = await asyncio.gather(*(bounded(t) for t in self.tickets))
        self.worker_results = list(results)

        successes = sum(1 for r in self.worker_results if r.success)
        self.log.action(
            f"Delegate complete - {successes}/{len(self.worker_results)} worker(s) succeeded."
        )
        for result in self.worker_results:
            if result.success:
                lines = result.proposed_diff.count("\n") if result.proposed_diff else 0
                detail = f"{lines} diff line(s)" if lines else "no changes"
                self.log.action(f"  [{result.ticket_id}] approved — {detail}")
            else:
                self.log.error(f"  [{result.ticket_id}] failed — {result.error[:120]}")
        return self.worker_results

    # ------------------------------------------------------------------
    # Worker (Phase 4)
    # ------------------------------------------------------------------

    async def _run_worker(self, ticket: Ticket) -> WorkerResult:
        """Spin up a Worker for *ticket* and return its result."""
        from turbine.worker import Worker  # local import avoids circular dependency

        # Collect the per-file locks for the files this ticket touches.
        # Include new_files so _check_and_stage can serialise concurrent creation
        # of the same new path across workers (even though _merge_overlapping_tickets
        # should prevent it, defence-in-depth is cheap here).
        file_locks = {
            f: self._file_locks[f]
            for f in ticket.relevant_files + ticket.new_files
            if f in self._file_locks
        }

        worker = Worker(
            ticket=ticket,
            vfs=self.vfs,
            client=self._client,
            model=self.model,
            token_manager=self.token_manager,
            diagnosis=self.diagnosis,
            file_locks=file_locks,
            ui=self.ui,
            verbose=self.verbose,
            json_ui=self._json_ui,
            cost_tracker=self._cost_tracker,
        )
        return await worker.run()

    # ------------------------------------------------------------------
    # Step 5: Commit & Verify
    # ------------------------------------------------------------------

    async def verify(self) -> tuple[CommitResult, list[TestRunResult], list[RepairTask]]:
        """Commit VFS to disk (with git integration) and run static check + tests.

        Uses :class:`GitAwareCommitEngine` so every disk write —
        including repair-loop re-writes — is automatically followed by
        a ``git add`` + ``git commit``.

        Phase 14: after a successful commit, a fast static check (ruff, mypy,
        tsc) runs *before* the test suite.  If the checker finds errors that
        can be attributed to specific workers, those workers' repair tasks are
        returned immediately and the full test suite is skipped for this round.
        The test suite only runs once the static check is clean.

        Returns
        -------
        (commit_result, test_results, repair_tasks)
            ``repair_tasks`` is non-empty when static check or tests fail and
            responsible workers can be identified.
        """
        self.ui.on_step(PipelineStep.COMMIT, "writing VFS to disk…")
        self.log.thinking("Step 5 — Commit & Verify: writing VFS to disk…")

        engine = GitAwareCommitEngine(
            vfs=self.vfs,
            project_root=self.project_root,
            git=self._git,
            dry_run=self.dry_run,
            original_snapshots=self._original_snapshots,
            diagnosis=self.diagnosis,
            tickets=self.tickets,
            user_request=self.user_request,
        )
        self.commit_result = engine.commit()

        if not self.commit_result.success:
            self.log.error(
                f"Commit engine reported errors: {self.commit_result.summary()}"
            )
            return self.commit_result, [], []

        # Phase 14: static check — runs after disk write, before test suite.
        if not self.dry_run:
            static_repair_tasks = await self._run_static_check()
            if static_repair_tasks:
                # Attribute errors to workers and short-circuit before tests.
                self.repair_tasks = static_repair_tasks
                return self.commit_result, [], static_repair_tasks

        if not self.test_commands:
            self.log.action("Step 5 — no test commands configured; skipping tests.")
            return self.commit_result, [], []

        # Build worker → files map for failure attribution
        worker_file_map: dict[str, list[str]] = {
            t.id: t.relevant_files for t in self.tickets
        }
        ticket_descriptions: dict[str, str] = {
            t.id: t.description for t in self.tickets
        }

        runner = TestRunner(
            project_root=self.project_root,
            commands=self.test_commands,
            worker_file_map=worker_file_map,
            ticket_descriptions=ticket_descriptions,
        )
        self.test_results, self.repair_tasks = await runner.run()

        if self.repair_tasks:
            self.log.thinking(
                f"Step 5 — {len(self.repair_tasks)} worker(s) flagged for repair."
            )
        else:
            self.log.action("Step 5 — all tests passed.")

        return self.commit_result, self.test_results, self.repair_tasks

    # ------------------------------------------------------------------
    # Step 5b: Repair loop
    # ------------------------------------------------------------------

    MAX_REPAIR_ROUNDS = 3          # Phase 12: round 1 = constraint-only, rounds 2-3 = full re-run
    MAX_INVESTIGATION_ROUNDS = 2  # Phase 11: max follow-up read rounds

    async def _repair_loop(
        self,
        repair_tasks: list[RepairTask],
        round_num: int = 1,
    ) -> None:
        """Re-run workers that have failing tests, then re-verify.

        Each failing worker is re-spawned with the test failure output
        appended to its conversation so the LLM can fix its own code.
        After all repairs complete, the VFS is re-committed and tests
        are re-run.  This repeats up to ``MAX_REPAIR_ROUNDS`` times.
        """
        if not repair_tasks or round_num > self.MAX_REPAIR_ROUNDS:
            if round_num > self.MAX_REPAIR_ROUNDS:
                self.log.error(
                    f"Repair loop exhausted after {self.MAX_REPAIR_ROUNDS} round(s) — "
                    "tests still failing."
                )
            return

        # Phase 12: round 1 uses constraint-only (targeted) repair;
        # subsequent rounds fall back to the full worker re-run.
        constraint_only = (round_num == 1)
        strategy_label = "constraint-only" if constraint_only else "full re-run"
        self.log.thinking(
            f"Step 5b — Repair round {round_num}/{self.MAX_REPAIR_ROUNDS} "
            f"[{strategy_label}]: re-running {len(repair_tasks)} worker(s)…"
        )

        # Build a lookup from worker_id → Ticket (also used for Phase 18 below)
        ticket_by_id: dict[str, Ticket] = {t.id: t for t in self.tickets}

        # Phase 18: signal repair to the UI with the files needing repair so
        # the VS Code extension can show inline diagnostics (squiggles).
        for rt in repair_tasks:
            ticket = ticket_by_id.get(rt.worker_id)
            repair_files = ticket.relevant_files if ticket else []
            self.ui.on_worker_repair(rt.worker_id, files=repair_files)

        async def _repair_one(rt: RepairTask) -> WorkerResult:
            from turbine.worker import Worker  # avoid circular import

            ticket = ticket_by_id.get(rt.worker_id)
            if ticket is None:
                return WorkerResult(
                    ticket_id=rt.worker_id,
                    success=False,
                    error=f"No ticket found for worker '{rt.worker_id}'",
                )

            file_locks = {
                f: self._file_locks[f]
                for f in ticket.relevant_files
                if f in self._file_locks
            }

            worker = Worker(
                ticket=ticket,
                vfs=self.vfs,
                client=self._client,
                model=self.model,
                token_manager=self.token_manager,
                diagnosis=self.diagnosis,
                file_locks=file_locks,
                ui=self.ui,
                verbose=self.verbose,
                json_ui=self._json_ui,
                cost_tracker=self._cost_tracker,
            )
            # Inject test failure; use targeted prompt on round 1
            return await worker.run(
                repair_feedback=rt.failure_output,
                constraint_only=constraint_only,
            )

        repair_semaphore = asyncio.Semaphore(self.max_workers)

        async def bounded_repair(rt: RepairTask) -> WorkerResult:
            async with repair_semaphore:
                return await _repair_one(rt)

        repair_results = await asyncio.gather(
            *(bounded_repair(rt) for rt in repair_tasks)
        )

        # Merge repair results back into worker_results
        result_by_id = {r.ticket_id: r for r in self.worker_results}
        for r in repair_results:
            result_by_id[r.ticket_id] = r
        self.worker_results = list(result_by_id.values())

        # Re-commit and re-test
        _, _, next_repair_tasks = await self.verify()
        await self._repair_loop(next_repair_tasks, round_num + 1)

    # ------------------------------------------------------------------
    # Phase 20: Deep Mode execution
    # ------------------------------------------------------------------

    async def _run_deep_mode(self) -> None:
        """Execute the DEEP pipeline using a DeepAgent tool-calling loop.

        The agent reads/writes files through the VFS, so CommitEngine and
        the review gate work identically to Wide Mode.  Exhaustion forces
        ``--review``.
        """
        from turbine.deep_agent import DeepAgent

        self.ui.on_step(PipelineStep.DELEGATE, "deep mode — tool-calling agent…")
        self.log.thinking("Step 4 (DEEP) — spawning DeepAgent…")

        # Use the first (and should be only) ticket; if empty create a synthetic one
        if self.tickets:
            ticket = self.tickets[0]
        else:
            from turbine.manager import Ticket
            ticket = Ticket(
                id="deep-1",
                description=self.user_request,
                relevant_files=self.relevant_files,
            )

        max_iter = getattr(self, "_deep_max_iterations", 20)
        agent = DeepAgent(
            ticket=ticket,
            vfs=self.vfs,
            manager=self,
            max_iterations=max_iter,
        )
        result = await agent.run()

        # Convert DeepAgentResult to WorkerResult so the rest of the pipeline
        # (verify, repair, report) can treat it identically to a Wide Mode run.
        from turbine.manager import WorkerResult
        worker_result = WorkerResult(
            ticket_id=ticket.id,
            success=result.success,
            error="" if result.success else f"exhausted after {result.iterations_used} iterations",
        )
        self.worker_results = [worker_result]

        if result.exhausted and not self.dry_run:
            self.log.error(
                "⚠️  DEEP MODE EXHAUSTED — agent did not call done() within the iteration "
                "budget. Forcing --review so you can inspect the partial changes."
            )
            self.review = True

    # ------------------------------------------------------------------
    # Full pipeline: Steps 2 → 3 → 4 → 5
    # ------------------------------------------------------------------

    async def run(self) -> list[WorkerResult]:
        """Execute Steps 2, 3, 4, and (if configured) 5 in sequence.

        Git lifecycle is handled inside this method:
        - Branch creation before delegation
        - Review gate (when ``self.review`` is True)
        - Cleanup of empty branches on no-op runs
        """
        self.ui.on_step(PipelineStep.DISCOVER, "mapping project tree…")

        # Phase 22: Preflight + smart branch creation before any disk writes
        if self._git and self._git.is_repo and not self.dry_run:
            preflight = self._git.preflight()
            if preflight.dirty_files:
                self.log.thinking(
                    f"Git: {len(preflight.dirty_files)} uncommitted change(s) detected — "
                    "branching from current disk state (manual edits become new baseline)."
                )
                for f in preflight.dirty_files[:8]:
                    self.log.thinking(f"  {f}")
                if len(preflight.dirty_files) > 8:
                    self.log.thinking(f"  … and {len(preflight.dirty_files) - 8} more")
            try:
                branch = self._git.create_branch(preflight=preflight)
                if branch:
                    if self._git.resumed:
                        self.log.action(f"Git: resumed existing branch '{branch}'")
                    else:
                        self.log.action(f"Git: created branch '{branch}'")
                    if self._git.disk_changed:
                        # New-chat base-branch reset removed turbine-committed files
                        # from disk — re-map the tree so the LLM sees the clean state.
                        from turbine.tree_mapper import TreeMapper
                        self.tree = TreeMapper(self.project_root).map()
                        self.log.action(
                            f"Tree re-mapped after base-branch reset — "
                            f"{len(self.tree.files)} file(s) found."
                        )
            except RuntimeError as exc:
                self.log.error(f"Git: could not create branch — {exc}")
                self.log.thinking("Git: continuing on current branch.")

        await self.preprocess()
        await self.investigate()

        # Phase 19: Clarification Gate — runs after the first investigation pass.
        if self.clarification_question and self.clarification_options:
            # Always emit the JSON event so the VS Code extension (or any
            # --json-events consumer) can render a question widget regardless
            # of whether --interactive is set.
            if self._json_ui is not None:
                self._json_ui.on_clarification_request(
                    question=self.clarification_question,
                    options=self.clarification_options,
                )
            if self.interactive:
                # Interactive mode: block on stdin for the answer, then
                # re-investigate with the answer injected into context.
                answer = await self._ask_clarification_interactive()
                self.clarification_answer = answer
                # Re-run investigate so the answer is injected via answer_note.
                await self.investigate()
            else:
                # Non-interactive: log warning and proceed with best-guess.
                self.log.error(
                    f"⚠️  CLARIFICATION NEEDED (best-guess used — run with --interactive "
                    f"to answer): {self.clarification_question}"
                )

        # Phase 20: resolve pipeline mode after investigation
        self.mode = self._resolve_mode()
        mode_label = self.mode.value.upper()
        self.log.action(
            f"Pipeline mode: {mode_label} "
            f"({'override' if self.mode_override else 'auto-routed'})"
        )
        # Notify UIs about selected mode
        self.ui.on_mode(self.mode)
        if self._json_ui is not None:
            self._json_ui.on_mode(self.mode)

        # Phase 12: if any files were truncated out of context AND the follow-up
        # loop could not resolve them, force --review mode and print a prominent
        # warning.  Never silently proceed on incomplete context.
        if getattr(self, 'needs_more_files', []) and not self.dry_run:
            self.log.error(
                "⚠️  INCOMPLETE CONTEXT — investigation could not load all required files: "
                + ", ".join(self.needs_more_files)
            )
            self.log.error(
                "Forcing --review mode so you can inspect changes before they are written."
            )
            self.review = True

        if self.mode == PipelineMode.DEEP:
            await self._run_deep_mode()
        else:
            await self.delegate()

        # Phase 12: if any worker flagged its output as uncertain, force --review
        # so the user inspects the changes before they are written to disk.
        uncertain_workers = [r.ticket_id for r in self.worker_results if r.uncertain]
        if uncertain_workers and not self.dry_run:
            self.log.error(
                "⚠️  UNCERTAIN OUTPUT — the following worker(s) flagged their changes as uncertain: "
                + ", ".join(uncertain_workers)
            )
            self.log.error(
                "Forcing --review mode so you can inspect changes before they are written."
            )
            self.review = True

        if self.review and not self.dry_run:
            # Review gate: show diff, prompt, then commit + test + repair
            engine = GitAwareCommitEngine(
                vfs=self.vfs,
                project_root=self.project_root,
                git=self._git,
                dry_run=False,
                original_snapshots=self._original_snapshots,
                diagnosis=self.diagnosis,
                tickets=self.tickets,
                user_request=self.user_request,
            )
            self.commit_result = engine.review_and_commit()

            # Run tests and repair loop after a confirmed review commit
            if self.commit_result.written_count and self.test_commands:
                _, _, repair_tasks = await self._run_tests()
                if repair_tasks:
                    await self._repair_loop(repair_tasks)

        elif self.test_commands or not self.dry_run:
            # Normal path: commit + test + repair
            _, _, repair_tasks = await self.verify()
            if repair_tasks:
                await self._repair_loop(repair_tasks)

        successes = sum(1 for r in self.worker_results if r.success)
        self.ui.on_done(
            f"{successes}/{len(self.worker_results)} worker(s) succeeded"
            if self.worker_results else "pipeline complete"
        )

        # Emit structured detail for JSON-mode consumers (VS Code extension etc.)
        from turbine.json_ui import JsonEventUI
        if isinstance(self.ui, JsonEventUI):
            files_written = self.commit_result.written_count if self.commit_result else 0
            diff_lines = sum(
                len(r.proposed_diff.splitlines()) for r in self.worker_results
            )
            self.ui.on_done_detail(
                workers_succeeded=successes,
                workers_total=len(self.worker_results),
                files_written=files_written,
                diff_lines=diff_lines,
                dry_run=self.dry_run,
                tickets=[t.to_dict() for t in self.tickets],
                diagnosis=self.diagnosis,
                total_input_tokens=self._cost_tracker.total_input_tokens,
                total_output_tokens=self._cost_tracker.total_output_tokens,
                total_cost_usd=self._cost_tracker.total_cost_usd,
            )

        # Phase 12: unverified-run warning — alert when changes were written
        # with no way to validate correctness.
        if (
            not self.dry_run
            and not self.test_commands
            and self.commit_result
            and self.commit_result.written_count
        ):
            self.log.error(
                "⚠️  UNVERIFIED RUN — no --test commands are configured. "
                "Changes have been written to disk but have not been validated. "
                "Consider using --dry-run or --review to inspect changes before committing."
            )

        # Phase 7: Cleanup empty branch if nothing was written
        if not self.dry_run and not self.review and self._git:
            nothing_written = not (
                self.commit_result and self.commit_result.written_count
            )
            if nothing_written and self._git.cleanup_if_empty():
                self.log.action(
                    "Git: no changes made — removed empty branch, "
                    "returned to original branch."
                )

        # Phase 15: Print cost report at the end of every run
        if self._cost_tracker.total_calls:
            self.log.action(self._cost_tracker.report())

        return self.worker_results

    # ------------------------------------------------------------------
    # Phase 14: Static check helper
    # ------------------------------------------------------------------

    async def _run_static_check(self) -> list[RepairTask]:
        """Run the configured (or auto-detected) static checker.

        Returns a (possibly empty) list of :class:`RepairTask` objects
        attributed to the workers whose files triggered type/lint errors.
        Returns an empty list when the checker is disabled, passes, or
        produces errors that cannot be attributed to any worker.
        """
        worker_file_map: dict[str, list[str]] = {
            t.id: t.relevant_files for t in self.tickets
        }
        ticket_descriptions: dict[str, str] = {
            t.id: t.description for t in self.tickets
        }
        checker = StaticChecker(
            project_root=self.project_root,
            command=self.static_check,
            worker_file_map=worker_file_map,
            ticket_descriptions=ticket_descriptions,
        )
        if not checker.has_command:
            return []
        result, repair_tasks = await checker.run()
        self.static_check_result = result
        return repair_tasks

    # ------------------------------------------------------------------
    # Post-commit test runner (used by review gate path)
    # ------------------------------------------------------------------

    async def _run_tests(self) -> tuple[CommitResult | None, list[TestRunResult], list[RepairTask]]:
        """Run static check + test commands — no disk commit.

        Used by the review-gate path where the disk write has already
        happened via ``review_and_commit()``.

        Phase 14: runs the static checker before the test suite; returns
        static repair tasks immediately if errors are attributed to workers.
        """
        # Phase 14: static check before test suite (review gate path)
        static_repair_tasks = await self._run_static_check()
        if static_repair_tasks:
            self.repair_tasks = static_repair_tasks
            return self.commit_result, [], static_repair_tasks

        if not self.test_commands:
            return self.commit_result, [], []

        worker_file_map: dict[str, list[str]] = {
            t.id: t.relevant_files for t in self.tickets
        }
        ticket_descriptions: dict[str, str] = {
            t.id: t.description for t in self.tickets
        }
        runner = TestRunner(
            project_root=self.project_root,
            commands=self.test_commands,
            worker_file_map=worker_file_map,
            ticket_descriptions=ticket_descriptions,
        )
        self.test_results, self.repair_tasks = await runner.run()
        return self.commit_result, self.test_results, self.repair_tasks

    # ------------------------------------------------------------------
    # Git hints for the final report
    # ------------------------------------------------------------------

    def git_hints(self) -> dict[str, str]:
        """Return git shell commands for the final report.

        Keys: ``merge``, ``undo``.  Values are shell commands
        (empty string if not applicable).  Turbine never performs merges
        directly — it only surfaces the commands for the user to run.
        """
        if self._git is None:
            return {"merge": "", "undo": ""}
        return {
            "merge": self._git.merge_hint(),
            "undo": self._git.undo_hint(),
        }

    # ------------------------------------------------------------------
    # Phase 20: Pipeline routing
    # ------------------------------------------------------------------

    def _resolve_mode(self) -> PipelineMode:
        """Determine the pipeline mode after investigation.

        Precedence:
        1. ``--mode`` CLI override (``mode_override``) always wins.
        2. If exactly 1 ticket after merging → DEEP.
        3. If 2+ tickets with no shared files → WIDE.
        4. LLM ``mode`` hint from the investigation response (soft signal).
        5. Default: WIDE.
        """
        if self.mode_override is not None:
            return self.mode_override
        if len(self.tickets) == 1:
            return PipelineMode.DEEP
        if len(self.tickets) >= 2:
            return PipelineMode.WIDE
        # 0 tickets — fall back to LLM hint
        return self._llm_mode_hint

    # ------------------------------------------------------------------
    # Phase 19: Clarification gate helpers
    # ------------------------------------------------------------------

    async def _ask_clarification_interactive(self) -> str:
        """Prompt the user on stdin to resolve an ambiguous request.

        Prints the question and numbered options, reads a line from stdin
        (in a thread-executor so the event loop is not blocked), and returns
        the selected answer string.

        Falls back to option 1 if stdin is not a TTY or the user enters
        nothing / an invalid number.
        """
        import sys
        loop = asyncio.get_event_loop()

        question = self.clarification_question
        options = self.clarification_options

        def _prompt() -> str:
            print(f"\n[Turbine] Clarification needed:\n  {question}")
            for i, opt in enumerate(options, 1):
                print(f"  {i}. {opt}")
            try:
                raw = input("Enter number (or type your answer): ").strip()
            except (EOFError, OSError):
                return options[0] if options else ""
            # Accept a numeric choice
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(options):
                    return options[idx]
            # Accept free-text answer
            return raw if raw else (options[0] if options else "")

        answer = await loop.run_in_executor(None, _prompt)
        self.log.action(f"Clarification answered: {answer!r}")
        return answer

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _chunk_names(chunks: list[str]) -> list[str]:
        """Extract the file path from each ``### path\\n``` ``` `` chunk header."""
        names: list[str] = []
        for chunk in chunks:
            first_line = chunk.splitlines()[0] if chunk else ""
            # Headers look like "### path/to/file.py" or "### path [LARGE FILE …]"
            name = first_line.lstrip("# ").split(" [")[0].strip()
            if name:
                names.append(name)
        return names

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove markdown code fences that the LLM sometimes wraps JSON in."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:])
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        return text.strip()

    async def _chat(self, system: str, user: str) -> str:
        """Send a single-turn chat request and return the assistant's text."""
        response = await self._client.chat.complete_async(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        # Phase 15: record token usage
        usage = getattr(response, "usage", None)
        if usage is not None:
            self._cost_tracker.record(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            )
        return response.choices[0].message.content or ""
