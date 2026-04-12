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
from pathlib import Path
from typing import Any, TYPE_CHECKING

from mistralai.client import Mistral

from turbine.commit_engine import CommitEngine, CommitResult, GitAwareCommitEngine
from turbine.logger import TurbineLogger
from turbine.test_runner import RepairTask, TestRunResult, TestRunner
from turbine.token_manager import TokenManager
from turbine.tree_mapper import ProjectTree
from turbine.ui import PipelineStep, TurbineUI
from turbine.vfs import ConflictDetector, VirtualFileSystem

if TYPE_CHECKING:
    from turbine.git_integration import GitIntegration

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "relevant_files": self.relevant_files,
            "new_files": self.new_files,
            "context": self.context,
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

Return ONLY a JSON object with this exact shape:
{
  "diagnosis": "<thorough description of the root cause or goal — be specific>",
  "tickets": [
    {
      "id": "ticket-1",
      "description": "<what this sub-task achieves>",
      "relevant_files": ["<relative path of existing file to modify>", ...],
      "new_files": ["<relative path of brand-new file to CREATE>", ...],
      "context": "1. <step>\\n2. <step>\\n3. <step>\\n..."
    }
  ]
}
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
        self.ui = ui or TurbineUI(enabled=False)   # no-op by default
        self.log = TurbineLogger()
        self.token_manager = TokenManager(model)
        self._client = Mistral(api_key=api_key or os.environ["MISTRAL_API_KEY"])
        self.vfs = VirtualFileSystem()
        self._git = git  # None when git integration is disabled

        # Per-file asyncio locks — workers that share a file are serialised
        # so only one holds the "write token" at a time.
        self._file_locks: dict[str, asyncio.Lock] = {}

        # Populated by each step
        self.relevant_files: list[str] = []
        self.diagnosis: str = ""
        self.tickets: list[Ticket] = []
        self.worker_results: list[WorkerResult] = []
        self._original_snapshots: dict[str, list[str]] = {}

        # Populated by Step 5
        self.commit_result: CommitResult | None = None
        self.test_results: list[TestRunResult] = []
        self.repair_tasks: list[RepairTask] = []

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

        file_chunks: list[str] = []
        for rel in self.relevant_files:
            abs_path = self.project_root / rel
            if not abs_path.is_file():
                self.log.error(f"Investigate: file not found — {rel}")
                continue
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            file_chunks.append(f"### {rel}\n```\n{content}\n```")
            # Load into VFS for later diff staging
            self.vfs.load_from_disk(abs_path, relative_key=rel)
            # Capture the original snapshot before any diffs are applied
            snap = self.vfs.get_snapshot(rel)
            if snap is not None:
                self._original_snapshots[rel] = snap

        # Respect context window — drop files that overflow
        kept = self.token_manager.truncate_to_fit(file_chunks)
        if len(kept) < len(file_chunks):
            dropped = len(file_chunks) - len(kept)
            self.log.action(f"Investigate: dropped {dropped} file(s) — context budget exceeded.")

        files_text = "\n\n".join(kept)
        user_content = (
            f"User request:\n{self.user_request}\n\n"
            f"Relevant file contents:\n{files_text}"
        )

        response = await self._chat(INVESTIGATE_SYSTEM, user_content)
        raw = self._strip_fences(response)

        try:
            data = json.loads(raw)
            self.diagnosis = data.get("diagnosis", "")
            raw_tickets = data.get("tickets", [])
            self.tickets = [
                Ticket(
                    id=t.get("id", f"ticket-{i+1}"),
                    description=t.get("description", ""),
                    relevant_files=t.get("relevant_files", []),
                    new_files=t.get("new_files", []),
                    context=t.get("context", ""),
                )
                for i, t in enumerate(raw_tickets)
            ]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            self.log.error(f"Investigate: could not parse LLM response — {exc}")
            self.tickets = []

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
            self.log.action(
                f"  [{ticket.id}] {ticket.description}  |  files: {files_label}{new_files_label}"
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
        )
        return await worker.run()

    # ------------------------------------------------------------------
    # Step 5: Commit & Verify
    # ------------------------------------------------------------------

    async def verify(self) -> tuple[CommitResult, list[TestRunResult], list[RepairTask]]:
        """Commit VFS to disk (with git integration) and run tests.

        Uses :class:`GitAwareCommitEngine` so every disk write —
        including repair-loop re-writes — is automatically followed by
        a ``git add`` + ``git commit``.

        Returns
        -------
        (commit_result, test_results, repair_tasks)
            ``repair_tasks`` is non-empty when tests fail and responsible
            workers can be identified.
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

    MAX_REPAIR_ROUNDS = 2

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

        self.log.thinking(
            f"Step 5b — Repair round {round_num}/{self.MAX_REPAIR_ROUNDS}: "
            f"re-running {len(repair_tasks)} worker(s)…"
        )

        # Build a lookup from worker_id → Ticket
        ticket_by_id: dict[str, Ticket] = {t.id: t for t in self.tickets}

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
            )
            # Inject test failure so the worker knows what to fix
            return await worker.run(repair_feedback=rt.failure_output)

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
        await self.delegate()

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

        return self.worker_results

    # ------------------------------------------------------------------
    # Post-commit test runner (used by review gate path)
    # ------------------------------------------------------------------

    async def _run_tests(self) -> tuple[CommitResult | None, list[TestRunResult], list[RepairTask]]:
        """Run test commands and attribute failures — no disk commit.

        Used by the review-gate path where the disk write has already
        happened via ``review_and_commit()``.
        """
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
    # Internal helpers
    # ------------------------------------------------------------------

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
        return response.choices[0].message.content or ""
