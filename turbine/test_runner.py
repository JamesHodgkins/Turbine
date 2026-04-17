"""Test Runner — Phase 5b.

Executes build/test commands in the project root, captures stdout and stderr,
and maps failures back to the specific workers whose diffs touched the
failing files.

Detection heuristics
--------------------
Failure messages commonly look like:

    FAILED tests/test_foo.py::SomeTest::test_bar
    ERROR  src/module.py:42: ...
    AssertionError: ...  src/util.py:7

The runner extracts file paths from these lines using a simple regex and
cross-references them against the ``worker_id → files`` mapping to decide
which workers should receive repair feedback.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from turbine.logger import TurbineLogger


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TestRunResult:
    """Outcome of a single test-command execution."""
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
        return f"[{status}] {self.command}"


@dataclass
class RepairTask:
    """A worker that needs to repair its diff because tests failed."""
    worker_id: str
    ticket_description: str
    relevant_files: list[str]
    failure_output: str   # the test output lines that mention its files


# ---------------------------------------------------------------------------
# File-path extraction from test output
# ---------------------------------------------------------------------------

# Matches things like:
#   FAILED tests/test_foo.py::bar
#   ERROR  src/a.py:12
#   File "src/b.py", line 5
#   ../src/c.py:99:
_PATH_RE = re.compile(
    r'(?:FAILED|ERROR|File)\s+"?([^\s"\']+\.py)'   # explicit keyword prefix
    r'|([^\s"\']+\.py):\d+'                          # path:lineno pattern
    r'|([^\s"\']+\.[a-z]{1,4}):\d+',                # generic file:lineno
    re.IGNORECASE,
)


def _extract_failed_paths(output: str) -> set[str]:
    """Return the set of file paths mentioned in test failure output."""
    paths: set[str] = set()
    for m in _PATH_RE.finditer(output):
        raw = m.group(1) or m.group(2) or m.group(3)
        if raw:
            # Normalise separators
            paths.add(raw.replace("\\", "/").lstrip("./"))
    return paths


# ---------------------------------------------------------------------------
# TestRunner
# ---------------------------------------------------------------------------

class TestRunner:
    """Runs test commands and maps failures to responsible workers.

    Parameters
    ----------
    project_root:
        Absolute path to the project; commands are executed here.
    commands:
        Ordered list of shell commands to run (e.g. ``["pytest", "make lint"]``).
        Execution stops at the first failure unless ``stop_on_failure=False``.
    worker_file_map:
        ``{worker_id: [relative_file_path, ...]}`` — produced by the Manager
        from the Worker results.
    ticket_descriptions:
        ``{worker_id: description}`` — for building human-readable repair tasks.
    stop_on_failure:
        If ``True`` (default), stop running commands after the first failure.
    timeout:
        Per-command timeout in seconds (default 120).
    """

    def __init__(
        self,
        project_root: str | Path,
        commands: list[str],
        worker_file_map: dict[str, list[str]] | None = None,
        ticket_descriptions: dict[str, str] | None = None,
        stop_on_failure: bool = True,
        timeout: int = 120,
    ) -> None:
        self._root = Path(project_root)
        self._commands = commands
        self._worker_file_map = worker_file_map or {}
        self._ticket_descriptions = ticket_descriptions or {}
        self._stop_on_failure = stop_on_failure
        self._timeout = timeout
        self.log = TurbineLogger()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> tuple[list[TestRunResult], list[RepairTask]]:
        """Execute all commands and return (results, repair_tasks).

        ``repair_tasks`` is empty when all tests pass.
        """
        results: list[TestRunResult] = []

        for cmd in self._commands:
            self.log.thinking(f"Test Runner — running: {cmd}")
            result = await self._run_command(cmd)
            results.append(result)

            if result.passed:
                self.log.action(f"  {result}")
            else:
                self.log.error(f"  {result}")
                if self._stop_on_failure:
                    break

        repair_tasks = self._build_repair_tasks(results)
        if repair_tasks:
            self.log.thinking(
                f"Test Runner — {len(repair_tasks)} worker(s) need repair."
            )
        else:
            self.log.action("Test Runner — all commands passed.")

        return results, repair_tasks

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_command(self, cmd: str) -> TestRunResult:
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
                return TestRunResult(
                    command=cmd,
                    returncode=1,
                    stdout="",
                    stderr=f"Command timed out after {self._timeout}s",
                )

            return TestRunResult(
                command=cmd,
                returncode=proc.returncode or 0,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )
        except (asyncio.TimeoutError, TimeoutError):
            # Raised when create_subprocess_shell itself times out (rare, but
            # possible when the mock raises TimeoutError during communicate).
            return TestRunResult(
                command=cmd,
                returncode=1,
                stdout="",
                stderr=f"Command timed out after {self._timeout}s",
            )
        except OSError as exc:
            return TestRunResult(
                command=cmd,
                returncode=1,
                stdout="",
                stderr=f"Failed to launch command: {exc}",
            )

    def _build_repair_tasks(self, results: list[TestRunResult]) -> list[RepairTask]:
        """Identify which workers are responsible for the failures.

        Phase 12 — callee attribution
        ------------------------------
        Beyond the direct match (a worker's file appears in the traceback), we
        also attribute to workers whose files are *imported by* any file in the
        traceback.  When worker A changes a shared interface and worker B's test
        file calls into it, the traceback mentions B's test file — but the actual
        bug lives in A's callee file.  We detect this by scanning each failing
        file for ``import``/``from … import`` statements and matching the
        imported module names against worker file paths.
        """
        if not self._worker_file_map:
            return []

        # Collect all output from failed runs
        failure_output = "\n".join(r.output for r in results if not r.passed)
        if not failure_output.strip():
            return []

        failed_paths = _extract_failed_paths(failure_output)
        if not failed_paths:
            return []

        # Phase 12: expand failed_paths to include files imported by any caller
        # in the traceback.  This catches "callee changed the interface" failures.
        callee_paths = self._expand_callee_paths(failed_paths)
        all_failure_paths = failed_paths | callee_paths

        repair_tasks: list[RepairTask] = []
        attributed: set[str] = set()  # avoid duplicate repair tasks
        for worker_id, files in self._worker_file_map.items():
            # Normalise worker file paths for comparison
            norm_files = [f.replace("\\", "/").lstrip("./") for f in files]
            # Direct match: worker's file is mentioned in the traceback
            direct = [
                f for f, nf in zip(files, norm_files)
                if any(nf in fp or fp in nf for fp in failed_paths)
            ]
            # Callee match: worker's file is imported by a file in the traceback
            callee = [
                f for f, nf in zip(files, norm_files)
                if f not in direct and any(nf in fp or fp in nf for fp in callee_paths)
            ]
            culprit_files = direct + callee
            if culprit_files and worker_id not in attributed:
                attributed.add(worker_id)
                repair_tasks.append(RepairTask(
                    worker_id=worker_id,
                    ticket_description=self._ticket_descriptions.get(worker_id, ""),
                    relevant_files=culprit_files,
                    failure_output=failure_output,
                ))

        return repair_tasks

    def _expand_callee_paths(self, failed_paths: set[str]) -> set[str]:
        """Return file paths imported by any file in *failed_paths*.

        Scans each on-disk file that matches a path in *failed_paths* for
        ``import X`` / ``from X import Y`` statements and converts the module
        names to candidate relative paths (``x/y.py`` style).  Only returns
        paths that actually exist under the project root, so speculative matches
        don't pollute the attribution.
        """
        import re as _re
        # Capture only the first dotted module name on each import line.
        # We restrict the match to a single line by using [^\n] so the
        # group does not greedily consume subsequent blank lines.
        _import_re = _re.compile(
            r"^[ \t]*(?:from[ \t]+([\w.]+)[ \t]+import|import[ \t]+([\w.][^\n,)]*?))"
            r"(?:[ \t]*(?:,|\n|$))",
            _re.MULTILINE,
        )
        callee_paths: set[str] = set()

        for rel_path in failed_paths:
            # Find a real file on disk that corresponds to this traceback path
            abs_path = self._root / rel_path
            if not abs_path.is_file():
                # Try stripping a leading directory component in case the
                # traceback path has extra prefix (e.g. "tests/../src/foo.py")
                continue
            try:
                source = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for m in _import_re.finditer(source):
                module = (m.group(1) or m.group(2) or "").strip().split(",")[0].strip()
                if not module:
                    continue
                # Convert "a.b.c" → candidate paths "a/b/c.py" and "a/b/c/__init__.py"
                parts = module.replace(".", "/")
                for candidate in (f"{parts}.py", f"{parts}/__init__.py"):
                    if (self._root / candidate).is_file():
                        callee_paths.add(candidate)

        return callee_paths
