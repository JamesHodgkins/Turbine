"""Tests for turbine.ui and turbine.commit_engine review gate — Phase 6."""

from __future__ import annotations

from pathlib import Path

import pytest

from turbine.commit_engine import CommitEngine, _stdin_confirm
from turbine.ui import PipelineStep, TurbineUI, WorkerState, WorkerStatus


# ---------------------------------------------------------------------------
# TurbineUI — disabled mode (all hooks must be no-ops)
# ---------------------------------------------------------------------------

class TestTurbineUIDisabled:
    def _ui(self) -> TurbineUI:
        return TurbineUI(enabled=False)

    def test_context_manager_does_not_raise(self):
        ui = self._ui()
        with ui:
            pass  # no exception

    def test_on_step_noop(self):
        ui = self._ui()
        ui.on_step(PipelineStep.PREPROCESS, "msg")   # should not raise

    def test_on_worker_start_noop(self):
        ui = self._ui()
        ui.on_worker_start("t1", "do something")

    def test_on_worker_attempt_noop(self):
        ui = self._ui()
        ui.on_worker_attempt("t1", 2)

    def test_on_worker_conflict_noop(self):
        ui = self._ui()
        ui.on_worker_conflict("t1", "some overlap")

    def test_on_worker_done_noop(self):
        ui = self._ui()
        ui.on_worker_done("t1", success=True)

    def test_on_worker_repair_noop(self):
        ui = self._ui()
        ui.on_worker_repair("t1")

    def test_on_done_noop(self):
        ui = self._ui()
        ui.on_done("all good")


# ---------------------------------------------------------------------------
# TurbineUI — enabled mode (state tracking)
# ---------------------------------------------------------------------------

class TestTurbineUIEnabled:
    def _ui(self) -> TurbineUI:
        # Use enabled=True but don't enter the context manager so no Live runs
        ui = TurbineUI(title="test-project", enabled=True)
        return ui

    def test_on_step_updates_current_step(self):
        ui = self._ui()
        ui.on_step(PipelineStep.INVESTIGATE, "reading…")
        assert ui._current_step == PipelineStep.INVESTIGATE
        assert "reading" in ui._step_messages[PipelineStep.INVESTIGATE]

    def test_on_worker_start_creates_state(self):
        ui = self._ui()
        ui.on_worker_start("ticket-1", "do the thing")
        assert "ticket-1" in ui._workers
        assert ui._workers["ticket-1"].status == WorkerStatus.RUNNING

    def test_on_worker_attempt_updates_attempt(self):
        ui = self._ui()
        ui.on_worker_start("t1", "desc")
        ui.on_worker_attempt("t1", 2)
        assert ui._workers["t1"].attempt == 2

    def test_on_worker_conflict_sets_conflict_status(self):
        ui = self._ui()
        ui.on_worker_start("t1", "desc")
        ui.on_worker_conflict("t1", "line overlap")
        assert ui._workers["t1"].status == WorkerStatus.CONFLICT
        assert "line overlap" in ui._workers["t1"].detail

    def test_on_worker_done_success(self):
        ui = self._ui()
        ui.on_worker_start("t1", "desc")
        ui.on_worker_done("t1", success=True)
        assert ui._workers["t1"].status == WorkerStatus.APPROVED

    def test_on_worker_done_failure(self):
        ui = self._ui()
        ui.on_worker_start("t1", "desc")
        ui.on_worker_done("t1", success=False, detail="API error")
        assert ui._workers["t1"].status == WorkerStatus.FAILED
        assert "API error" in ui._workers["t1"].detail

    def test_on_worker_repair_sets_repair_status(self):
        ui = self._ui()
        ui.on_worker_start("t1", "desc")
        ui.on_worker_repair("t1")
        assert ui._workers["t1"].status == WorkerStatus.REPAIR

    def test_on_done_sets_done_step(self):
        ui = self._ui()
        ui.on_done("2/2 succeeded")
        assert ui._current_step == PipelineStep.DONE
        assert "2/2" in ui._step_messages[PipelineStep.DONE]

    def test_render_does_not_raise(self):
        ui = self._ui()
        ui.on_step(PipelineStep.DELEGATE, "spawning…")
        ui.on_worker_start("t1", "fix the bug")
        ui.on_worker_attempt("t1", 1)
        # _render() should produce a renderable without crashing
        renderable = ui._render()
        assert renderable is not None


