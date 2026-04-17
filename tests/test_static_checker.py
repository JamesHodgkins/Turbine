"""Tests for turbine.static_checker — Phase 14."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turbine.static_checker import (
    StaticCheckResult,
    StaticChecker,
    _extract_paths,
    detect_static_check_command,
)
from turbine.test_runner import RepairTask


# ---------------------------------------------------------------------------
# _extract_paths
# ---------------------------------------------------------------------------


class TestExtractPaths:
    def test_mypy_format(self):
        output = "turbine/foo.py:12: error: Argument 1 to 'f' has incompatible type"
        paths = _extract_paths(output)
        assert any("turbine/foo.py" in p for p in paths)

    def test_ruff_format(self):
        output = "turbine/bar.py:34:5: E501 Line too long"
        paths = _extract_paths(output)
        assert any("turbine/bar.py" in p for p in paths)

    def test_tsc_format(self):
        output = "src/index.ts(12,3): error TS2345: Argument of type 'string'"
        paths = _extract_paths(output)
        assert any("src/index.ts" in p for p in paths)

    def test_multiple_files(self):
        output = (
            "turbine/a.py:1: error: Module not found\n"
            "turbine/b.py:5: error: Incompatible return type\n"
        )
        paths = _extract_paths(output)
        assert any("turbine/a.py" in p for p in paths)
        assert any("turbine/b.py" in p for p in paths)

    def test_no_paths_in_clean_output(self):
        output = "All checks passed."
        paths = _extract_paths(output)
        assert paths == set()

    def test_normalises_backslashes(self):
        output = "turbine\\foo.py:1: error: test"
        paths = _extract_paths(output)
        assert any("turbine/foo.py" in p for p in paths)

    def test_strips_leading_dot_slash(self):
        output = "./turbine/foo.py:1: error: test"
        paths = _extract_paths(output)
        assert any("turbine/foo.py" in p for p in paths)


# ---------------------------------------------------------------------------
# detect_static_check_command
# ---------------------------------------------------------------------------


class TestDetectStaticCheckCommand:
    def test_ruff_config_detected(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.ruff]\nline-length = 88\n", encoding="utf-8"
        )
        cmd = detect_static_check_command(tmp_path)
        assert cmd == "ruff check ."

    def test_mypy_config_detected(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.mypy]\nstrict = true\n", encoding="utf-8"
        )
        cmd = detect_static_check_command(tmp_path)
        assert cmd == "mypy --no-error-summary ."

    def test_ruff_takes_priority_over_mypy(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.ruff]\n[tool.mypy]\n", encoding="utf-8"
        )
        cmd = detect_static_check_command(tmp_path)
        assert cmd == "ruff check ."

    def test_tsconfig_detected(self, tmp_path: Path):
        (tmp_path / "tsconfig.json").write_text('{"compilerOptions": {}}', encoding="utf-8")
        cmd = detect_static_check_command(tmp_path)
        assert cmd == "tsc --noEmit"

    def test_no_config_returns_none(self, tmp_path: Path):
        cmd = detect_static_check_command(tmp_path)
        assert cmd is None

    def test_pyproject_without_tool_sections(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\nrequires = ['setuptools']\n", encoding="utf-8"
        )
        cmd = detect_static_check_command(tmp_path)
        assert cmd is None


# ---------------------------------------------------------------------------
# StaticCheckResult
# ---------------------------------------------------------------------------


class TestStaticCheckResult:
    def test_passed_when_returncode_zero(self):
        r = StaticCheckResult(command="ruff check .", returncode=0, stdout="", stderr="")
        assert r.passed

    def test_failed_when_nonzero(self):
        r = StaticCheckResult(command="ruff check .", returncode=1, stdout="err", stderr="")
        assert not r.passed

    def test_output_combines_stdout_stderr(self):
        r = StaticCheckResult(command="x", returncode=1, stdout="STDOUT", stderr="STDERR")
        assert "STDOUT" in r.output
        assert "STDERR" in r.output

    def test_str_shows_passed(self):
        r = StaticCheckResult(command="ruff check .", returncode=0, stdout="", stderr="")
        assert "PASSED" in str(r)

    def test_str_shows_failed_with_exit_code(self):
        r = StaticCheckResult(command="ruff check .", returncode=1, stdout="", stderr="")
        assert "FAILED" in str(r)
        assert "exit 1" in str(r)


# ---------------------------------------------------------------------------
# StaticChecker — has_command / auto-detect
# ---------------------------------------------------------------------------


class TestStaticCheckerHasCommand:
    def test_explicit_command_used_as_is(self, tmp_path: Path):
        checker = StaticChecker(project_root=tmp_path, command="mypy .")
        assert checker.has_command
        assert checker._command == "mypy ."

    def test_empty_string_disables_checker(self, tmp_path: Path):
        checker = StaticChecker(project_root=tmp_path, command="")
        assert not checker.has_command

    def test_none_triggers_auto_detect(self, tmp_path: Path):
        # No config files → auto-detect returns None → has_command False
        checker = StaticChecker(project_root=tmp_path, command=None)
        assert not checker.has_command

    def test_none_with_ruff_config_auto_detects(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
        checker = StaticChecker(project_root=tmp_path, command=None)
        assert checker.has_command
        assert checker._command == "ruff check ."


# ---------------------------------------------------------------------------
# StaticChecker.run()
# ---------------------------------------------------------------------------


class TestStaticCheckerRun:
    def _make_checker(
        self,
        tmp_path: Path,
        command: str,
        worker_file_map: dict | None = None,
        ticket_descriptions: dict | None = None,
    ) -> StaticChecker:
        return StaticChecker(
            project_root=tmp_path,
            command=command,
            worker_file_map=worker_file_map or {},
            ticket_descriptions=ticket_descriptions or {},
        )

    def _mock_proc(self, returncode: int, stdout: str, stderr: str = "") -> MagicMock:
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(
            return_value=(stdout.encode(), stderr.encode())
        )
        proc.kill = MagicMock()
        return proc

    @pytest.mark.asyncio
    async def test_returns_none_when_no_command(self, tmp_path: Path):
        checker = StaticChecker(project_root=tmp_path, command="")
        result, tasks = await checker.run()
        assert result is None
        assert tasks == []

    @pytest.mark.asyncio
    async def test_passing_check_returns_empty_repair_tasks(self, tmp_path: Path):
        checker = self._make_checker(tmp_path, "ruff check .")
        proc = self._mock_proc(0, "All checks passed.")
        with patch("asyncio.create_subprocess_shell", return_value=proc):
            result, tasks = await checker.run()
        assert result is not None
        assert result.passed
        assert tasks == []

    @pytest.mark.asyncio
    async def test_failing_check_attributes_to_worker(self, tmp_path: Path):
        checker = self._make_checker(
            tmp_path,
            "ruff check .",
            worker_file_map={"ticket-1": ["turbine/foo.py"]},
            ticket_descriptions={"ticket-1": "fix foo"},
        )
        output = "turbine/foo.py:5:1: E501 Line too long (120 > 88 characters)\n"
        proc = self._mock_proc(1, output)
        with patch("asyncio.create_subprocess_shell", return_value=proc):
            result, tasks = await checker.run()
        assert result is not None
        assert not result.passed
        assert len(tasks) == 1
        task = tasks[0]
        assert task.worker_id == "ticket-1"
        assert task.ticket_description == "fix foo"
        assert "turbine/foo.py" in task.relevant_files
        assert "turbine/foo.py" in task.failure_output

    @pytest.mark.asyncio
    async def test_failing_check_no_attribution_returns_empty_tasks(self, tmp_path: Path):
        checker = self._make_checker(
            tmp_path,
            "ruff check .",
            worker_file_map={"ticket-1": ["turbine/other.py"]},
        )
        output = "turbine/foo.py:5:1: E501 Line too long\n"
        proc = self._mock_proc(1, output)
        with patch("asyncio.create_subprocess_shell", return_value=proc):
            result, tasks = await checker.run()
        assert result is not None
        assert not result.passed
        # foo.py was not in ticket-1's files → no attribution
        assert tasks == []

    @pytest.mark.asyncio
    async def test_timeout_returns_failed_result(self, tmp_path: Path):
        checker = StaticChecker(project_root=tmp_path, command="ruff check .", timeout=1)
        proc = MagicMock()
        proc.returncode = None
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))

        async def _fake_wait_for(coro, timeout):
            raise asyncio.TimeoutError()

        with patch("asyncio.create_subprocess_shell", return_value=proc), \
             patch("asyncio.wait_for", side_effect=_fake_wait_for):
            result, tasks = await checker.run()
        assert result is not None
        assert not result.passed
        assert "timed out" in result.stderr

    @pytest.mark.asyncio
    async def test_os_error_returns_failed_result(self, tmp_path: Path):
        checker = self._make_checker(tmp_path, "nonexistent_tool .")
        with patch(
            "asyncio.create_subprocess_shell",
            side_effect=OSError("No such file"),
        ):
            result, tasks = await checker.run()
        assert result is not None
        assert not result.passed
        assert "Failed to launch" in result.stderr

    @pytest.mark.asyncio
    async def test_multiple_workers_attributed_correctly(self, tmp_path: Path):
        checker = self._make_checker(
            tmp_path,
            "mypy --no-error-summary .",
            worker_file_map={
                "ticket-1": ["turbine/a.py"],
                "ticket-2": ["turbine/b.py"],
            },
        )
        output = (
            "turbine/a.py:1: error: Module not found\n"
            "turbine/b.py:5: error: Incompatible return type\n"
        )
        proc = self._mock_proc(1, output)
        with patch("asyncio.create_subprocess_shell", return_value=proc):
            result, tasks = await checker.run()
        worker_ids = {t.worker_id for t in tasks}
        assert "ticket-1" in worker_ids
        assert "ticket-2" in worker_ids
