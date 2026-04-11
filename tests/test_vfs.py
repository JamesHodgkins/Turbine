"""Tests for VirtualFileSystem and ConflictDetector (Phase 2)."""

import pytest
from pathlib import Path
from turbine.vfs import VirtualFileSystem, ConflictDetector, Hunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_FILE = """\
line one
line two
line three
line four
line five
"""

def _make_diff(file_key: str, orig_start: int, orig_count: int, new_body: list[str]) -> str:
    """Build a minimal unified diff string."""
    new_count = len(new_body)
    removed = "\n".join(f"-placeholder" for _ in range(orig_count))
    added   = "\n".join(f"+{l}" for l in new_body)
    context_lines = "\n".join(f" placeholder" for _ in range(orig_count))
    return (
        f"--- a/{file_key}\n"
        f"+++ b/{file_key}\n"
        f"@@ -{orig_start},{orig_count} +{orig_start},{new_count} @@\n"
        + "\n".join(f"-old line" for _ in range(orig_count))
        + "\n"
        + "\n".join(f"+{l}" for l in new_body)
        + "\n"
    )


# ---------------------------------------------------------------------------
# VirtualFileSystem — load / snapshot
# ---------------------------------------------------------------------------

def test_load_text_stores_lines():
    vfs = VirtualFileSystem()
    vfs.load_text("src/foo.py", "a\nb\nc")
    assert vfs.get_snapshot("src/foo.py") == ["a", "b", "c"]


def test_load_from_disk(tmp_path):
    f = tmp_path / "hello.py"
    f.write_text("x = 1\ny = 2\n")
    vfs = VirtualFileSystem()
    vfs.load_from_disk(f, relative_key="hello.py")
    assert vfs.get_snapshot("hello.py") == ["x = 1", "y = 2"]


# ---------------------------------------------------------------------------
# VirtualFileSystem — apply_diff
# ---------------------------------------------------------------------------

def test_apply_diff_replaces_lines():
    vfs = VirtualFileSystem()
    vfs.load_text("f.py", "line one\nline two\nline three")
    diff = (
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -2,1 +2,1 @@\n"
        "-line two\n"
        "+LINE TWO\n"
    )
    hunks = vfs.apply_diff("worker-1", diff)
    assert len(hunks) == 1
    assert vfs.get_snapshot("f.py") == ["line one", "LINE TWO", "line three"]


def test_apply_diff_deletion():
    vfs = VirtualFileSystem()
    vfs.load_text("f.py", "a\nb\nc")
    diff = (
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -2,1 +2,0 @@\n"
        "-b\n"
    )
    vfs.apply_diff("worker-1", diff)
    assert vfs.get_snapshot("f.py") == ["a", "c"]


def test_apply_diff_insertion():
    vfs = VirtualFileSystem()
    vfs.load_text("f.py", "a\nc")
    diff = (
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,1 +1,2 @@\n"
        " a\n"
        "+b\n"
    )
    vfs.apply_diff("worker-1", diff)
    # context line "a" is kept; "b" is inserted after it
    assert vfs.get_snapshot("f.py") == ["a", "b", "c"]


def test_apply_diff_unknown_file_raises():
    vfs = VirtualFileSystem()
    diff = (
        "--- a/unknown.py\n"
        "+++ b/unknown.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    with pytest.raises(ValueError, match="unknown.py"):
        vfs.apply_diff("worker-1", diff)


def test_staged_hunks_recorded():
    vfs = VirtualFileSystem()
    vfs.load_text("f.py", "a\nb\nc")
    diff = (
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -2,1 +2,1 @@\n"
        "-b\n"
        "+B\n"
    )
    vfs.apply_diff("worker-1", diff)
    staged = vfs.staged_hunks("f.py")
    assert len(staged) == 1
    assert staged[0].worker_id == "worker-1"
    assert staged[0].orig_start == 2


# ---------------------------------------------------------------------------
# VirtualFileSystem — commit_to_disk
# ---------------------------------------------------------------------------

def test_commit_to_disk(tmp_path):
    vfs = VirtualFileSystem()
    vfs.load_text("src/module.py", "x = 1\ny = 2")
    diff = (
        "--- a/src/module.py\n"
        "+++ b/src/module.py\n"
        "@@ -2,1 +2,1 @@\n"
        "-y = 2\n"
        "+y = 99\n"
    )
    vfs.apply_diff("worker-1", diff)
    written = vfs.commit_to_disk(tmp_path)
    assert len(written) == 1
    content = (tmp_path / "src" / "module.py").read_text()
    assert "y = 99" in content
    assert "y = 2" not in content


# ---------------------------------------------------------------------------
# ConflictDetector
# ---------------------------------------------------------------------------

def _two_worker_vfs() -> VirtualFileSystem:
    vfs = VirtualFileSystem()
    vfs.load_text("app.py", "\n".join(f"line {i}" for i in range(1, 11)))
    return vfs


def test_no_conflict_disjoint_ranges():
    vfs = _two_worker_vfs()
    diff_a = (
        "--- a/app.py\n+++ b/app.py\n"
        "@@ -1,1 +1,1 @@\n-line 1\n+LINE 1\n"
    )
    diff_b = (
        "--- a/app.py\n+++ b/app.py\n"
        "@@ -5,1 +5,1 @@\n-line 5\n+LINE 5\n"
    )
    vfs.apply_diff("worker-A", diff_a)
    vfs.apply_diff("worker-B", diff_b)
    detector = ConflictDetector(vfs)
    assert detector.check() == []


def test_conflict_overlapping_ranges():
    vfs = _two_worker_vfs()
    # Both workers want to rewrite lines 3-4
    diff_a = (
        "--- a/app.py\n+++ b/app.py\n"
        "@@ -3,2 +3,2 @@\n-line 3\n-line 4\n+A3\n+A4\n"
    )
    diff_b = (
        "--- a/app.py\n+++ b/app.py\n"
        "@@ -4,1 +4,1 @@\n-line 4\n+B4\n"
    )
    vfs.apply_diff("worker-A", diff_a)
    vfs.apply_diff("worker-B", diff_b)
    detector = ConflictDetector(vfs)
    conflicts = detector.check()
    assert len(conflicts) == 1
    assert conflicts[0].hunk_a.worker_id == "worker-A"
    assert conflicts[0].hunk_b.worker_id == "worker-B"


def test_same_worker_hunks_not_flagged():
    """A worker whose own hunks happen to be adjacent should not self-conflict."""
    vfs = _two_worker_vfs()
    diff = (
        "--- a/app.py\n+++ b/app.py\n"
        "@@ -1,1 +1,1 @@\n-line 1\n+X\n"
        "@@ -3,1 +3,1 @@\n-line 3\n+Y\n"
    )
    vfs.apply_diff("worker-A", diff)
    detector = ConflictDetector(vfs)
    assert detector.check() == []


def test_conflict_str_is_readable():
    vfs = _two_worker_vfs()
    diff_a = "--- a/app.py\n+++ b/app.py\n@@ -2,2 +2,2 @@\n-line 2\n-line 3\n+A\n+A\n"
    diff_b = "--- a/app.py\n+++ b/app.py\n@@ -3,1 +3,1 @@\n-line 3\n+B\n"
    vfs.apply_diff("worker-A", diff_a)
    vfs.apply_diff("worker-B", diff_b)
    conflicts = ConflictDetector(vfs).check()
    assert "worker-A" in str(conflicts[0])
    assert "worker-B" in str(conflicts[0])
    assert "app.py" in str(conflicts[0])
