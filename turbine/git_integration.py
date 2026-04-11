"""Git Integration — Phase 7.

Wraps a small subset of git operations against the *project under repair*.
All operations are silently skipped (returning safe defaults) when the
target directory is not inside a git repository, so Turbine works equally
well on bare or zip-extracted projects.

Public surface
--------------
GitIntegration
    Scoped git wrapper.  Instantiate once per run, call in order:
        1. preflight()      — check for uncommitted changes
        2. create_branch()  — create turbine/run-<ts> and check it out
        3. auto_commit()    — stage + commit the files Turbine wrote
        4. diff_hint()      — shell command to review the commit
        5. undo_hint()      — shell command to revert the commit

build_commit_message(diagnosis, tickets, user_request)
    Build the structured commit message from Manager output.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol


# ---------------------------------------------------------------------------
# Protocol — avoids a circular import with turbine.manager.Ticket
# ---------------------------------------------------------------------------

class _TicketLike(Protocol):
    id: str
    description: str


# ---------------------------------------------------------------------------
# PreflightResult
# ---------------------------------------------------------------------------

@dataclass
class PreflightResult:
    """Outcome of the dirty-tree check."""
    is_repo: bool
    dirty_files: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.dirty_files


# ---------------------------------------------------------------------------
# GitIntegration
# ---------------------------------------------------------------------------

class GitIntegration:
    """Lightweight git wrapper scoped to *project_root*.

    Parameters
    ----------
    project_root:
        Absolute path to the project Turbine is repairing.  Every ``git``
        subprocess call uses ``-C <project_root>`` so it is scoped entirely
        to that repository and cannot affect any other repo on the machine.
    no_branch:
        When ``True``, skip branch creation.  Auto-commit still runs on
        whatever branch the user is currently on.
    """

    def __init__(self, project_root: str | Path, no_branch: bool = False) -> None:
        self._root = Path(project_root)
        self._no_branch = no_branch
        self._branch: str | None = None
        self._committed: bool = False
        self._is_repo: bool = self._detect_repo()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_repo(self) -> bool:
        """``True`` if *project_root* is inside a git repository."""
        return self._is_repo

    @property
    def branch(self) -> str | None:
        """The branch created by :meth:`create_branch`, or ``None``."""
        return self._branch

    @property
    def committed(self) -> bool:
        """``True`` after a successful :meth:`auto_commit` call."""
        return self._committed

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def preflight(self) -> PreflightResult:
        """Check for uncommitted changes in the working tree.

        Returns a :class:`PreflightResult` whose ``dirty_files`` list is
        non-empty when there are staged or unstaged changes.  Always returns
        an empty list (not an error) when the target is not a git repo.
        """
        if not self._is_repo:
            return PreflightResult(is_repo=False)
        result = self._run("status", "--porcelain")
        dirty: list[str] = []
        for line in result.stdout.splitlines():
            if line.strip():
                # "XY filename" — strip the two-character status code + space
                dirty.append(line[3:].strip())
        return PreflightResult(is_repo=True, dirty_files=dirty)

    def create_branch(self, timestamp: str | None = None) -> str | None:
        """Create and check out a ``turbine/run-<timestamp>`` branch.

        Returns the branch name on success, or ``None`` when creation is
        skipped (not a repo, or ``--no-branch`` was requested).

        Raises
        ------
        RuntimeError
            If ``git checkout -b`` fails (e.g. no initial commit yet, or a
            branch with that name already exists).
        """
        if not self._is_repo or self._no_branch:
            return None
        ts = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
        self._branch = f"turbine/run-{ts}"
        result = self._run("checkout", "-b", self._branch)
        if result.returncode != 0:
            raise RuntimeError(
                f"git checkout -b {self._branch!r} failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return self._branch

    def auto_commit(self, files: list[str], message: str) -> bool:
        """Stage *files* and create a git commit with *message*.

        Only the supplied relative paths are staged (``git add -- <files>``),
        so pre-existing dirty changes in the working tree are not accidentally
        included in Turbine's commit.

        Parameters
        ----------
        files:
            Relative paths (from *project_root*) of every file Turbine wrote.
        message:
            Commit message — typically built with :func:`build_commit_message`.

        Returns
        -------
        bool
            ``True`` if the commit was created successfully.
        """
        if not self._is_repo or not files:
            return False
        add = self._run("add", "--", *files)
        if add.returncode != 0:
            return False
        commit = self._run("commit", "-m", message)
        if commit.returncode == 0:
            self._committed = True
            return True
        return False

    def diff_hint(self) -> str:
        """Shell command to review what Turbine committed.

        Returns an empty string when the target is not a git repo or no
        commit has been made yet.
        """
        if not self._is_repo or not self._committed:
            return ""
        return f"git -C {self._root} show HEAD"

    def undo_hint(self) -> str:
        """Shell command to revert Turbine's commit cleanly.

        Returns an empty string when the target is not a git repo or no
        commit has been made yet.
        """
        if not self._is_repo or not self._committed:
            return ""
        return f"git -C {self._root} revert HEAD --no-edit"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self._root), *args],
            capture_output=True,
            text=True,
        )

    def _detect_repo(self) -> bool:
        """Return ``True`` if *project_root* is inside a git repository."""
        result = self._run("rev-parse", "--is-inside-work-tree")
        return result.returncode == 0


# ---------------------------------------------------------------------------
# Commit message builder
# ---------------------------------------------------------------------------

def build_commit_message(
    diagnosis: str,
    tickets: list[_TicketLike],
    user_request: str = "",
) -> str:
    """Build a structured git commit message from Manager output.

    Subject line: ``turbine: <first sentence of diagnosis>``
    Body:         full diagnosis + ticket list.

    Parameters
    ----------
    diagnosis:
        The Manager's diagnosis string (Step 3 output).
    tickets:
        The decomposed tickets (Step 3 output).
    user_request:
        The original user request, used as a fallback subject and included
        in the body.

    Returns
    -------
    str
        A complete git commit message (subject + blank line + body).
    """
    # --- Subject line ---
    first_sentence = ""
    if diagnosis:
        # Take up to the first full stop or newline, cap at 60 chars
        raw = diagnosis.split(".")[0].split("\n")[0].strip()
        first_sentence = raw[:60] + ("…" if len(raw) > 60 else "")
    if not first_sentence and user_request:
        first_sentence = user_request[:60] + ("…" if len(user_request) > 60 else "")
    subject = f"turbine: {first_sentence}" if first_sentence else "turbine: automated repair"

    # --- Body ---
    body_parts: list[str] = []
    if user_request:
        body_parts.append(f"Request: {user_request}")
    if diagnosis:
        body_parts.append(f"Diagnosis:\n{diagnosis}")
    if tickets:
        lines = "\n".join(f"  - {t.id}: {t.description}" for t in tickets)
        body_parts.append(f"Tickets:\n{lines}")
    body = "\n\n".join(body_parts)

    return f"{subject}\n\n{body}" if body else subject
