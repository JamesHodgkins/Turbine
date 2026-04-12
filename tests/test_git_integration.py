"""Tests for turbine.git_integration — Phase 7 / Phase 22."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from turbine.git_integration import GitIntegration, PreflightResult, build_commit_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True,
    )


def _init_repo(path: Path) -> None:
    """Initialise a git repo with one committed file so HEAD exists."""
    _git("init", cwd=path)
    _git("config", "user.email", "test@turbine.dev", cwd=path)
    _git("config", "user.name", "Turbine Test", cwd=path)
    (path / "init.txt").write_text("initial\n")
    _git("add", "init.txt", cwd=path)
    _git("commit", "-m", "init", cwd=path)


def _dirty(path: Path, filename: str = "dirty.txt") -> None:
    """Create an untracked (dirty) file in the repo."""
    (path / filename).write_text("dirty\n")


def _tracked_and_modified(path: Path) -> None:
    """Stage and then locally modify a tracked file."""
    f = path / "tracked.txt"
    f.write_text("v1\n")
    _git("add", "tracked.txt", cwd=path)
    _git("commit", "-m", "add tracked", cwd=path)
    f.write_text("v2\n")   # modify without staging → dirty


# ---------------------------------------------------------------------------
# GitIntegration.is_repo
# ---------------------------------------------------------------------------

class TestIsRepo:
    def test_not_a_git_repo(self, tmp_path: Path):
        gi = GitIntegration(tmp_path)
        assert gi.is_repo is False

    def test_is_a_git_repo(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path)
        assert gi.is_repo is True

    def test_subdirectory_of_repo_is_not_is_repo(self, tmp_path: Path):
        """A subdirectory inside a git repo should NOT be treated as a repo root."""
        _init_repo(tmp_path)
        subdir = tmp_path / "my_project"
        subdir.mkdir()
        gi = GitIntegration(subdir)
        assert gi.is_repo is False

    def test_subdirectory_sets_is_subdir_of_repo(self, tmp_path: Path):
        """The subdir flag must be set so callers can emit a diagnostic."""
        _init_repo(tmp_path)
        subdir = tmp_path / "my_project"
        subdir.mkdir()
        gi = GitIntegration(subdir)
        assert gi.is_subdir_of_repo is True

    def test_repo_root_does_not_set_is_subdir_of_repo(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path)
        assert gi.is_subdir_of_repo is False

    def test_non_repo_does_not_set_is_subdir_of_repo(self, tmp_path: Path):
        gi = GitIntegration(tmp_path)
        assert gi.is_subdir_of_repo is False


# ---------------------------------------------------------------------------
# GitIntegration.preflight
# ---------------------------------------------------------------------------

class TestPreflight:
    def test_not_a_repo_returns_empty(self, tmp_path: Path):
        gi = GitIntegration(tmp_path)
        result = gi.preflight()
        assert result.is_repo is False
        assert result.dirty_files == []
        assert result.is_clean

    def test_clean_repo_is_clean(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path)
        result = gi.preflight()
        assert result.is_repo is True
        assert result.is_clean

    def test_untracked_file_shows_as_dirty(self, tmp_path: Path):
        _init_repo(tmp_path)
        _dirty(tmp_path, "new.py")
        gi = GitIntegration(tmp_path)
        result = gi.preflight()
        assert not result.is_clean
        assert any("new.py" in f for f in result.dirty_files)

    def test_modified_tracked_file_shows_as_dirty(self, tmp_path: Path):
        _init_repo(tmp_path)
        _tracked_and_modified(tmp_path)
        gi = GitIntegration(tmp_path)
        result = gi.preflight()
        assert not result.is_clean
        assert any("tracked.txt" in f for f in result.dirty_files)


# ---------------------------------------------------------------------------
# GitIntegration.create_branch
# ---------------------------------------------------------------------------

class TestCreateBranch:
    def test_not_a_repo_returns_none(self, tmp_path: Path):
        gi = GitIntegration(tmp_path)
        assert gi.create_branch() is None

    def test_no_branch_flag_returns_none(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path, no_branch=True)
        assert gi.create_branch() is None
        assert gi.branch is None

    def test_creates_branch_with_timestamp(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path)
        branch = gi.create_branch(timestamp="20240101-120000")
        assert branch == "turbine/run-20240101-120000"
        assert gi.branch == branch
        # Verify the branch actually exists in git
        result = _git("branch", "--list", branch, cwd=tmp_path)
        assert branch in result.stdout

    def test_auto_timestamp_format(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path)
        branch = gi.create_branch()
        assert branch is not None
        assert branch.startswith("turbine/run-")

    def test_duplicate_branch_raises(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path)
        gi.create_branch(timestamp="20240101-120000")
        gi2 = GitIntegration(tmp_path)
        with pytest.raises(RuntimeError, match="checkout -b"):
            gi2.create_branch(timestamp="20240101-120000")


# ---------------------------------------------------------------------------
# GitIntegration.auto_commit
# ---------------------------------------------------------------------------

class TestAutoCommit:
    def test_not_a_repo_returns_false(self, tmp_path: Path):
        gi = GitIntegration(tmp_path)
        assert gi.auto_commit(["file.py"], "msg") is False

    def test_empty_file_list_returns_false(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path)
        assert gi.auto_commit([], "msg") is False

    def test_commits_written_file(self, tmp_path: Path):
        _init_repo(tmp_path)
        target = tmp_path / "app.py"
        target.write_text("x = 1\n")
        gi = GitIntegration(tmp_path)
        gi.create_branch(timestamp="20240101-130000")
        ok = gi.auto_commit(["app.py"], "turbine: add app")
        assert ok is True
        assert gi.committed is True
        # Verify the commit exists
        log = _git("log", "--oneline", "-1", cwd=tmp_path)
        assert "turbine: add app" in log.stdout

    def test_only_stages_specified_files(self, tmp_path: Path):
        """Pre-existing dirty file must NOT appear in Turbine's commit."""
        _init_repo(tmp_path)
        # Write and commit via turbine
        (tmp_path / "turbine_wrote.py").write_text("a = 1\n")
        # Leave an unrelated dirty file untouched
        (tmp_path / "user_dirty.py").write_text("user change\n")
        gi = GitIntegration(tmp_path)
        gi.create_branch(timestamp="20240101-140000")
        gi.auto_commit(["turbine_wrote.py"], "turbine: write app")
        # The user's dirty file should still be untracked / unstaged
        status = _git("status", "--porcelain", cwd=tmp_path)
        assert "user_dirty.py" in status.stdout

    def test_committed_property_false_before_commit(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path)
        assert gi.committed is False

    def test_committed_property_true_after_commit(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "f.py").write_text("x\n")
        gi = GitIntegration(tmp_path)
        gi.create_branch(timestamp="20240101-150000")
        gi.auto_commit(["f.py"], "turbine: commit")
        assert gi.committed is True


