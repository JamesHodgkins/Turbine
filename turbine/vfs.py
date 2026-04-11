"""Virtual File System (VFS) — Phase 2.

Holds in-memory snapshots of files and stages worker diffs without touching
the real filesystem.  The ConflictDetector checks staged hunks for line-range
overlaps before any write is committed to disk.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Diff primitives
# ---------------------------------------------------------------------------

class Hunk(NamedTuple):
    """A single contiguous change block within a unified diff."""
    worker_id: str
    file_path: str        # normalised relative path
    orig_start: int       # 1-based first line of the original range
    orig_count: int       # number of lines in the original range
    new_lines: list[str]  # replacement lines (may be empty for deletions)

    @property
    def orig_end(self) -> int:
        """Last line (inclusive) of the original range."""
        return self.orig_start + max(self.orig_count - 1, 0)


# ---------------------------------------------------------------------------
# Conflict representation
# ---------------------------------------------------------------------------

@dataclass
class Conflict:
    file_path: str
    hunk_a: Hunk
    hunk_b: Hunk

    def __str__(self) -> str:
        return (
            f"CONFLICT in '{self.file_path}': "
            f"worker '{self.hunk_a.worker_id}' lines {self.hunk_a.orig_start}–{self.hunk_a.orig_end} "
            f"overlaps worker '{self.hunk_b.worker_id}' lines {self.hunk_b.orig_start}–{self.hunk_b.orig_end}"
        )


# ---------------------------------------------------------------------------
# Virtual File System
# ---------------------------------------------------------------------------

class VirtualFileSystem:
    """In-memory mirror of the files a set of workers are allowed to modify.

    Typical lifecycle
    -----------------
    1. ``load_from_disk(path)``  — snapshot a real file into the VFS.
    2. ``apply_diff(worker_id, unified_diff)``  — stage a worker's proposed
       changes; returns the list of ``Hunk`` objects that were applied.
    3. ``conflict_detector.check()``  — find overlapping hunks before commit.
    4. ``commit_to_disk(root)``  — write approved snapshots back to disk.
    """

    def __init__(self) -> None:
        # file_path -> list of lines (no trailing newline stored)
        self._snapshots: dict[str, list[str]] = {}
        # file_path -> list of staged Hunk objects (in application order)
        self._staged: dict[str, list[Hunk]] = {}
        # file_path -> original lines before any diffs were applied (immutable baseline)
        self._baselines: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Snapshot management
    # ------------------------------------------------------------------

    def load_from_disk(self, path: str | Path, relative_key: str | None = None) -> None:
        """Read a real file and store its lines as the base snapshot."""
        p = Path(path)
        key = relative_key or str(p)
        lines = p.read_text(encoding="utf-8").splitlines()
        self._snapshots[key] = lines
        self._baselines[key] = list(lines)
        self._staged.setdefault(key, [])

    def load_text(self, key: str, text: str) -> None:
        """Load a file from a string (useful for tests)."""
        lines = text.splitlines()
        self._snapshots[key] = lines
        self._baselines[key] = list(lines)
        self._staged.setdefault(key, [])

    def get_snapshot(self, key: str) -> list[str] | None:
        """Return the current (post-staged-edits) lines for a file."""
        return list(self._snapshots.get(key, []))

    def get_baseline(self, key: str) -> list[str] | None:
        """Return the original lines for a file before any diffs were applied."""
        lines = self._baselines.get(key)
        return list(lines) if lines is not None else None

    def staged_hunks(self, key: str) -> list[Hunk]:
        return list(self._staged.get(key, []))

    def all_staged_hunks(self) -> dict[str, list[Hunk]]:
        return {k: list(v) for k, v in self._staged.items()}

    def all_snapshots(self) -> dict[str, list[str]]:
        """Return a copy of all current in-memory snapshots."""
        return {k: list(v) for k, v in self._snapshots.items()}

    # ------------------------------------------------------------------
    # Diff application
    # ------------------------------------------------------------------

    def apply_diff(self, worker_id: str, unified_diff: str) -> list[Hunk]:
        """Parse and apply a unified diff string; return applied Hunk list.

        Only the ``@@`` hunk headers and the +/-/space lines are required;
        the ``---``/``+++`` header lines identify the target file.

        Raises
        ------
        ValueError
            If the diff targets an unknown file or a hunk cannot be applied
            cleanly (context mismatch).
        """
        hunks = _parse_unified_diff(worker_id, unified_diff)
        applied: list[Hunk] = []

        # Validate all target files exist before mutating anything
        for hunk in hunks:
            if hunk.file_path not in self._snapshots:
                raise ValueError(
                    f"VFS has no snapshot for '{hunk.file_path}'. "
                    "Call load_from_disk() or load_text() first."
                )

        # Group new hunks by file and rebuild each snapshot by replaying
        # all staged hunks (existing + new) from the baseline in one pass.
        # This keeps orig_start offsets consistent — they always refer to
        # the baseline, and the replay accounts for line-count shifts.
        new_by_file: dict[str, list[Hunk]] = defaultdict(list)
        for hunk in hunks:
            new_by_file[hunk.file_path].append(hunk)

        for key, new_hunks in new_by_file.items():
            baseline = self._baselines.get(key)
            if baseline is None:
                raise ValueError(f"VFS has no baseline for '{key}'.")
            all_hunks = self._staged[key] + new_hunks
            self._snapshots[key] = _replay_hunks(list(baseline), all_hunks)
            self._staged[key] = all_hunks
            applied.extend(new_hunks)

        return applied

    def rollback_diff(self, applied_hunks: list[Hunk]) -> None:
        """Revert a previously applied set of hunks, restoring the snapshot.

        ``applied_hunks`` must be the list returned by the ``apply_diff`` call
        being rolled back — it is used to determine how many hunks to strip
        from the tail of each file's staged list.

        The snapshot is rebuilt from the immutable baseline by re-applying
        only the surviving staged hunks, so the in-memory state is clean
        regardless of how the hunk mutated the snapshot.
        """
        # Group the rolled-back hunks by file so we know how many to strip
        # from the tail of each file's staged list.
        tail_counts: Counter[str] = Counter(h.file_path for h in applied_hunks)

        files_to_fix: set[str] = set(tail_counts.keys())

        for key in files_to_fix:
            if key not in self._snapshots:
                continue
            n = tail_counts[key]
            if n:
                self._staged[key] = self._staged[key][:-n]

        # Rebuild each affected snapshot from the immutable baseline by
        # re-applying only the surviving staged hunks in order.
        for key in files_to_fix:
            baseline = self._baselines.get(key)
            if baseline is None:
                continue
            self._snapshots[key] = _replay_hunks(list(baseline), self._staged[key])

    # ------------------------------------------------------------------
    # Disk commit
    # ------------------------------------------------------------------

    def commit_to_disk(self, root: str | Path) -> list[Path]:
        """Write all snapshots back to disk under ``root``.

        Returns the list of paths that were written.
        """
        root = Path(root)
        written: list[Path] = []
        for key, lines in self._snapshots.items():
            dest = root / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
            written.append(dest)
        return written

    def reset(self, key: str | None = None) -> None:
        """Clear staged hunks (and snapshots) for one file or all files."""
        if key:
            self._staged.pop(key, None)
            self._snapshots.pop(key, None)
            self._baselines.pop(key, None)
        else:
            self._staged.clear()
            self._snapshots.clear()
            self._baselines.clear()


# ---------------------------------------------------------------------------
# Conflict Detector
# ---------------------------------------------------------------------------

class ConflictDetector:
    """Checks a VFS's staged hunks for overlapping line ranges.

    Two hunks conflict when they target the same file and their *original*
    line ranges overlap — i.e. they both want to rewrite at least one of the
    same source lines.
    """

    def __init__(self, vfs: VirtualFileSystem) -> None:
        self._vfs = vfs

    def check(self) -> list[Conflict]:
        """Return all pairwise conflicts found across every staged file."""
        conflicts: list[Conflict] = []
        for file_path, hunks in self._vfs.all_staged_hunks().items():
            conflicts.extend(_find_overlaps(file_path, hunks))
        return conflicts

    def check_file(self, file_path: str) -> list[Conflict]:
        """Return conflicts for a single file."""
        hunks = self._vfs.staged_hunks(file_path)
        return _find_overlaps(file_path, hunks)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE_FROM   = re.compile(r"^--- (.+)")
_FILE_TO     = re.compile(r"^\+\+\+ (.+)")


def _parse_unified_diff(worker_id: str, diff_text: str) -> list[Hunk]:
    """Extract Hunk objects from a unified diff string."""
    hunks: list[Hunk] = []
    current_file: str = ""
    lines = diff_text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]

        # Detect target file from +++ header (strip a/ b/ git prefixes)
        m = _FILE_TO.match(line)
        if m:
            raw = m.group(1).strip()
            # strip git b/ prefix
            current_file = raw[2:] if raw.startswith("b/") else raw
            # normalise to forward slashes
            current_file = current_file.replace("\\", "/")
            i += 1
            continue

        # Hunk header
        m = _HUNK_HEADER.match(line)
        if m:
            orig_start = int(m.group(1))
            orig_count = int(m.group(2)) if m.group(2) is not None else 1
            i += 1
            new_lines: list[str] = []
            while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("--- ") and not lines[i].startswith("+++ "):
                body = lines[i]
                if body.startswith("+"):
                    new_lines.append(body[1:])
                elif body.startswith(" "):
                    new_lines.append(body[1:])
                # lines starting with "-" are removed — not added to new_lines
                i += 1
            hunks.append(
                Hunk(
                    worker_id=worker_id,
                    file_path=current_file,
                    orig_start=orig_start,
                    orig_count=orig_count,
                    new_lines=new_lines,
                )
            )
            continue

        i += 1

    return hunks


def _replay_hunks(baseline: list[str], hunks: list[Hunk]) -> list[str]:
    """Apply *hunks* in order to *baseline*, returning the resulting lines.

    All hunk ``orig_start`` values are treated as offsets into *baseline*
    (1-based).  A running ``offset`` accumulates the line-count delta from
    each applied hunk so that subsequent hunks land at the correct position
    in the evolving line list.
    """
    lines = baseline
    offset = 0  # cumulative shift: lines added minus lines removed so far
    for hunk in sorted(hunks, key=lambda h: h.orig_start):
        start = hunk.orig_start - 1 + offset   # convert to 0-based + shift
        end = start + hunk.orig_count
        lines = lines[:start] + hunk.new_lines + lines[end:]
        offset += len(hunk.new_lines) - hunk.orig_count
    return lines


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Return True if [a_start, a_end] and [b_start, b_end] overlap."""
    return a_start <= b_end and b_start <= a_end


def _find_overlaps(file_path: str, hunks: list[Hunk]) -> list[Conflict]:
    conflicts: list[Conflict] = []
    for idx, a in enumerate(hunks):
        for b in hunks[idx + 1:]:
            if a.worker_id == b.worker_id:
                continue  # same worker — not a cross-worker conflict
            if _ranges_overlap(a.orig_start, a.orig_end, b.orig_start, b.orig_end):
                conflicts.append(Conflict(file_path=file_path, hunk_a=a, hunk_b=b))
    return conflicts
