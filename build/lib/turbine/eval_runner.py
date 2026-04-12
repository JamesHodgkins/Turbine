"""Eval Harness — Phase 13.

Runs Turbine against a directory of structured benchmark tasks and scores
the results against declarative assertions.

Task format
-----------
Each task is a ``*.json`` file with this shape::

    {
      "id": "task-001",
      "description": "Human-readable description",
      "category": "single-file-edit | multi-file-refactor | bug-fix-with-tests | ...",
      "request": "The natural-language instruction given to Turbine",
      "snapshot": {
        "relative/path.py": "<file contents>",
        ...
      },
      "assertions": [
        {"type": "file_modified",       "file": "relative/path.py"},
        {"type": "content_contains",    "file": "relative/path.py", "pattern": "<regex>"},
        {"type": "content_not_contains","file": "relative/path.py", "pattern": "<regex>"},
        {"type": "valid_python",        "file": "relative/path.py"},
        {"type": "tests_pass",          "command": "python -m pytest test_foo.py -x -q"}
      ],
      "test_commands": []
    }

Assertion types
---------------
file_modified
    The file's content on disk differs from the original snapshot.
content_contains
    The file matches the given regex pattern.
content_not_contains
    The file does not match the given regex pattern.
valid_python
    The file parses without a SyntaxError.
tests_pass
    The shell command exits with return code 0 (run inside the task's temp dir).
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from rich.console import Console
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# Protocol — lets AssertionChecker accept Turbine's Manager without a hard
# import that would create a circular dependency.
# ---------------------------------------------------------------------------

class _ManagerLike(Protocol):
    diagnosis: str
    relevant_files: list[str]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class EvalTask:
    """A single benchmark task loaded from a JSON file."""
    id: str
    description: str
    category: str
    request: str
    snapshot: dict[str, str]          # relative_path → file content
    assertions: list[dict[str, Any]]
    test_commands: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalTask:
        return cls(
            id=data["id"],
            description=data["description"],
            category=data.get("category", "uncategorized"),
            request=data["request"],
            snapshot=data["snapshot"],
            assertions=data.get("assertions", []),
            test_commands=data.get("test_commands", []),
        )

    @classmethod
    def from_file(cls, path: Path) -> EvalTask:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


@dataclass
class AssertionResult:
    """Outcome of a single assertion check."""
    type: str
    passed: bool
    message: str


@dataclass
class EvalResult:
    """Aggregate outcome for one task run."""
    task: EvalTask
    passed: bool
    score: float                           # fraction of assertions that passed (0.0–1.0)
    assertion_results: list[AssertionResult]
    error: str = ""                        # non-empty only when the pipeline itself crashed
    duration_seconds: float = 0.0

    @property
    def score_pct(self) -> str:
        return f"{self.score * 100:.0f}%"


# ---------------------------------------------------------------------------
# Assertion checker
# ---------------------------------------------------------------------------

class AssertionChecker:
    """Evaluates assertion specs against the post-run state of a temp directory."""

    def check(
        self,
        spec: dict[str, Any],
        tmp_dir: Path,
        original_snapshot: dict[str, str],
    ) -> AssertionResult:
        """Dispatch to the appropriate check method."""
        t = spec.get("type", "")
        try:
            if t == "file_modified":
                return self._file_modified(spec, tmp_dir, original_snapshot)
            if t == "content_contains":
                return self._content_contains(spec, tmp_dir)
            if t == "content_not_contains":
                return self._content_not_contains(spec, tmp_dir)
            if t == "valid_python":
                return self._valid_python(spec, tmp_dir)
            if t == "tests_pass":
                return self._tests_pass(spec, tmp_dir)
            return AssertionResult(t, False, f"Unknown assertion type: {t!r}")
        except Exception as exc:  # noqa: BLE001
            return AssertionResult(t, False, f"Checker error: {exc}")

    # ------------------------------------------------------------------
    # Assertion implementations
    # ------------------------------------------------------------------

    def _file_modified(
        self,
        spec: dict[str, Any],
        tmp_dir: Path,
        original: dict[str, str],
    ) -> AssertionResult:
        file = spec["file"]
        abs_path = tmp_dir / file
        if not abs_path.exists():
            return AssertionResult("file_modified", False, f"{file}: does not exist after run")
        current = abs_path.read_text(encoding="utf-8")
        modified = current != original.get(file, "")
        msg = f"{file}: {'was modified' if modified else 'was NOT modified'}"
        return AssertionResult("file_modified", modified, msg)

    def _content_contains(self, spec: dict[str, Any], tmp_dir: Path) -> AssertionResult:
        file, pattern = spec["file"], spec["pattern"]
        abs_path = tmp_dir / file
        if not abs_path.exists():
            return AssertionResult("content_contains", False, f"{file}: does not exist")
        found = bool(re.search(pattern, abs_path.read_text(encoding="utf-8")))
        msg = f"{file}: {'contains' if found else 'missing'} pattern {pattern!r}"
        return AssertionResult("content_contains", found, msg)

    def _content_not_contains(self, spec: dict[str, Any], tmp_dir: Path) -> AssertionResult:
        file, pattern = spec["file"], spec["pattern"]
        abs_path = tmp_dir / file
        if not abs_path.exists():
            return AssertionResult("content_not_contains", False, f"{file}: does not exist")
        found = bool(re.search(pattern, abs_path.read_text(encoding="utf-8")))
        passed = not found
        msg = f"{file}: {'correctly absent' if passed else 'unexpectedly contains'} {pattern!r}"
        return AssertionResult("content_not_contains", passed, msg)

    def _valid_python(self, spec: dict[str, Any], tmp_dir: Path) -> AssertionResult:
        file = spec["file"]
        abs_path = tmp_dir / file
        if not abs_path.exists():
            return AssertionResult("valid_python", False, f"{file}: does not exist")
        try:
            ast.parse(abs_path.read_text(encoding="utf-8"))
            return AssertionResult("valid_python", True, f"{file}: valid Python")
        except SyntaxError as exc:
            return AssertionResult("valid_python", False, f"{file}: SyntaxError — {exc}")

    def _tests_pass(self, spec: dict[str, Any], tmp_dir: Path) -> AssertionResult:
        command = spec.get("command", "python -m pytest -x -q")
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return AssertionResult("tests_pass", False, "Test command timed out after 60 s")
        passed = result.returncode == 0
        detail = (result.stdout + result.stderr).strip()
        # Keep the message short for the report
        brief = detail[:300].replace("\n", " ") if detail else "(no output)"
        msg = f"tests {'passed' if passed else 'FAILED'}: {brief}"
        return AssertionResult("tests_pass", passed, msg)


# ---------------------------------------------------------------------------
# EvalRunner
# ---------------------------------------------------------------------------

class EvalRunner:
    """Loads eval tasks and runs the Turbine pipeline against each one.

    Parameters
    ----------
    eval_dir:
        Directory containing ``*.json`` task files (sorted by filename).
    api_key:
        Mistral API key forwarded to the Manager.
    model:
        Mistral model identifier used for all pipeline calls.
    concurrency:
        Maximum number of tasks to run concurrently (default 1 to avoid
        rate-limit issues).
    """

    def __init__(
        self,
        eval_dir: Path,
        api_key: str,
        model: str = "mistral-large-latest",
        concurrency: int = 1,
    ) -> None:
        self.eval_dir = Path(eval_dir)
        self.api_key = api_key
        self.model = model
        self.concurrency = max(1, concurrency)
        self._checker = AssertionChecker()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_tasks(self) -> list[EvalTask]:
        """Return all ``*.json`` tasks from *eval_dir*, sorted by filename."""
        paths = sorted(self.eval_dir.glob("*.json"))
        if not paths:
            return []
        tasks: list[EvalTask] = []
        for path in paths:
            try:
                tasks.append(EvalTask.from_file(path))
            except Exception as exc:
                raise ValueError(f"Failed to load {path.name}: {exc}") from exc
        return tasks

    async def run(self, tasks: list[EvalTask] | None = None) -> list[EvalResult]:
        """Run *tasks* (or all loaded tasks) and return one result per task."""
        import asyncio
        if tasks is None:
            tasks = self.load_tasks()
        semaphore = asyncio.Semaphore(self.concurrency)

        async def bounded(task: EvalTask) -> EvalResult:
            async with semaphore:
                return await self._run_task(task)

        return list(await asyncio.gather(*(bounded(t) for t in tasks)))

    # ------------------------------------------------------------------
    # Per-task execution
    # ------------------------------------------------------------------

    async def _run_task(self, task: EvalTask) -> EvalResult:
        start = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            try:
                _write_snapshot(task.snapshot, tmp_dir)
                await self._run_pipeline(task, tmp_dir)
                assertion_results = [
                    self._checker.check(spec, tmp_dir, task.snapshot)
                    for spec in task.assertions
                ]
            except Exception as exc:  # noqa: BLE001
                duration = time.monotonic() - start
                return EvalResult(
                    task=task,
                    passed=False,
                    score=0.0,
                    assertion_results=[],
                    error=str(exc),
                    duration_seconds=duration,
                )

        duration = time.monotonic() - start
        n = len(assertion_results)
        score = sum(r.passed for r in assertion_results) / max(n, 1)
        passed = bool(assertion_results) and all(r.passed for r in assertion_results)
        return EvalResult(
            task=task,
            passed=passed,
            score=score,
            assertion_results=assertion_results,
            duration_seconds=duration,
        )

    async def _run_pipeline(self, task: EvalTask, tmp_dir: Path) -> None:
        """Run the full Turbine Manager pipeline against *tmp_dir*."""
        from turbine.manager import Manager
        from turbine.tree_mapper import TreeMapper
        from turbine.ui import TurbineUI

        mapper = TreeMapper(str(tmp_dir))
        tree = mapper.map()
        manager = Manager(
            tree=tree,
            user_request=task.request,
            project_root=str(tmp_dir),
            api_key=self.api_key,
            model=self.model,
            test_commands=task.test_commands,
            dry_run=False,
            ui=TurbineUI(enabled=False),
        )
        await manager.run()


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_eval_report(results: list[EvalResult], console: Console | None = None) -> None:
    """Print a Rich pass/fail table followed by a summary line."""
    c = console or Console()

    table = Table(title="Turbine Eval Results", show_lines=True, expand=False)
    table.add_column("ID",          style="dim",     width=10)
    table.add_column("Category",                     width=22)
    table.add_column("Description",                  width=38)
    table.add_column("Score",   justify="center",    width=7)
    table.add_column("Status",  justify="center",    width=7)
    table.add_column("Time",    justify="right",     width=7)

    for r in results:
        if r.error:
            status = Text("ERROR", style="bold yellow")
        elif r.passed:
            status = Text("PASS",  style="bold green")
        else:
            status = Text("FAIL",  style="bold red")

        score_style = "green" if r.score >= 1.0 else ("yellow" if r.score > 0 else "red")
        score_text  = Text(r.score_pct, style=score_style)

        table.add_row(
            r.task.id,
            r.task.category,
            r.task.description,
            score_text,
            status,
            f"{r.duration_seconds:.1f}s",
        )

    c.print(table)

    total   = len(results)
    passed  = sum(1 for r in results if r.passed)
    avg     = sum(r.score for r in results) / max(total, 1)
    errors  = sum(1 for r in results if r.error)
    c.print(
        f"\n[bold]Results:[/bold] {passed}/{total} passed  |  "
        f"avg score {avg * 100:.0f}%  |  {errors} error(s)"
    )

    # Detail block for failed tasks
    failed = [r for r in results if not r.passed]
    if failed:
        c.print()
        c.rule("[bold red]Failures[/bold red]")
        for r in failed:
            c.print(f"\n[bold]{r.task.id}[/bold] — {r.task.description}")
            if r.error:
                c.print(f"  [yellow]Pipeline error:[/yellow] {r.error}")
            else:
                for ar in r.assertion_results:
                    sym = "[green]✓[/green]" if ar.passed else "[red]✗[/red]"
                    c.print(f"  {sym} [{ar.type}] {ar.message}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_snapshot(snapshot: dict[str, str], tmp_dir: Path) -> None:
    """Write every file in *snapshot* to *tmp_dir*, creating parent dirs."""
    for rel_path, content in snapshot.items():
        abs_path = tmp_dir / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
