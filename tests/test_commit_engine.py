"""Tests for turbine.commit_engine — Phase 5a."""

from __future__ import annotations

from pathlib import Path

import pytest

from turbine.commit_engine import CommitEngine, CommitResult
from turbine.vfs import VirtualFileSystem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vfs(files: dict[str, str]) -> VirtualFileSystem:
    vfs = VirtualFileSystem()
    for key, content in files.items():
        vfs.load_text(key, content)
    return vfs


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_does_not_write_files(self, tmp_path: Path):
        vfs = _make_vfs({"src/foo.py": "x = 1\n"})
        engine = CommitEngine(vfs=vfs, project_root=tmp_path, dry_run=True)
        result = engine.commit()

        assert result.dry_run
        assert not (tmp_path / "src" / "foo.py").exists()

    def test_dry_run_reports_written(self, tmp_path: Path):
        """Files with changed content are reported as 'would be written'."""
        vfs = _make_vfs({"a.py": "new content\n"})
        originals = {"a.py": ["old content"]}
        engine = CommitEngine(
            vfs=vfs, project_root=tmp_path, dry_run=True, original_snapshots=originals
        )
        result = engine.commit()

        assert result.written_count == 1
        assert result.success

    def test_dry_run_skips_unchanged_files(self, tmp_path: Path):
        content = "x = 1"
        vfs = _make_vfs({"a.py": content})
        originals = {"a.py": ["x = 1"]}
        engine = CommitEngine(
            vfs=vfs, project_root=tmp_path, dry_run=True, original_snapshots=originals
        )
        result = engine.commit()

        assert result.written_count == 0
        assert result.skipped_count == 1


# ---------------------------------------------------------------------------
# Real write mode
# ---------------------------------------------------------------------------

class TestRealCommit:
    def test_writes_file_to_disk(self, tmp_path: Path):
        vfs = _make_vfs({"src/bar.py": "y = 2\n"})
        engine = CommitEngine(vfs=vfs, project_root=tmp_path, dry_run=False)
        result = engine.commit()

        assert result.success
        assert result.written_count == 1
        assert (tmp_path / "src" / "bar.py").read_text() == "y = 2\n"

    def test_creates_parent_directories(self, tmp_path: Path):
        vfs = _make_vfs({"deep/nested/dir/file.py": "z = 3\n"})
        engine = CommitEngine(vfs=vfs, project_root=tmp_path, dry_run=False)
        result = engine.commit()

        assert result.success
        assert (tmp_path / "deep" / "nested" / "dir" / "file.py").exists()

    def test_skips_unchanged_files(self, tmp_path: Path):
        existing = ["unchanged = True"]
        vfs = _make_vfs({"mod.py": "unchanged = True\n"})
        originals = {"mod.py": existing}
        engine = CommitEngine(
            vfs=vfs, project_root=tmp_path, dry_run=False, original_snapshots=originals
        )
        result = engine.commit()

        # File should not be written (content unchanged)
        assert result.written_count == 0
        assert result.skipped_count == 1
        assert not (tmp_path / "mod.py").exists()

    def test_multiple_files(self, tmp_path: Path):
        vfs = _make_vfs({
            "a.py": "a = 1\n",
            "b.py": "b = 2\n",
        })
        engine = CommitEngine(vfs=vfs, project_root=tmp_path, dry_run=False)
        result = engine.commit()

        assert result.success
        assert result.written_count == 2

    def test_lines_before_and_after_tracked(self, tmp_path: Path):
        vfs = _make_vfs({"f.py": "line1\nline2\nline3\n"})
        originals = {"f.py": ["only_one_line"]}
        engine = CommitEngine(
            vfs=vfs, project_root=tmp_path, dry_run=False, original_snapshots=originals
        )
        result = engine.commit()

        file_result = result.files[0]
        assert file_result.lines_before == 1
        assert file_result.lines_after == 3


# ---------------------------------------------------------------------------
# CommitResult helpers
# ---------------------------------------------------------------------------

class TestCommitResult:
    def test_summary_string(self, tmp_path: Path):
        vfs = _make_vfs({"x.py": "val\n"})
        engine = CommitEngine(vfs=vfs, project_root=tmp_path, dry_run=False)
        result = engine.commit()
        summary = result.summary()
        assert "written" in summary
        assert "error" in summary

    def test_file_result_str_dry_run(self, tmp_path: Path):
        vfs = _make_vfs({"x.py": "new\n"})
        originals = {"x.py": ["old"]}
        engine = CommitEngine(
            vfs=vfs, project_root=tmp_path, dry_run=True, original_snapshots=originals
        )
        result = engine.commit()
        s = str(result.files[0])
        assert "DRY-RUN" in s
