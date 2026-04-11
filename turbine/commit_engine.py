"""Commit Engine — Phase 5a / 6.

Writes VFS in-memory snapshots to disk once all workers have reached
consensus (i.e. no staged conflicts remain).

The engine operates in two modes:

* **dry_run=True** — computes what *would* be written and returns a summary
  without touching any file.
* **dry_run=False** — actually writes every modified snapshot to disk.

Additionally, the engine supports a **manual review gate** (Phase 6):

* ``review_and_commit()`` renders a coloured unified-diff preview of every
  changed file using Rich, then prompts ``[y/N]`` before writing.  If the
  user declines, the commit is aborted and a ``CommitResult`` with
  ``written_count=0`` is returned.

Only files that were changed by at least one worker are written; unmodified
snapshots are skipped.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text

from turbine.logger import TurbineLogger
from turbine.vfs import VirtualFileSystem


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FileCommitResult:
    """Outcome for a single file."""
    relative_path: str
    abs_path: Path
    written: bool           # True if the file was (or would be) written
    dry_run: bool
    lines_before: int
    lines_after: int
    error: str = ""

    @property
    def changed(self) -> bool:
        return self.lines_before != self.lines_after

    def __str__(self) -> str:
        mode = "[DRY-RUN] " if self.dry_run else ""
        status = "WRITTEN" if self.written else ("SKIPPED (no change)" if not self.error else f"ERROR: {self.error}")
        return f"{mode}{self.relative_path}: {status} ({self.lines_before} → {self.lines_after} lines)"


@dataclass
class CommitResult:
    """Aggregate outcome of a commit run."""
    dry_run: bool
    files: list[FileCommitResult] = field(default_factory=list)

    @property
    def written_count(self) -> int:
        return sum(1 for f in self.files if f.written)

    @property
    def skipped_count(self) -> int:
        return sum(1 for f in self.files if not f.written and not f.error)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.files if f.error)

    @property
    def success(self) -> bool:
        return self.error_count == 0

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "COMMIT"
        return (
            f"[{mode}] {self.written_count} written, "
            f"{self.skipped_count} skipped, "
            f"{self.error_count} error(s)."
        )


# ---------------------------------------------------------------------------
# CommitEngine
# ---------------------------------------------------------------------------

class CommitEngine:
    """Writes approved VFS snapshots to disk.

    Parameters
    ----------
    vfs:
        The ``VirtualFileSystem`` holding the approved snapshots.
    project_root:
        Absolute path to the root of the project being modified.
    dry_run:
        When ``True`` (default), simulate the writes without touching disk.
    original_snapshots:
        Optional mapping of ``{relative_key: original_lines}`` captured
        *before* any diffs were applied.  Used to skip files whose content
        has not actually changed.  If omitted, all snapshots are written.
    """

    def __init__(
        self,
        vfs: VirtualFileSystem,
        project_root: str | Path,
        dry_run: bool = True,
        original_snapshots: dict[str, list[str]] | None = None,
    ) -> None:
        self._vfs = vfs
        self._root = Path(project_root)
        self._dry_run = dry_run
        self._originals = original_snapshots or {}
        self.log = TurbineLogger()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def commit(self) -> CommitResult:
        """Execute the commit (or dry-run) and return a :class:`CommitResult`."""
        result = CommitResult(dry_run=self._dry_run)

        for rel_key, new_lines in self._vfs.all_snapshots().items():
            orig_lines = self._originals.get(rel_key)
            abs_path = self._root / rel_key

            # Determine if the file actually changed
            changed = (orig_lines is None) or (orig_lines != new_lines)

            if not changed:
                result.files.append(FileCommitResult(
                    relative_path=rel_key,
                    abs_path=abs_path,
                    written=False,
                    dry_run=self._dry_run,
                    lines_before=len(orig_lines) if orig_lines else 0,
                    lines_after=len(new_lines),
                ))
                continue

            lines_before = len(orig_lines) if orig_lines is not None else 0
            lines_after = len(new_lines)

            if self._dry_run:
                self.log.thinking(
                    f"[DRY-RUN] Would write {rel_key} "
                    f"({lines_before} → {lines_after} lines)"
                )
                result.files.append(FileCommitResult(
                    relative_path=rel_key,
                    abs_path=abs_path,
                    written=True,   # "would be written"
                    dry_run=True,
                    lines_before=lines_before,
                    lines_after=lines_after,
                ))
            else:
                try:
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    abs_path.write_text(
                        "\n".join(new_lines) + "\n", encoding="utf-8"
                    )
                    self.log.action(
                        f"Written {rel_key} ({lines_before} → {lines_after} lines)"
                    )
                    result.files.append(FileCommitResult(
                        relative_path=rel_key,
                        abs_path=abs_path,
                        written=True,
                        dry_run=False,
                        lines_before=lines_before,
                        lines_after=lines_after,
                    ))
                except OSError as exc:
                    self.log.error(f"Failed to write {rel_key}: {exc}")
                    result.files.append(FileCommitResult(
                        relative_path=rel_key,
                        abs_path=abs_path,
                        written=False,
                        dry_run=False,
                        lines_before=lines_before,
                        lines_after=lines_after,
                        error=str(exc),
                    ))

        mode = "DRY-RUN" if self._dry_run else "COMMIT"
        self.log.action(f"[{mode}] {result.summary()}")
        return result

    # ------------------------------------------------------------------
    # Phase 6: Manual review gate
    # ------------------------------------------------------------------

    def review_and_commit(
        self,
        confirm_fn: Callable[[str], bool] | None = None,
    ) -> CommitResult:
        """Show a unified-diff preview, prompt for confirmation, then commit.

        Parameters
        ----------
        confirm_fn:
            Optional callable that receives the prompt string and returns
            ``True`` to proceed or ``False`` to abort.  Defaults to reading
            from stdin (``input()``).  Inject a custom function in tests to
            avoid interactive prompts.

        Returns
        -------
        CommitResult
            If the user declines, ``written_count`` will be 0 and all file
            results will have ``written=False``.
        """
        console = Console()
        changed = self._collect_changed_files()

        if not changed:
            self.log.action("Review gate: no changes to review.")
            return CommitResult(dry_run=self._dry_run)

        # Render diff preview
        console.rule("[bold blue]Turbine — Diff Preview[/bold blue]")
        for rel_key, orig_lines, new_lines in changed:
            diff_lines = list(difflib.unified_diff(
                [l + "\n" for l in orig_lines],
                [l + "\n" for l in new_lines],
                fromfile=f"a/{rel_key}",
                tofile=f"b/{rel_key}",
            ))
            if diff_lines:
                diff_text = "".join(diff_lines)
                console.print(
                    Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
                )
            else:
                console.print(f"[dim]{rel_key}: (no textual diff)[/dim]")

        console.rule()
        summary_text = Text()
        summary_text.append(f"{len(changed)} file(s) will be modified.", style="bold")
        console.print(summary_text)

        # Confirm
        _confirm = confirm_fn or _stdin_confirm
        if not _confirm("Proceed with commit? [y/N] "):
            self.log.action("Review gate: commit aborted by user.")
            result = CommitResult(dry_run=self._dry_run)
            for rel_key, orig, new in changed:
                result.files.append(FileCommitResult(
                    relative_path=rel_key,
                    abs_path=self._root / rel_key,
                    written=False,
                    dry_run=self._dry_run,
                    lines_before=len(orig),
                    lines_after=len(new),
                    error="aborted by user",
                ))
            return result

        # User confirmed — run the real commit (with dry_run=False override)
        orig_dry = self._dry_run
        self._dry_run = False
        result = self.commit()
        self._dry_run = orig_dry
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_changed_files(
        self,
    ) -> list[tuple[str, list[str], list[str]]]:
        """Return [(rel_key, orig_lines, new_lines)] for modified files."""
        changed = []
        for rel_key, new_lines in self._vfs.all_snapshots().items():
            orig_lines = self._originals.get(rel_key)
            if orig_lines is None or orig_lines != new_lines:
                changed.append((rel_key, orig_lines or [], new_lines))
        return changed


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _stdin_confirm(prompt: str) -> bool:
    """Read y/n from stdin. Returns True only for 'y' or 'yes'."""
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")
