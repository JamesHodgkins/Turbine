"""Static Checker — Phase 14.

Runs a fast static analysis tool (mypy, ruff, tsc) immediately after
CommitEngine writes files to disk, but *before* the full test suite.
Errors are parsed, attributed to the workers responsible for the flagged
files, and returned as :class:`~turbine.test_runner.RepairTask` objects so
the existing repair loop can fix them cheaply before the slower tests run.

Supported checkers (auto-detected from project files)
------------------------------------------------------
* **ruff** — detected when ``[tool.ruff`` appears in ``pyproject.toml``.
  Command: ``ruff check .``
* **mypy** — detected when ``[tool.mypy`` appears in ``pyproject.toml`` and
  ruff is *not* present (mypy is slower; prefer ruff as the first-pass tool).
  Command: ``mypy --no-error-summary .``
* **tsc** — detected when ``tsconfig.json`` exists in the project root.
  Command: ``tsc --noEmit``

The checker command can be overridden per-project via the ``static_check``
parameter of :class:`~turbine.manager.Manager`, or disabled entirely by
passing an empty string.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from turbine.logger import TurbineLogger
from turbine.test_runner import RepairTask


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class StaticCheckResult:
    """Outcome of a static-check command."""

    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """Combined stdout + stderr."""
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout)
        if self.stderr.strip():
            parts.append(self.stderr)
        return "\n".join(parts)

    def __str__(self) -> str:
        status = "PASSED" if self.passed else f"FAILED (exit {self.returncode})"
        return f"[static] [{status}] {self.command}"


# ---------------------------------------------------------------------------
# File-path extraction from static-checker output
# ---------------------------------------------------------------------------

# Handles the most common output formats:
#   mypy:   turbine/foo.py:12: error: …
#   ruff:   turbine/foo.py:12:3: E123 …
#   tsc:    src/foo.ts(12,3): error TS1234: …
_STATIC_PATH_RE = re.compile(
    r'([^\s"\'*?<>|,]+\.(?:py|ts|tsx|js|jsx))[:\(]\d+',
    re.IGNORECASE,
)


def _extract_paths(output: str) -> set[str]:
    """Return the set of file paths mentioned in static-checker output."""
    paths: set[str] = set()
    for m in _STATIC_PATH_RE.finditer(output):
        raw = m.group(1)
        if raw:
            paths.add(raw.replace("\\", "/").lstrip("./"))
    return paths


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------


def detect_static_check_command(project_root: Path) -> str | None:
    """Return an appropriate static-check command for the project, or ``None``.

    Detection order (first match wins):

    1. ``pyproject.toml`` with ``[tool.ruff`` → ``ruff check .``
    2. ``pyproject.toml`` with ``[tool.mypy`` → ``mypy --no-error-summary .``
    3. ``tsconfig.json`` at the project root → ``tsc --noEmit``

    Returns ``None`` when no supported configuration is found.
    """
    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            content = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        if "[tool.ruff" in content:
            return "ruff check ."
        if "[tool.mypy" in content:
            return "mypy --no-error-summary ."

    tsconfig = project_root / "tsconfig.json"
    if tsconfig.is_file():
        return "tsc --noEmit"

    return None


# ---------------------------------------------------------------------------
# StaticChecker
# ---------------------------------------------------------------------------


class StaticChecker:
    """Runs a static-analysis command and attributes failures to workers.

    Parameters
    ----------
    project_root:
        Absolute path to the project; the command is executed here.
    command:
        Shell command to run (e.g. ``"ruff check ."``).
        Pass ``None`` to auto-detect from project files.
        Pass ``""`` (empty string) to disable the checker entirely.
    worker_file_map:
        ``{worker_id: [relative_file_path, …]}`` — supplied by the Manager.
    ticket_descriptions:
        ``{worker_id: description}`` — for human-readable repair tasks.
    timeout:
        Per-command timeout in seconds (default 60).
    """

    def __init__(
        self,
        project_root: str | Path,
        command: str | None = None,
        worker_file_map: dict[str, list[str]] | None = None,
        ticket_descriptions: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> None:
        self._root = Path(project_root)
        # Empty string explicitly disables the checker; None triggers auto-detect.
        if command == "":
            self._command: str | None = None
        elif command is None:
            self._command = detect_static_check_command(self._root)
        else:
            self._command = command
        self._worker_file_map = worker_file_map or {}
        self._ticket_descriptions = ticket_descriptions or {}
        self._timeout = timeout
        self.log = TurbineLogger()

    @property
    def has_command(self) -> bool:
        """``True`` if a checker command is configured or was auto-detected."""
        return self._command is not None

    async def run(self) -> tuple[StaticCheckResult | None, list[RepairTask]]:
        """Execute the static-check command and return *(result, repair_tasks)*.

        Returns ``(None, [])`` when no command is configured.
        ``repair_tasks`` is empty when the check passes or when failing
        errors cannot be attributed to any specific worker.
        """
        if not self._command:
            return None, []

        self.log.thinking(f"Static Checker — running: {self._command}")
        result = await self._run_command(self._command)

        if result.passed:
            self.log.action(f"  {result}")
            return result, []

        self.log.error(f"  {result}")
        repair_tasks = self._build_repair_tasks(result)
        if repair_tasks:
            self.log.thinking(
                f"Static Checker — {len(repair_tasks)} worker(s) attributed to errors."
            )
        else:
            self.log.error(
                "Static Checker — errors found but could not be attributed to a "
                "specific worker; proceeding to tests."
            )
        return result, repair_tasks

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_command(self, cmd: str) -> StaticCheckResult:
        """Execute *cmd* as a subprocess and capture output."""
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._root),
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return StaticCheckResult(
                    command=cmd,
                    returncode=1,
                    stdout="",
                    stderr=f"Static check timed out after {self._timeout}s",
                )
            return StaticCheckResult(
                command=cmd,
                returncode=proc.returncode or 0,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )
        except OSError as exc:
            return StaticCheckResult(
                command=cmd,
                returncode=1,
                stdout="",
                stderr=f"Failed to launch static check: {exc}",
            )

    def _build_repair_tasks(self, result: StaticCheckResult) -> list[RepairTask]:
        """Identify which workers are responsible for the static check errors."""
        if not self._worker_file_map:
            return []

        failure_output = result.output
        if not failure_output.strip():
            return []

        failed_paths = _extract_paths(failure_output)
        if not failed_paths:
            return []

        repair_tasks: list[RepairTask] = []
        attributed: set[str] = set()
        for worker_id, files in self._worker_file_map.items():
            norm_files = [f.replace("\\", "/").lstrip("./") for f in files]
            culprit_files = [
                f
                for f, nf in zip(files, norm_files)
                if any(nf in fp or fp in nf for fp in failed_paths)
            ]
            if culprit_files and worker_id not in attributed:
                attributed.add(worker_id)
                repair_tasks.append(
                    RepairTask(
                        worker_id=worker_id,
                        ticket_description=self._ticket_descriptions.get(worker_id, ""),
                        relevant_files=culprit_files,
                        failure_output=failure_output,
                    )
                )

        return repair_tasks
