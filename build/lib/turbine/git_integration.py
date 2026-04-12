"""Git Integration — Phase 7 / Phase 22.

Wraps a small subset of git operations against the *project under repair*.
All operations are silently skipped (returning safe defaults) when the
target directory is not inside a git repository, so Turbine works equally
well on bare or zip-extracted projects.

Public surface
--------------
GitIntegration
    Scoped git wrapper.  Instantiate once per run, call in order:
        1. acquire_lock()   — create .turbine.lock; abort if another run is active
        2. preflight()      — check for uncommitted changes
        3. create_branch()  — create/resume a turbine branch and check it out
        4. auto_commit()    — stage + commit the files Turbine wrote
        5. merge_hint()     — shell command to merge the turbine branch
        6. undo_hint()      — shell command to revert the commit
        7. release_lock()   — remove .turbine.lock on clean exit or crash recovery

build_commit_message(diagnosis, tickets, user_request)
    Build the structured commit message from Manager output.
"""

from __future__ import annotations

import os
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

    def __init__(
        self,
        project_root: str | Path,
        no_branch: bool = False,
        chat_id: str | None = None,
        new_chat: bool = False,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._no_branch = no_branch
        self._chat_id = chat_id
        self._new_chat = new_chat
        self._branch: str | None = None
        self._original_branch: str | None = None
        self._committed: bool = False
        self._resumed: bool = False
        self._disk_changed: bool = False
        self._is_subdir_of_repo: bool = False  # set by _detect_repo
        self._is_repo: bool = self._detect_repo()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_repo(self) -> bool:
        """``True`` if *project_root* is the root of a git repository."""
        return self._is_repo

    @property
    def is_subdir_of_repo(self) -> bool:
        """``True`` when the target is a subdirectory of a larger git repo.

        In this case git integration is intentionally disabled to prevent
        ``git status`` from leaking files from the enclosing repo into the
        dirty-file listing, and to prevent branch/commit operations from
        affecting the wrong repository scope.
        """
        return self._is_subdir_of_repo

    @property
    def branch(self) -> str | None:
        """The branch created by :meth:`create_branch`, or ``None``."""
        return self._branch

    @property
    def committed(self) -> bool:
        """``True`` after a successful :meth:`auto_commit` call."""
        return self._committed

    @property
    def resumed(self) -> bool:
        """``True`` when :meth:`create_branch` checked out an existing branch."""
        return self._resumed

    @property
    def disk_changed(self) -> bool:
        """``True`` when :meth:`create_branch` changed disk state (new-chat base-branch reset)."""
        return self._disk_changed

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire_lock(self) -> None:
        """Create ``.turbine.lock`` in the project root.

        Raises
        ------
        RuntimeError
            If the lock file already exists, indicating another Turbine
            process is active on this project.  Remove the file manually
            if no process is running (e.g. after a hard crash).
        """
        lock_path = self._root / ".turbine.lock"
        if lock_path.exists():
            try:
                pid = lock_path.read_text().strip()
            except OSError:
                pid = "unknown"
            raise RuntimeError(
                f"Turbine is already running on this project "
                f"(pid {pid}; lock: {lock_path}). "
                "Remove .turbine.lock manually if no process is active."
            )
        lock_path.write_text(str(os.getpid()))

    def release_lock(self) -> None:
        """Remove ``.turbine.lock`` if it exists (clean exit or crash recovery)."""
        (self._root / ".turbine.lock").unlink(missing_ok=True)

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

    def create_branch(
        self,
        timestamp: str | None = None,
        preflight: "PreflightResult | None" = None,
    ) -> str | None:
        """Create or resume a Turbine branch and check it out.

        **With ``chat_id``** (set on the constructor):

        - *Clean tree* → try to resume an existing ``turbine/chat-{chat_id}``
          branch; if not found, create it from HEAD.
        - *Dirty tree* → skip any existing chat branch and create a new
          ``turbine/chat-{chat_id}`` branch from the current disk state,
          capturing manual edits as the new baseline.

        **Without ``chat_id``** (legacy / run-mode):

        - Always create a new ``turbine/run-{timestamp}`` branch.

        Parameters
        ----------
        timestamp:
            Optional fixed timestamp string (used in tests to produce
            deterministic branch names when *chat_id* is not set).
        preflight:
            Result of a prior :meth:`preflight` call.  When supplied and
            the tree is dirty, resume logic is skipped even if *chat_id*
            is set.

        Returns
        -------
        str | None
            The branch name, or ``None`` when branch creation is skipped.

        Raises
        ------
        RuntimeError
            If ``git checkout`` fails.
        """
        if not self._is_repo or self._no_branch:
            return None
        # Record where we are so we can return here if nothing is committed.
        ref = self._run("rev-parse", "--abbrev-ref", "HEAD")
        if ref.returncode == 0:
            self._original_branch = ref.stdout.strip()

        if self._chat_id:
            branch_name = f"turbine/chat-{self._chat_id}"
            dirty = preflight is not None and not preflight.is_clean
            if not dirty and not self._new_chat:
                # Try to resume an existing chat branch (clean tree, same session)
                check = self._run("rev-parse", "--verify", branch_name)
                if check.returncode == 0:
                    result = self._run("checkout", branch_name)
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"git checkout {branch_name!r} failed: "
                            f"{result.stderr.strip() or result.stdout.strip()}"
                        )
                    self._branch = branch_name
                    self._resumed = True
                    return self._branch

            if self._new_chat:
                # New chat: return to base branch so turbine-committed files
                # from the previous session are removed from disk, giving the
                # LLM a clean view of the user's real project state.
                # We stash any tracked-file edits first so the checkout succeeds.
                base = self._find_base_branch()
                current = self._original_branch or ""
                if base and current != base:
                    stash = self._run("stash", "push", "-m", "turbine-new-chat")
                    stashed = stash.returncode == 0 and "No local changes" not in stash.stdout
                    co = self._run("checkout", base)
                    if co.returncode == 0:
                        self._original_branch = base
                        result = self._run("checkout", "-b", branch_name)
                        if stashed:
                            self._run("stash", "pop")
                        if result.returncode != 0:
                            raise RuntimeError(
                                f"git checkout -b {branch_name!r} failed: "
                                f"{result.stderr.strip() or result.stdout.strip()}"
                            )
                        self._branch = branch_name
                        self._disk_changed = True
                        return self._branch
                    else:
                        # Could not return to base; restore and fall through
                        if stashed:
                            self._run("stash", "pop")

            # Create new branch from current HEAD (same-chat dirty case, or
            # new-chat fallback when base checkout failed).
            result = self._run("checkout", "-b", branch_name)
            if result.returncode != 0:
                raise RuntimeError(
                    f"git checkout -b {branch_name!r} failed: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            self._branch = branch_name
        else:
            ts = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
            self._branch = f"turbine/run-{ts}"
            result = self._run("checkout", "-b", self._branch)
            if result.returncode != 0:
                raise RuntimeError(
                    f"git checkout -b {self._branch!r} failed: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            return self._branch
        return self._branch

    def cleanup_if_empty(self) -> bool:
        """Return to the original branch and delete the turbine branch if nothing was committed.

        Called at the end of a run when no files were written, so abandoned
        turbine branches don't accumulate in the repository.

        Returns ``True`` if cleanup succeeded, ``False`` if skipped or failed.
        """
        if not self._is_repo or not self._branch or self._committed:
            return False
        if not self._original_branch or self._original_branch == self._branch:
            return False
        checkout = self._run("checkout", self._original_branch)
        if checkout.returncode != 0:
            return False
        # -d requires the branch to be fully merged; use -D as fallback for
        # branches that were created from a detached HEAD or other edge cases.
        delete = self._run("branch", "-d", self._branch)
        if delete.returncode != 0:
            delete = self._run("branch", "-D", self._branch)
        if delete.returncode == 0:
            self._branch = None
            return True
        return False

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

    def merge_hint(self) -> str:
        """Shell command to merge the turbine branch into the original branch.

        Returns an empty string when no branch was created, or when turbine
        committed directly to the original branch (``--no-branch`` mode).
        """
        if not self._is_repo or not self._committed:
            return ""
        if not self._branch or not self._original_branch:
            return ""
        if self._branch == self._original_branch:
            return ""
        return (
            f"git -C {self._root} checkout {self._original_branch} "
            f"&& git -C {self._root} merge {self._branch}"
        )

    def undo_hint(self) -> str:
        """Shell command to revert Turbine's commit cleanly.

        Returns an empty string when the target is not a git repo or no
        commit has been made yet.
        """
        if not self._is_repo or not self._committed:
            return ""
        return f"git -C {self._root} revert HEAD --no-edit"

    def purge_history(self) -> list[str]:
        """Force-delete all local ``turbine/`` branches and return to a safe branch.

        If the current branch is itself a turbine branch, this method checks
        out the project's base branch (``main`` or ``master``) before deleting.

        Returns
        -------
        list[str]
            Names of every branch that was successfully deleted.

        Raises
        ------
        RuntimeError
            If the target is not a git repository.
        """
        if not self._is_repo:
            raise RuntimeError(
                "purge_history requires a git repository; "
                f"{self._root} is not one."
            )

        # Find current branch
        ref = self._run("rev-parse", "--abbrev-ref", "HEAD")
        current = ref.stdout.strip() if ref.returncode == 0 else ""

        # If we're on a turbine branch, move to a safe base branch first
        if current.startswith("turbine/"):
            base = self._find_base_branch()
            if base:
                self._run("checkout", base)
            else:
                self._run("checkout", "--detach", "HEAD")

        # List all turbine/* branches
        listed = self._run(
            "branch", "--list", "turbine/*", "--format=%(refname:short)"
        )
        branches = [b.strip() for b in listed.stdout.splitlines() if b.strip()]

        deleted: list[str] = []
        for branch in branches:
            result = self._run("branch", "-D", branch)
            if result.returncode == 0:
                deleted.append(branch)
        return deleted

    def restore_working_tree(self) -> bool:
        """Revert all unstaged working tree changes to match HEAD.

        Runs ``git checkout -- .`` to discard modifications.  This is used
        for atomic rollback: when ``CommitEngine`` partially writes files
        and a subsequent step fails, this method restores every tracked file
        to the last committed state so the disk is never left half-written.

        Returns ``True`` if the checkout succeeded, ``False`` otherwise
        (including when the target is not a git repo).
        """
        if not self._is_repo:
            return False
        result = self._run("checkout", "--", ".")
        return result.returncode == 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_base_branch(self) -> str | None:
        """Return the name of the project's base branch (``main`` or ``master``).

        Tries ``main`` first, then ``master``.  Returns ``None`` if neither
        exists (e.g. a brand-new repo with only a single commit).
        """
        for candidate in ("main", "master"):
            result = self._run("rev-parse", "--verify", candidate)
            if result.returncode == 0:
                return candidate
        return None

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self._root), *args],
            capture_output=True,
            text=True,
        )

    def _detect_repo(self) -> bool:
        """Return ``True`` if *project_root* IS the root of a git repository.

        A project that is a *subdirectory* of a larger repo is treated as
        "not a repo" so that git operations (status, branch, commit) don't
        bleed into the enclosing repo.  Running ``turbine ./subdir "…"``
        against a subdirectory would otherwise show dirty files from the
        entire parent repo and create branches at the wrong scope.

        Sets ``self._is_subdir_of_repo`` as a side-effect so callers can
        emit a helpful diagnostic when git integration is silently skipped.
        """
        result = self._run("rev-parse", "--is-inside-work-tree")
        if result.returncode != 0:
            return False
        toplevel = self._run("rev-parse", "--show-toplevel")
        if toplevel.returncode != 0:
            return False
        repo_root = Path(toplevel.stdout.strip()).resolve()
        if repo_root == self._root:
            return True
        # project_root is a subdirectory of a git repo — flag it and bail.
        self._is_subdir_of_repo = True
        return False


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
