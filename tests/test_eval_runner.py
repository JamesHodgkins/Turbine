"""Tests for turbine.eval_runner — Phase 13.

No real LLM calls are made.  EvalRunner._run_pipeline is patched to write
controlled file content so the assertion checker can be exercised end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from turbine.eval_runner import (
    AssertionChecker,
    BaselineRegression,
    EvalResult,
    EvalRunner,
    EvalTask,
    _write_snapshot,
    compare_to_baseline,
    load_baseline,
    print_baseline_report,
    print_eval_report,
    save_baseline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task(**kwargs: Any) -> EvalTask:
    defaults: dict[str, Any] = {
        "id": "task-test",
        "description": "Test task",
        "category": "single-file-edit",
        "request": "Do something",
        "snapshot": {"foo.py": "x = 1\n"},
        "assertions": [],
        "test_commands": [],
    }
    defaults.update(kwargs)
    return EvalTask(**defaults)


def _write(tmp_path: Path, rel: str, content: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# EvalTask
# ---------------------------------------------------------------------------

class TestEvalTask:
    def test_from_dict_minimal(self):
        data = {
            "id": "t-1",
            "description": "desc",
            "request": "do it",
            "snapshot": {"a.py": "x=1\n"},
        }
        task = EvalTask.from_dict(data)
        assert task.id == "t-1"
        assert task.category == "uncategorized"
        assert task.assertions == []
        assert task.test_commands == []

    def test_from_dict_full(self):
        data = {
            "id": "t-2",
            "description": "d",
            "category": "multi-file-refactor",
            "request": "r",
            "snapshot": {"a.py": ""},
            "assertions": [{"type": "valid_python", "file": "a.py"}],
            "test_commands": ["pytest"],
        }
        task = EvalTask.from_dict(data)
        assert task.category == "multi-file-refactor"
        assert len(task.assertions) == 1
        assert task.test_commands == ["pytest"]

    def test_from_file(self, tmp_path: Path):
        data = {
            "id": "t-3",
            "description": "file load",
            "request": "x",
            "snapshot": {"b.py": "y=2\n"},
        }
        f = tmp_path / "task.json"
        f.write_text(json.dumps(data))
        task = EvalTask.from_file(f)
        assert task.id == "t-3"

    def test_from_file_bad_json_raises(self, tmp_path: Path):
        f = tmp_path / "bad.json"
        f.write_text("not json{{{")
        with pytest.raises(Exception):
            EvalTask.from_file(f)


# ---------------------------------------------------------------------------
# EvalResult
# ---------------------------------------------------------------------------

class TestEvalResult:
    def test_score_pct_full(self):
        task = _task()
        r = EvalResult(task=task, passed=True, score=1.0, assertion_results=[])
        assert r.score_pct == "100%"

    def test_score_pct_partial(self):
        task = _task()
        r = EvalResult(task=task, passed=False, score=0.5, assertion_results=[])
        assert r.score_pct == "50%"

    def test_score_pct_zero(self):
        task = _task()
        r = EvalResult(task=task, passed=False, score=0.0, assertion_results=[])
        assert r.score_pct == "0%"


# ---------------------------------------------------------------------------
# _write_snapshot
# ---------------------------------------------------------------------------

class TestWriteSnapshot:
    def test_creates_files(self, tmp_path: Path):
        _write_snapshot({"a.py": "x=1\n", "sub/b.py": "y=2\n"}, tmp_path)
        assert (tmp_path / "a.py").read_text() == "x=1\n"
        assert (tmp_path / "sub" / "b.py").read_text() == "y=2\n"

    def test_creates_parent_dirs(self, tmp_path: Path):
        _write_snapshot({"deep/nested/c.py": "z=3\n"}, tmp_path)
        assert (tmp_path / "deep" / "nested" / "c.py").exists()


# ---------------------------------------------------------------------------
# AssertionChecker — file_modified
# ---------------------------------------------------------------------------

class TestFileModified:
    def test_passes_when_content_changed(self, tmp_path: Path):
        original = {"foo.py": "x = 1\n"}
        _write(tmp_path, "foo.py", "x = 2\n")   # different from original
        checker = AssertionChecker()
        r = checker.check({"type": "file_modified", "file": "foo.py"}, tmp_path, original)
        assert r.passed

    def test_fails_when_content_unchanged(self, tmp_path: Path):
        original = {"foo.py": "x = 1\n"}
        _write(tmp_path, "foo.py", "x = 1\n")
        checker = AssertionChecker()
        r = checker.check({"type": "file_modified", "file": "foo.py"}, tmp_path, original)
        assert not r.passed

    def test_fails_when_file_missing(self, tmp_path: Path):
        checker = AssertionChecker()
        r = checker.check({"type": "file_modified", "file": "ghost.py"}, tmp_path, {})
        assert not r.passed
        assert "does not exist" in r.message


# ---------------------------------------------------------------------------
# AssertionChecker — content_contains
# ---------------------------------------------------------------------------

class TestContentContains:
    def test_passes_when_pattern_found(self, tmp_path: Path):
        _write(tmp_path, "a.py", "def foo(): pass\n")
        r = AssertionChecker().check(
            {"type": "content_contains", "file": "a.py", "pattern": "def foo"},
            tmp_path, {},
        )
        assert r.passed

    def test_fails_when_pattern_absent(self, tmp_path: Path):
        _write(tmp_path, "a.py", "x = 1\n")
        r = AssertionChecker().check(
            {"type": "content_contains", "file": "a.py", "pattern": "def foo"},
            tmp_path, {},
        )
        assert not r.passed

    def test_uses_regex(self, tmp_path: Path):
        _write(tmp_path, "a.py", "PI = 3.14159\n")
        r = AssertionChecker().check(
            {"type": "content_contains", "file": "a.py", "pattern": "PI\\s*="},
            tmp_path, {},
        )
        assert r.passed

    def test_fails_when_file_missing(self, tmp_path: Path):
        r = AssertionChecker().check(
            {"type": "content_contains", "file": "nope.py", "pattern": "x"},
            tmp_path, {},
        )
        assert not r.passed


# ---------------------------------------------------------------------------
# AssertionChecker — content_not_contains
# ---------------------------------------------------------------------------

class TestContentNotContains:
    def test_passes_when_pattern_absent(self, tmp_path: Path):
        _write(tmp_path, "a.py", "count = 0\n")
        r = AssertionChecker().check(
            {"type": "content_not_contains", "file": "a.py", "pattern": "global x"},
            tmp_path, {},
        )
        assert r.passed

    def test_fails_when_pattern_present(self, tmp_path: Path):
        _write(tmp_path, "a.py", "global x\nx = 0\n")
        r = AssertionChecker().check(
            {"type": "content_not_contains", "file": "a.py", "pattern": "global x"},
            tmp_path, {},
        )
        assert not r.passed


# ---------------------------------------------------------------------------
# AssertionChecker — valid_python
# ---------------------------------------------------------------------------

class TestValidPython:
    def test_passes_for_valid_file(self, tmp_path: Path):
        _write(tmp_path, "ok.py", "def f(x):\n    return x + 1\n")
        r = AssertionChecker().check(
            {"type": "valid_python", "file": "ok.py"}, tmp_path, {}
        )
        assert r.passed

    def test_fails_for_syntax_error(self, tmp_path: Path):
        _write(tmp_path, "bad.py", "def f(\n")
        r = AssertionChecker().check(
            {"type": "valid_python", "file": "bad.py"}, tmp_path, {}
        )
        assert not r.passed
        assert "SyntaxError" in r.message

    def test_fails_for_missing_file(self, tmp_path: Path):
        r = AssertionChecker().check(
            {"type": "valid_python", "file": "ghost.py"}, tmp_path, {}
        )
        assert not r.passed


# ---------------------------------------------------------------------------
# AssertionChecker — tests_pass
# ---------------------------------------------------------------------------

class TestTestsPass:
    def test_passes_for_passing_command(self, tmp_path: Path):
        r = AssertionChecker().check(
            {"type": "tests_pass", "command": "python -c \"assert 1 == 1\""},
            tmp_path, {},
        )
        assert r.passed

    def test_fails_for_failing_command(self, tmp_path: Path):
        r = AssertionChecker().check(
            {"type": "tests_pass", "command": "python -c \"assert 1 == 2\""},
            tmp_path, {},
        )
        assert not r.passed

    def test_unknown_type_returns_failure(self, tmp_path: Path):
        r = AssertionChecker().check(
            {"type": "unknown_assertion_xyz"}, tmp_path, {}
        )
        assert not r.passed
        assert "Unknown" in r.message


# ---------------------------------------------------------------------------
# AssertionChecker — tests_pass with real pytest (integration)
# ---------------------------------------------------------------------------

class TestTestsPassPytest:
    def test_pytest_passes(self, tmp_path: Path):
        _write(tmp_path, "test_ok.py", "def test_ok():\n    assert 1 + 1 == 2\n")
        r = AssertionChecker().check(
            {"type": "tests_pass", "command": "python -m pytest test_ok.py -x -q --tb=short"},
            tmp_path, {},
        )
        assert r.passed

    def test_pytest_fails(self, tmp_path: Path):
        _write(tmp_path, "test_bad.py", "def test_bad():\n    assert 1 == 2\n")
        r = AssertionChecker().check(
            {"type": "tests_pass", "command": "python -m pytest test_bad.py -x -q --tb=short"},
            tmp_path, {},
        )
        assert not r.passed


# ---------------------------------------------------------------------------
# EvalRunner — load_tasks
# ---------------------------------------------------------------------------

class TestLoadTasks:
    def test_loads_sorted_by_filename(self, tmp_path: Path):
        for name, id_ in [("002.json", "t-2"), ("001.json", "t-1")]:
            data = {"id": id_, "description": "d", "request": "r", "snapshot": {}}
            (tmp_path / name).write_text(json.dumps(data))
        runner = EvalRunner(tmp_path, api_key="fake")
        tasks = runner.load_tasks()
        assert [t.id for t in tasks] == ["t-1", "t-2"]

    def test_empty_dir_returns_empty(self, tmp_path: Path):
        runner = EvalRunner(tmp_path, api_key="fake")
        assert runner.load_tasks() == []

    def test_bad_json_raises_value_error(self, tmp_path: Path):
        (tmp_path / "bad.json").write_text("{{invalid")
        runner = EvalRunner(tmp_path, api_key="fake")
        with pytest.raises(ValueError, match="bad.json"):
            runner.load_tasks()

    def test_all_real_tasks_load(self):
        """Smoke-test that every task in evals/tasks/ is valid JSON."""
        tasks_dir = Path(__file__).parent.parent / "evals" / "tasks"
        if not tasks_dir.exists():
            pytest.skip("evals/tasks directory not found")
        runner = EvalRunner(tasks_dir, api_key="fake")
        tasks = runner.load_tasks()
        # Phase 20 adds 20 new tasks (021-040); expect at least 40
        assert len(tasks) >= 40
        ids = [t.id for t in tasks]
        assert "task-001" in ids
        assert "task-020" in ids


# ---------------------------------------------------------------------------
# EvalRunner.run — patched pipeline
# ---------------------------------------------------------------------------

class TestEvalRunnerRun:
    """Run the EvalRunner end-to-end with a patched pipeline."""

    @pytest.mark.asyncio
    async def test_all_assertions_pass(self, tmp_path: Path):
        """Pipeline writes the 'fixed' file; all assertions should pass."""
        task = _task(
            snapshot={"foo.py": "x = 1\n"},
            assertions=[
                {"type": "file_modified",    "file": "foo.py"},
                {"type": "content_contains", "file": "foo.py", "pattern": "count"},
                {"type": "valid_python",     "file": "foo.py"},
            ],
        )

        async def fake_pipeline(self_inner, t, tmp_dir):  # noqa: ARG001
            (tmp_dir / "foo.py").write_text("count = 1\n")

        runner = EvalRunner(tmp_path, api_key="fake")
        with patch.object(EvalRunner, "_run_pipeline", new=fake_pipeline):
            results = await runner.run([task])

        assert len(results) == 1
        r = results[0]
        assert r.passed
        assert r.score == 1.0
        assert r.error == ""

    @pytest.mark.asyncio
    async def test_partial_score_on_failed_assertion(self, tmp_path: Path):
        task = _task(
            snapshot={"foo.py": "x = 1\n"},
            assertions=[
                {"type": "file_modified",        "file": "foo.py"},   # passes (content changed)
                {"type": "content_not_contains", "file": "foo.py", "pattern": "count"},  # fails
            ],
        )

        async def fake_pipeline(self_inner, t, tmp_dir):  # noqa: ARG001
            (tmp_dir / "foo.py").write_text("count = 1\n")

        runner = EvalRunner(tmp_path, api_key="fake")
        with patch.object(EvalRunner, "_run_pipeline", new=fake_pipeline):
            results = await runner.run([task])

        r = results[0]
        assert not r.passed
        assert r.score == 0.5

    @pytest.mark.asyncio
    async def test_pipeline_exception_recorded_as_error(self, tmp_path: Path):
        task = _task()

        async def bad_pipeline(self_inner, t, tmp_dir):  # noqa: ARG001
            raise RuntimeError("API exploded")

        runner = EvalRunner(tmp_path, api_key="fake")
        with patch.object(EvalRunner, "_run_pipeline", new=bad_pipeline):
            results = await runner.run([task])

        r = results[0]
        assert not r.passed
        assert r.score == 0.0
        assert "API exploded" in r.error

    @pytest.mark.asyncio
    async def test_empty_assertions_not_passed(self, tmp_path: Path):
        """A task with no assertions: passed=False (nothing to verify), score=0.0."""
        task = _task(assertions=[])

        async def noop(self_inner, t, tmp_dir):  # noqa: ARG001
            pass

        runner = EvalRunner(tmp_path, api_key="fake")
        with patch.object(EvalRunner, "_run_pipeline", new=noop):
            results = await runner.run([task])

        r = results[0]
        # No assertions → nothing was verified, so score is 0 and task is not passed
        assert r.score == 0.0
        assert not r.passed


# ---------------------------------------------------------------------------
# print_eval_report — smoke test
# ---------------------------------------------------------------------------

class TestPrintEvalReport:
    def test_runs_without_error(self, tmp_path: Path):
        from rich.console import Console
        from io import StringIO

        task = _task()
        results = [
            EvalResult(task=task, passed=True,  score=1.0, assertion_results=[]),
            EvalResult(task=task, passed=False, score=0.5, assertion_results=[],
                       error="something went wrong"),
        ]
        buf = StringIO()
        c = Console(file=buf, width=120)
        print_eval_report(results, console=c)
        output = buf.getvalue()
        assert "PASS"  in output
        assert "FAIL"  in output or "ERROR" in output
        assert "1/2"   in output


# ---------------------------------------------------------------------------
# Baseline helpers — Phase 13 gate
# ---------------------------------------------------------------------------

def _make_result(task_id: str, score: float) -> EvalResult:
    task = _task(id=task_id)
    return EvalResult(task=task, passed=score == 1.0, score=score, assertion_results=[])


class TestSaveLoadBaseline:
    def test_round_trip(self, tmp_path: Path):
        results = [_make_result("t-1", 1.0), _make_result("t-2", 0.5)]
        path = tmp_path / "baseline.json"
        save_baseline(results, path)
        loaded = load_baseline(path)
        assert loaded == {"t-1": 1.0, "t-2": 0.5}

    def test_sorted_keys_in_file(self, tmp_path: Path):
        results = [_make_result("z-task", 1.0), _make_result("a-task", 0.5)]
        path = tmp_path / "baseline.json"
        save_baseline(results, path)
        raw = path.read_text()
        assert raw.index("a-task") < raw.index("z-task")

    def test_load_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Could not read baseline"):
            load_baseline(tmp_path / "nonexistent.json")

    def test_load_bad_json_raises(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json{{")
        with pytest.raises(ValueError, match="Could not read baseline"):
            load_baseline(bad)

    def test_load_non_object_raises(self, tmp_path: Path):
        path = tmp_path / "arr.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(ValueError, match="not a JSON object"):
            load_baseline(path)


class TestCompareToBaseline:
    def test_no_regressions_when_scores_equal(self):
        results = [_make_result("t-1", 1.0), _make_result("t-2", 0.5)]
        baseline = {"t-1": 1.0, "t-2": 0.5}
        assert compare_to_baseline(results, baseline) == []

    def test_no_regressions_when_scores_improved(self):
        results = [_make_result("t-1", 1.0)]
        baseline = {"t-1": 0.5}
        assert compare_to_baseline(results, baseline) == []

    def test_detects_regression(self):
        results = [_make_result("t-1", 0.5)]
        baseline = {"t-1": 1.0}
        regressions = compare_to_baseline(results, baseline)
        assert len(regressions) == 1
        reg = regressions[0]
        assert reg.task_id == "t-1"
        assert reg.baseline_score == 1.0
        assert reg.current_score == 0.5
        assert reg.delta == pytest.approx(-0.5)

    def test_new_tasks_not_flagged(self):
        """Tasks not in the baseline cannot regress."""
        results = [_make_result("brand-new", 0.0)]
        baseline = {"t-1": 1.0}
        assert compare_to_baseline(results, baseline) == []

    def test_multiple_regressions(self):
        results = [
            _make_result("t-1", 0.0),
            _make_result("t-2", 0.5),
            _make_result("t-3", 1.0),  # no regression
        ]
        baseline = {"t-1": 1.0, "t-2": 1.0, "t-3": 1.0}
        regressions = compare_to_baseline(results, baseline)
        assert {r.task_id for r in regressions} == {"t-1", "t-2"}

    def test_baseline_regression_delta(self):
        results = [_make_result("t-1", 0.25)]
        baseline = {"t-1": 0.75}
        reg = compare_to_baseline(results, baseline)[0]
        assert reg.delta == pytest.approx(-0.5)


class TestPrintBaselineReport:
    def test_prints_nothing_when_no_regressions(self):
        from io import StringIO
        from rich.console import Console
        buf = StringIO()
        c = Console(file=buf, width=120)
        print_baseline_report([], console=c)
        assert buf.getvalue() == ""

    def test_prints_regressions(self):
        from io import StringIO
        from rich.console import Console
        buf = StringIO()
        c = Console(file=buf, width=120, highlight=False)
        regressions = [BaselineRegression("t-1", 1.0, 0.5)]
        print_baseline_report(regressions, console=c)
        output = buf.getvalue()
        assert "t-1" in output
        assert "REGRESSED" in output or "regression" in output.lower()
