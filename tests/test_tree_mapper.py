"""Tests for TreeMapper (Step 1: Discovery)."""

import pytest
from pathlib import Path
from turbine.tree_mapper import TreeMapper


def test_maps_current_project(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "b.py").write_text("y = 2")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.pyc").write_bytes(b"")

    tree = TreeMapper(tmp_path).map()

    relatives = [f.relative for f in tree.files]
    assert "a.py" in relatives
    assert "b.py" in relatives
    # Ignored dirs and extensions are excluded
    assert not any("__pycache__" in r for r in relatives)
    assert not any(r.endswith(".pyc") for r in relatives)


def test_summary_contains_root(tmp_path):
    (tmp_path / "main.py").write_text("")
    tree = TreeMapper(tmp_path).map()
    assert str(tmp_path) in tree.summary()


def test_by_extension(tmp_path):
    (tmp_path / "script.py").write_text("")
    (tmp_path / "config.toml").write_text("")
    tree = TreeMapper(tmp_path).map()
    assert len(tree.by_extension(".py")) == 1
    assert len(tree.by_extension(".toml")) == 1
