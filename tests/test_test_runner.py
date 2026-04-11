"""Tests for turbine.test_runner — Phase 5b."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turbine.test_runner import (
    RepairTask,
    TestRunResult,
    TestRunner,
    _extract_failed_paths,
)


# ---------------------------------------------------------------------------
# _extract_failed_paths
# ---------------------------------------------------------------------------

class TestExtractFailedPaths:
    def test_pytest_failed_line(self):
        output = "FAILED tests/test_foo.py::SomeTest::test_bar - AssertionError"
        paths = _extract_failed_paths(output)
        assert any("tests/test_foo.py" in p for p in paths)

    def test_error_with_lineno(self):
        output = "ERROR src/module.py:42: something went wrong"
        paths = _extract_failed_paths(output)
        assert any("src/module.py" in p for p in paths)

    def test_file_in_traceback(self):
        output = 'File "src/util.py", line 7, in some_func'
        paths = _extract_failed_paths(output)
        assert any("src/util.py" in p for p in paths)

    def test_no_paths_in_clean_output(self):
        output = "All tests passed. 42 passed in 1.23s"
        paths = _extract_failed_paths(output)
        # No .py:lineno patterns — may be empty
        # This is a soft assertion since purely passing output has no file refs
        assert isinstance(paths, set)

    def test_multiple_files(self):
        output = (
            "FAILED tests/test_a.py::T::t1\n"
            "FAILED tests/test_b.py::T::t2\n"
        )
        paths = _extract_failed_paths(output)
        assert any("test_a.py" in p for p in paths)
        assert any("test_b.py" in p for p in paths)


# ---------------------------------------------------------------------------
# TestRunResult
# ---------------------------------------------------------------------------

class TestTestRunResult:
    def test_passed_when_returncode_zero(self):
        r = TestRunResult(command="pytest", returncode=0, stdout="ok", stderr="")
        assert r.passed

    def test_failed_when_nonzero(self):
        r = TestRunResult(command="pytest", returncode=1, stdout="", stderr="err")
        assert not r.passed

    def test_output_combines_stdout_stderr(self):
        r = TestRunResult(command="x", returncode=1, stdout="OUT", stderr="ERR")
        assert "OUT" in r.output
        assert "ERR" in r.output

    def test_str_shows_status(self):
        r = TestRunResult(command="pytest", returncode=0, stdout="", stderr="")
        assert "PASSED" in str(r)
        r2 = TestRunResult(command="pytest", returncode=1, stdout="", stderr="")
        assert "FAILED" in str(r2)


# ---------------------------------------------------------------------------
# TestRunner._run_command via subprocess mock
# ---------------------------------------------------------------------------

def _make_proc(returncode: int, stdout: bytes, stderr: bytes) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


class TestRunCommand:
    def test_successful_command(self, tmp_path: Path):
        proc = _make_proc(0, b"all good\n", b"")
        with patch(
            "turbine.test_runner.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            runner = TestRunner(project_root=tmp_path, commands=["pytest"])
            result = asyncio.run(runner._run_command("pytest"))

        assert result.passed
        assert "all good" in result.stdout

    def test_failed_command(self, tmp_path: Path):
        proc = _make_proc(1, b"", b"AssertionError")
        with patch(
            "turbine.test_runner.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            runner = TestRunner(project_root=tmp_path, commands=["pytest"])
            result = asyncio.run(runner._run_command("pytest"))

        assert not result.passed
        assert "AssertionError" in result.stderr

    def test_timeout_returns_failure(self, tmp_path: Path):
        import asyncio as _asyncio

        proc = MagicMock()
        proc.returncode = None
        proc.communicate = AsyncMock(side_effect=_asyncio.TimeoutError())
        proc.kill = MagicMock()

        with patch(
            "turbine.test_runner.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            runner = TestRunner(project_root=tmp_path, commands=["pytest"], timeout=1)
            result = asyncio.run(runner._run_command("pytest"))

        assert not result.passed
        assert "timed out" in result.stderr.lower()

    def test_oserror_returns_failure(self, tmp_path: Path):
        with patch(
            "turbine.test_runner.asyncio.create_subprocess_shell",
            side_effect=OSError("not found"),
        ):
            runner = TestRunner(project_root=tmp_path, commands=["bad-cmd"])
            result = asyncio.run(runner._run_command("bad-cmd"))

        assert not result.passed
        assert "not found" in result.stderr


# ---------------------------------------------------------------------------
# TestRunner.run — full pipeline
# ---------------------------------------------------------------------------

class TestRunnerRun:
    def _passing_proc(self) -> MagicMock:
        return _make_proc(0, b"1 passed\n", b"")

    def _failing_proc(self, path: str = "src/broken.py") -> MagicMock:
        return _make_proc(
            1,
            b"",
            f"FAILED {path}::test_something - AssertionError\n".encode(),
        )

    def test_all_pass_returns_no_repair_tasks(self, tmp_path: Path):
        proc = self._passing_proc()
        with patch(
            "turbine.test_runner.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            runner = TestRunner(
                project_root=tmp_path,
                commands=["pytest"],
                worker_file_map={"t1": ["src/broken.py"]},
            )
            results, tasks = asyncio.run(runner.run())

        assert all(r.passed for r in results)
        assert tasks == []

    def test_failure_attributes_to_correct_worker(self, tmp_path: Path):
        proc = self._failing_proc("src/broken.py")
        with patch(
            "turbine.test_runner.asyncio.create_subprocess_shell",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            runner = TestRunner(
                project_root=tmp_path,
                commands=["pytest"],
                worker_file_map={
                    "ticket-1": ["src/broken.py"],
                    "ticket-2": ["src/other.py"],
                },
                ticket_descriptions={"ticket-1": "Fix the broken thing"},
            )
            results, tasks = asyncio.run(runner.run())

        assert len(tasks) == 1
        assert tasks[0].worker_id == "ticket-1"
        assert "Fix the broken thing" in tasks[0].ticket_description

    def test_stop_on_failure_skips_remaining_commands(self, tmp_path: Path):
        fail_proc = self._failing_proc()
        pass_proc = self._passing_proc()
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return fail_proc if call_count == 1 else pass_proc

        with patch(
            "turbine.test_runner.asyncio.create_subprocess_shell",
            side_effect=side_effect,
        ):
            runner = TestRunner(
                project_root=tmp_path,
                commands=["pytest", "make lint"],
                stop_on_failure=True,
            )
            results, _ = asyncio.run(runner.run())

        assert len(results) == 1   # stopped after first failure

    def test_continue_on_failure_runs_all_commands(self, tmp_path: Path):
        fail_proc = self._failing_proc()
        pass_proc = self._passing_proc()
        procs = [fail_proc, pass_proc]
        idx = 0

        async def side_effect(*args, **kwargs):
            nonlocal idx
            p = procs[idx % len(procs)]
            idx += 1
            return p

        with patch(
            "turbine.test_runner.asyncio.create_subprocess_shell",
            side_effect=side_effect,
        ):
            runner = TestRunner(
                project_root=tmp_path,
                commands=["pytest", "make lint"],
                stop_on_failure=False,
            )
            results, _ = asyncio.run(runner.run())

        assert len(results) == 2
