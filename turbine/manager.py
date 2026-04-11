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
from typing import Any

from mistralai.client import Mistral

from turbine.commit_engine import CommitEngine, CommitResult
from turbine.logger import TurbineLogger
from turbine.test_runner import RepairTask, TestRunResult, TestRunner
from turbine.token_manager import TokenManager
from turbine.tree_mapper import ProjectTree
from turbine.ui import PipelineStep, TurbineUI
from turbine.vfs import ConflictDetector, VirtualFileSystem


# ---------------------------------------------------------------------------
# Ticket — a single unit of work for one worker
# ---------------------------------------------------------------------------

@dataclass
class Ticket:
    """A decomposed sub-task produced by Step 3 investigation."""
    id: str                          # e.g. "ticket-1"
    description: str                 # human-readable goal
    relevant_files: list[str]        # relative paths the worker may touch
    context: str = ""                # extra notes from the Manager for the worker

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "relevant_files": self.relevant_files,
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
Include files that may need to be read or modified. \
Return nothing but the JSON array — no prose, no markdown fences."""

INVESTIGATE_SYSTEM = """\
You are Turbine's Investigator. Given a user request and the contents of relevant \
files, produce a structured diagnosis and task decomposition. \
Return ONLY a JSON object with this exact shape:
{
  "diagnosis": "<brief description of the issue or goal>",
  "tickets": [
    {
      "id": "ticket-1",
      "description": "<what this sub-task achieves>",
      "relevant_files": ["<relative path>", ...],
      "context": "<any extra notes for the worker>"
    }
  ]
}
Return nothing but the JSON object — no prose, no markdown fences."""


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
        ui: TurbineUI | None = None,
    ) -> None:
        self.tree = tree
        self.user_request = user_request
        self.project_root = Path(project_root)
        self.model = model
        self.max_workers = max_workers
        self.test_commands = test_commands or []
        self.dry_run = dry_run
        self.ui = ui or TurbineUI(enabled=False)   # no-op by default
        self.log = TurbineLogger()
        self.token_manager = TokenManager(model)
        self._client = Mistral(api_key=api_key or os.environ["MISTRAL_API_KEY"])
        self.vfs = VirtualFileSystem()

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
                    context=t.get("context", ""),
                )
                for i, t in enumerate(raw_tickets)
            ]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            self.log.error(f"Investigate: could not parse LLM response — {exc}")
            self.tickets = []

        self.log.action(
            f"Investigate complete — diagnosis: \"{self.diagnosis}\" | "
            f"{len(self.tickets)} ticket(s) created."
        )
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

        async def bounded(ticket: Ticket) -> WorkerResult:
            async with semaphore:
                return await self._run_worker(ticket)

        results = await asyncio.gather(*(bounded(t) for t in self.tickets))
        self.worker_results = list(results)

        successes = sum(1 for r in self.worker_results if r.success)
        self.log.action(
            f"Delegate complete — {successes}/{len(self.worker_results)} worker(s) succeeded."
        )
        return self.worker_results

    # ------------------------------------------------------------------
    # Worker (Phase 4)
    # ------------------------------------------------------------------

    async def _run_worker(self, ticket: Ticket) -> WorkerResult:
        """Spin up a Worker for *ticket* and return its result."""
        from turbine.worker import Worker  # local import avoids circular dependency

        worker = Worker(
            ticket=ticket,
            vfs=self.vfs,
            client=self._client,
            model=self.model,
            token_manager=self.token_manager,
            ui=self.ui,
        )
        return await worker.run()

    # ------------------------------------------------------------------
    # Step 5: Commit & Verify
    # ------------------------------------------------------------------

    async def verify(self) -> tuple[CommitResult, list[TestRunResult], list[RepairTask]]:
        """Commit VFS to disk and run tests.

        Returns
        -------
        (commit_result, test_results, repair_tasks)
            ``repair_tasks`` is non-empty when tests fail and responsible
            workers can be identified.
        """
        self.ui.on_step(PipelineStep.COMMIT, "writing VFS to disk…")
        self.log.thinking("Step 5 — Commit & Verify: writing VFS to disk…")

        engine = CommitEngine(
            vfs=self.vfs,
            project_root=self.project_root,
            dry_run=self.dry_run,
            original_snapshots=self._original_snapshots,
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
    # Full pipeline: Steps 2 → 3 → 4 → 5
    # ------------------------------------------------------------------

    async def run(self) -> list[WorkerResult]:
        """Execute Steps 2, 3, 4, and (if configured) 5 in sequence."""
        self.ui.on_step(PipelineStep.DISCOVER, "mapping project tree…")
        await self.preprocess()
        await self.investigate()
        await self.delegate()
        if self.test_commands or not self.dry_run:
            await self.verify()
        successes = sum(1 for r in self.worker_results if r.success)
        self.ui.on_done(
            f"{successes}/{len(self.worker_results)} worker(s) succeeded"
            if self.worker_results else "pipeline complete"
        )
        return self.worker_results

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