# ---------------------------------------------------------------------------
# GitIntegration.diff_hint / undo_hint
# ---------------------------------------------------------------------------

class TestHints:
    def test_diff_hint_not_a_repo(self, tmp_path: Path):
        gi = GitIntegration(tmp_path)
        assert gi.diff_hint() == ""

    def test_undo_hint_not_a_repo(self, tmp_path: Path):
        gi = GitIntegration(tmp_path)
        assert gi.undo_hint() == ""

    def test_hints_empty_before_commit(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path)
        assert gi.diff_hint() == ""
        assert gi.undo_hint() == ""

    def test_diff_hint_after_commit(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "x.py").write_text("1\n")
        gi = GitIntegration(tmp_path)
        gi.create_branch(timestamp="20240101-160000")
        gi.auto_commit(["x.py"], "turbine: x")
        hint = gi.diff_hint()
        assert "show" in hint
        assert "HEAD" in hint

    def test_undo_hint_after_commit(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "y.py").write_text("2\n")
        gi = GitIntegration(tmp_path)
        gi.create_branch(timestamp="20240101-170000")
        gi.auto_commit(["y.py"], "turbine: y")
        hint = gi.undo_hint()
        assert "revert" in hint
        assert "HEAD" in hint


# ---------------------------------------------------------------------------
# build_commit_message
# ---------------------------------------------------------------------------

