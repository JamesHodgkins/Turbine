"""Tree Mapper — Step 1: Discovery. Maps project file structure and symbol relationships."""

import os
from pathlib import Path
from dataclasses import dataclass, field


IGNORED_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache"}
IGNORED_EXTENSIONS = {".pyc", ".pyo", ".egg-info"}

TURBINEIGNORE_FILE = ".turbineignore"


def _load_turbineignore(root: Path) -> set[str]:
    """Read ``.turbineignore`` from *root* and return the set of patterns.

    Each non-blank, non-comment line is treated as either:
    - A directory name (no slash) — added to IGNORED_DIRS for this run.
    - A file extension starting with ``*`` (e.g. ``*.log``) — treated as an
      extension pattern.
    - Any other string — matched as a path prefix relative to *root*.

    Returns a set of raw pattern strings.  The caller is responsible for
    interpreting them; :class:`TreeMapper` uses ``_matches_ignore`` below.
    """
    path = root / TURBINEIGNORE_FILE
    if not path.is_file():
        return set()
    patterns: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.add(line)
    return patterns


def _matches_ignore(relative: str, patterns: set[str]) -> bool:
    """Return True if *relative* (forward-slash path) matches any ignore pattern."""
    for pat in patterns:
        if pat.startswith("*."):
            # Extension glob: *.log, *.tmp, etc.
            if relative.endswith(pat[1:]):
                return True
        elif "/" not in pat:
            # Bare name — matches any path component (directory or filename)
            parts = relative.replace("\\", "/").split("/")
            if pat in parts:
                return True
        else:
            # Prefix match for paths like "dist/" or "build/output"
            prefix = pat.rstrip("/")
            if relative == prefix or relative.startswith(prefix + "/"):
                return True
    return False


@dataclass
class FileNode:
    path: Path
    relative: str
    size_bytes: int
    extension: str


@dataclass
class ProjectTree:
    root: Path
    files: list[FileNode] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Project root: {self.root}", f"Files found: {len(self.files)}", ""]
        for f in self.files:
            lines.append(f"  {f.relative}")
        return "\n".join(lines)

    def by_extension(self, ext: str) -> list[FileNode]:
        return [f for f in self.files if f.extension == ext]


class TreeMapper:
    def __init__(self, root: str | Path, extra_ignore_patterns: list[str] | None = None):
        self.root = Path(root).resolve()
        # Phase 17: extra patterns from turbine.toml ignore_patterns
        self._extra_ignore_patterns: list[str] = extra_ignore_patterns or []

    def map(self) -> ProjectTree:
        # Phase 12: load user-defined ignore patterns from .turbineignore
        # Phase 17: also merge in patterns from turbine.toml
        extra_patterns = _load_turbineignore(self.root) | set(self._extra_ignore_patterns)

        tree = ProjectTree(root=self.root)
        for dirpath, dirnames, filenames in os.walk(self.root):
            # Prune ignored directories in-place (built-ins + .turbineignore names)
            dirnames[:] = [
                d for d in dirnames
                if d not in IGNORED_DIRS
                and not _matches_ignore(d, extra_patterns)
            ]
            for filename in filenames:
                full_path = Path(dirpath) / filename
                ext = full_path.suffix
                if ext in IGNORED_EXTENSIONS:
                    continue
                # Normalize to forward slashes so VFS keys are consistent
                # on Windows (where Path uses backslash separators).
                relative = str(full_path.relative_to(self.root)).replace("\\", "/")
                # Phase 12: apply .turbineignore file/path patterns
                if _matches_ignore(relative, extra_patterns):
                    continue
                tree.files.append(
                    FileNode(
                        path=full_path,
                        relative=relative,
                        size_bytes=full_path.stat().st_size,
                        extension=ext,
                    )
                )
        return tree
