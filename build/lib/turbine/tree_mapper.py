"""Tree Mapper — Step 1: Discovery. Maps project file structure and symbol relationships."""

import os
from pathlib import Path
from dataclasses import dataclass, field


IGNORED_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache"}
IGNORED_EXTENSIONS = {".pyc", ".pyo", ".egg-info"}


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
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def map(self) -> ProjectTree:
        tree = ProjectTree(root=self.root)
        for dirpath, dirnames, filenames in os.walk(self.root):
            # Prune ignored directories in-place
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            for filename in filenames:
                full_path = Path(dirpath) / filename
                ext = full_path.suffix
                if ext in IGNORED_EXTENSIONS:
                    continue
                # Normalize to forward slashes so VFS keys are consistent
                # on Windows (where Path uses backslash separators).
                relative = str(full_path.relative_to(self.root)).replace("\\", "/")
                tree.files.append(
                    FileNode(
                        path=full_path,
                        relative=relative,
                        size_bytes=full_path.stat().st_size,
                        extension=ext,
                    )
                )
        return tree