class TestBuildCommitMessage:
    def test_subject_from_diagnosis(self):
        msg = build_commit_message("The cache is never invalidated.", [], "")
        assert msg.startswith("turbine: The cache is never invalidated")

    def test_subject_capped_at_60_chars(self):
        long = "A" * 70
        msg = build_commit_message(long, [], "")
        subject = msg.splitlines()[0]
        # Subject = "turbine: " (9) + 60 chars + "…" = 70 chars max
        assert len(subject) <= 71

    def test_fallback_to_user_request_when_no_diagnosis(self):
        msg = build_commit_message("", [], "fix the login bug")
        assert "fix the login bug" in msg.splitlines()[0]

    def test_default_subject_when_nothing_provided(self):
        msg = build_commit_message("", [], "")
        assert msg.startswith("turbine: automated repair")

    def test_body_contains_diagnosis(self):
        msg = build_commit_message("Root cause: foo", [], "")
        assert "Root cause: foo" in msg

    def test_body_contains_ticket_list(self):
        class T:
            def __init__(self, id_: str, desc: str):
                self.id = id_
                self.description = desc

        tickets = [T("ticket-1", "fix cache"), T("ticket-2", "update tests")]
        msg = build_commit_message("diagnosis text", tickets, "")
        assert "ticket-1: fix cache" in msg
        assert "ticket-2: update tests" in msg

    def test_body_contains_user_request(self):
        msg = build_commit_message("diagnosis", [], "please fix the bug")
        assert "please fix the bug" in msg

    def test_no_empty_body_when_no_info(self):
        msg = build_commit_message("", [], "")
        # Should not have a trailing blank line from an empty body
        assert msg.strip() == "turbine: automated repair"


# ---------------------------------------------------------------------------
# Phase 22 — Lock file
# ---------------------------------------------------------------------------

class TestLockFile:
    def test_acquire_creates_lock_file(self, tmp_path: Path):
        gi = GitIntegration(tmp_path)
        gi.acquire_lock()
        assert (tmp_path / ".turbine.lock").exists()
        gi.release_lock()

    def test_lock_file_contains_pid(self, tmp_path: Path):
        import os
        gi = GitIntegration(tmp_path)
        gi.acquire_lock()
        content = (tmp_path / ".turbine.lock").read_text().strip()
        assert content == str(os.getpid())
        gi.release_lock()

    def test_second_acquire_raises(self, tmp_path: Path):
        gi1 = GitIntegration(tmp_path)
        gi1.acquire_lock()
        gi2 = GitIntegration(tmp_path)
        with pytest.raises(RuntimeError, match=r"\.turbine\.lock"):
            gi2.acquire_lock()
        gi1.release_lock()

    def test_release_removes_lock_file(self, tmp_path: Path):
        gi = GitIntegration(tmp_path)
        gi.acquire_lock()
        gi.release_lock()
        assert not (tmp_path / ".turbine.lock").exists()

    def test_release_is_idempotent(self, tmp_path: Path):
        gi = GitIntegration(tmp_path)
        gi.release_lock()  # no-op when no lock exists — must not raise

    def test_acquire_works_in_non_repo(self, tmp_path: Path):
        """Lock must work regardless of whether the target is a git repo."""
        gi = GitIntegration(tmp_path)
        assert gi.is_repo is False
        gi.acquire_lock()
        assert (tmp_path / ".turbine.lock").exists()
        gi.release_lock()


# ---------------------------------------------------------------------------
# Phase 22 — chat_id branch creation and resume
# ---------------------------------------------------------------------------

class TestChatIdBranching:
    def test_chat_id_creates_named_branch(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path, chat_id="abc123")
        branch = gi.create_branch()
        assert branch == "turbine/chat-abc123"
        assert gi.branch == branch
        assert gi.resumed is False
        result = _git("branch", "--list", branch, cwd=tmp_path)
        assert branch in result.stdout

    def test_chat_id_resumes_on_clean_tree(self, tmp_path: Path):
        _init_repo(tmp_path)
        # Create the branch first
        gi1 = GitIntegration(tmp_path, chat_id="sess1")
        gi1.create_branch()
        # Return to main/master so we can test resume
        _git("checkout", "-", cwd=tmp_path)

        gi2 = GitIntegration(tmp_path, chat_id="sess1")
        preflight = gi2.preflight()
        assert preflight.is_clean
        branch = gi2.create_branch(preflight=preflight)
        assert branch == "turbine/chat-sess1"
        assert gi2.resumed is True

    def test_chat_id_skips_resume_on_dirty_tree(self, tmp_path: Path):
        _init_repo(tmp_path)
        # Create the branch first
        gi1 = GitIntegration(tmp_path, chat_id="sess2")
        gi1.create_branch()
        _git("checkout", "-", cwd=tmp_path)

        # Dirty the tree
        _dirty(tmp_path, "manual_edit.py")

        gi2 = GitIntegration(tmp_path, chat_id="sess2")
        preflight = gi2.preflight()
        assert not preflight.is_clean

        # With dirty tree, create_branch must fail because
        # turbine/chat-sess2 already exists — the caller should
        # handle this (e.g. by appending a suffix); for now we
        # confirm it does NOT resume (i.e. RuntimeError is raised
        # when trying to create a branch that already exists)
        with pytest.raises(RuntimeError):
            gi2.create_branch(preflight=preflight)

    def test_no_chat_id_still_uses_timestamp(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path)
        branch = gi.create_branch(timestamp="20240101-120000")
        assert branch == "turbine/run-20240101-120000"
        assert gi.resumed is False

    def test_resumed_false_on_new_branch(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path, chat_id="newid")
        gi.create_branch()
        assert gi.resumed is False