# ---------------------------------------------------------------------------
# WorkerState
# ---------------------------------------------------------------------------

class TestWorkerState:
    def test_initial_status_is_pending(self):
        w = WorkerState(ticket_id="t1", description="do stuff")
        assert w.status == WorkerStatus.PENDING

    def test_start_sets_running(self):
        w = WorkerState(ticket_id="t1", description="do stuff")
        w.start()
        assert w.status == WorkerStatus.RUNNING

    def test_finish_success(self):
        w = WorkerState(ticket_id="t1", description="do stuff")
        w.start()
        w.finish(success=True)
        assert w.status == WorkerStatus.APPROVED
        assert w.elapsed >= 0.0

    def test_finish_failure(self):
        w = WorkerState(ticket_id="t1", description="do stuff")
        w.start()
        w.finish(success=False)
        assert w.status == WorkerStatus.FAILED

    def test_tick_elapsed_only_when_running(self):
        w = WorkerState(ticket_id="t1", description="do stuff")
        w.elapsed = 0.0
        w.tick_elapsed()  # PENDING — should not update
        assert w.elapsed == 0.0
        w.start()
        w.tick_elapsed()  # RUNNING — elapsed should grow
        assert w.elapsed >= 0.0


# ---------------------------------------------------------------------------
# CommitEngine.review_and_commit — manual review gate
# ---------------------------------------------------------------------------

def _make_vfs_with_change():
    from turbine.vfs import VirtualFileSystem
    vfs = VirtualFileSystem()
    vfs.load_text("src/foo.py", "old line\n")
    # Simulate a worker having changed the snapshot
    vfs._snapshots["src/foo.py"] = ["new line"]
    return vfs


class TestReviewGate:
    def test_user_confirms_writes_file(self, tmp_path: Path):
        vfs = _make_vfs_with_change()
        engine = CommitEngine(
            vfs=vfs,
            project_root=tmp_path,
            dry_run=False,
            original_snapshots={"src/foo.py": ["old line"]},
        )
        result = engine.review_and_commit(confirm_fn=lambda _: True)
        assert result.written_count == 1
        assert (tmp_path / "src" / "foo.py").read_text() == "new line\n"

    def test_user_declines_does_not_write(self, tmp_path: Path):
        vfs = _make_vfs_with_change()
        engine = CommitEngine(
            vfs=vfs,
            project_root=tmp_path,
            dry_run=False,
            original_snapshots={"src/foo.py": ["old line"]},
        )
        result = engine.review_and_commit(confirm_fn=lambda _: False)
        assert result.written_count == 0
        assert not (tmp_path / "src" / "foo.py").exists()
        assert any("aborted" in f.error for f in result.files)

    def test_no_changes_skips_prompt(self, tmp_path: Path):
        from turbine.vfs import VirtualFileSystem
        vfs = VirtualFileSystem()
        vfs.load_text("src/foo.py", "same\n")
        engine = CommitEngine(
            vfs=vfs,
            project_root=tmp_path,
            dry_run=False,
            original_snapshots={"src/foo.py": ["same"]},
        )
        called = []
        result = engine.review_and_commit(confirm_fn=lambda _: called.append(1) or True)
        assert called == []   # confirm never called — nothing changed
        assert result.written_count == 0

    def test_collect_changed_files_detects_new_file(self, tmp_path: Path):
        from turbine.vfs import VirtualFileSystem
        vfs = VirtualFileSystem()
        vfs.load_text("new_file.py", "brand new\n")
        engine = CommitEngine(
            vfs=vfs,
            project_root=tmp_path,
            dry_run=False,
            original_snapshots={},  # no original → always "changed"
        )
        changed = engine._collect_changed_files()
        assert len(changed) == 1
        assert changed[0][0] == "new_file.py"


# ---------------------------------------------------------------------------
# _stdin_confirm helper
# ---------------------------------------------------------------------------

class TestStdinConfirm:
    def test_yes_returns_true(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda _: "y")
        assert _stdin_confirm("ok? ") is True

    def test_yes_full_returns_true(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda _: "yes")
        assert _stdin_confirm("ok? ") is True

    def test_no_returns_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert _stdin_confirm("ok? ") is False

    def test_empty_returns_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert _stdin_confirm("ok? ") is False

    def test_eof_returns_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))
        assert _stdin_confirm("ok? ") is False