# ---------------------------------------------------------------------------
# Phase 21 — --new-chat isolation
# ---------------------------------------------------------------------------

class TestNewChat:
    def test_new_chat_creates_unique_branch(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path, chat_id="uniqueid", new_chat=True)
        branch = gi.create_branch()
        assert branch == "turbine/chat-uniqueid"
        assert gi.resumed is False

    def test_new_chat_returns_to_base_when_on_turbine_branch(self, tmp_path: Path):
        """new_chat=True must ensure the new branch starts from the base branch,
        removing turbine-committed files from disk."""
        _init_repo(tmp_path)
        # Create and switch to a prior turbine branch
        gi_old = GitIntegration(tmp_path, chat_id="old")
        gi_old.create_branch()
        # Confirm we are now on a turbine branch
        result = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tmp_path)
        assert result.stdout.strip().startswith("turbine/")

        # New chat — should return to base branch before creating new branch
        gi_new = GitIntegration(tmp_path, chat_id="fresh", new_chat=True)
        branch = gi_new.create_branch()
        assert branch == "turbine/chat-fresh"
        # original_branch is the base (main/master) — disk reset to clean state
        assert gi_new._original_branch in ("main", "master")
        # disk_changed flag must be set so manager re-maps the tree
        assert gi_new.disk_changed is True

    def test_new_chat_stays_on_base_when_already_there(self, tmp_path: Path):
        _init_repo(tmp_path)
        # Already on the base branch — create_branch should work normally
        gi = GitIntegration(tmp_path, chat_id="abc", new_chat=True)
        branch = gi.create_branch()
        assert branch == "turbine/chat-abc"


# ---------------------------------------------------------------------------
# Phase 21 — purge_history
# ---------------------------------------------------------------------------

class TestPurgeHistory:
    def test_purge_no_turbine_branches(self, tmp_path: Path):
        _init_repo(tmp_path)
        gi = GitIntegration(tmp_path)
        deleted = gi.purge_history()
        assert deleted == []

    def test_purge_deletes_all_turbine_branches(self, tmp_path: Path):
        _init_repo(tmp_path)
        # Create two turbine branches
        _git("checkout", "-b", "turbine/chat-aaa", cwd=tmp_path)
        _git("checkout", "-", cwd=tmp_path)
        _git("checkout", "-b", "turbine/run-20240101-120000", cwd=tmp_path)
        _git("checkout", "-", cwd=tmp_path)

        gi = GitIntegration(tmp_path)
        deleted = gi.purge_history()
        assert len(deleted) == 2
        assert "turbine/chat-aaa" in deleted
        assert "turbine/run-20240101-120000" in deleted

        # Confirm branches are gone
        remaining = _git("branch", "--list", "turbine/*", cwd=tmp_path)
        assert remaining.stdout.strip() == ""

    def test_purge_leaves_non_turbine_branches(self, tmp_path: Path):
        _init_repo(tmp_path)
        _git("checkout", "-b", "feature/my-feature", cwd=tmp_path)
        _git("checkout", "-", cwd=tmp_path)
        _git("checkout", "-b", "turbine/run-ts", cwd=tmp_path)
        _git("checkout", "-", cwd=tmp_path)

        gi = GitIntegration(tmp_path)
        deleted = gi.purge_history()
        assert deleted == ["turbine/run-ts"]

        remaining = _git("branch", "--list", "feature/*", cwd=tmp_path)
        assert "feature/my-feature" in remaining.stdout

    def test_purge_switches_off_turbine_branch_first(self, tmp_path: Path):
        _init_repo(tmp_path)
        _git("checkout", "-b", "turbine/chat-xyz", cwd=tmp_path)
        # We're now ON a turbine branch — purge must switch away first

        gi = GitIntegration(tmp_path)
        deleted = gi.purge_history()
        assert "turbine/chat-xyz" in deleted

        # Should be back on main/master, not on the deleted branch
        current = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tmp_path)
        assert current.stdout.strip() in ("main", "master")

    def test_purge_raises_when_not_a_repo(self, tmp_path: Path):
        gi = GitIntegration(tmp_path)
        with pytest.raises(RuntimeError, match="git repository"):
            gi.purge_history()
