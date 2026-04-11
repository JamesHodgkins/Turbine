"""Tests for turbine.worker — Phase 4 Worker loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turbine.manager import Ticket, WorkerResult
from turbine.vfs import VirtualFileSystem
from turbine.worker import Worker, _extract_diff, CONSTRAINT_TEMPLATE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIMPLE_DIFF = """\
--- a/src/main.py
+++ b/src/main.py
@@ -1,3 +1,4 @@
 def main():
+    print("hello")
     pass
"""


def _make_vfs(files: dict[str, str]) -> VirtualFileSystem:
    vfs = VirtualFileSystem()
    for key, content in files.items():
        vfs.load_text(key, content)
    return vfs


def _make_token_manager(fits: bool = True) -> MagicMock:
    tm = MagicMock()
    tm.fits.return_value = fits
    return tm


def _make_client(response_text: str) -> MagicMock:
    client = MagicMock()
    client.chat.complete_async = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content=response_text))]
        )
    )
    return client


def _make_worker(
    ticket: Ticket,
    vfs: VirtualFileSystem,
    response_text: str = "",
    fits: bool = True,
    max_handshake_attempts: int = 3,
    max_api_retries: int = 1,
) -> Worker:
    return Worker(
        ticket=ticket,
        vfs=vfs,
        client=_make_client(response_text),
        model="mistral-large-latest",
        token_manager=_make_token_manager(fits),
        max_handshake_attempts=max_handshake_attempts,
        max_api_retries=max_api_retries,
    )


# ---------------------------------------------------------------------------
# _extract_diff
# ---------------------------------------------------------------------------

class TestExtractDiff:
    def test_plain_diff(self):
        result = _extract_diff(SIMPLE_DIFF)
        assert result.startswith("---")
        assert "+++ b/src/main.py" in result

    def test_diff_wrapped_in_prose(self):
        text = "Here is my change:\n" + SIMPLE_DIFF + "\nDone."
        result = _extract_diff(text)
        assert result.startswith("---")

    def test_diff_in_markdown_fence(self):
        text = "```diff\n" + SIMPLE_DIFF + "\n```"
        result = _extract_diff(text)
        assert "+++" in result
        assert "```" not in result

    def test_no_diff_returns_empty(self):
        assert _extract_diff("No changes needed.") == ""

    def test_git_diff_header(self):
        text = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x\n+y\n"
        result = _extract_diff(text)
        assert result.startswith("diff --git")


# ---------------------------------------------------------------------------
# Worker.run — happy path
# ---------------------------------------------------------------------------

class TestWorkerRun:
    def _ticket(self, files: list[str] | None = None) -> Ticket:
        return Ticket(
            id="ticket-1",
            description="Add a print statement",
            relevant_files=files or ["src/main.py"],
        )

    def test_empty_diff_succeeds(self):
        vfs = _make_vfs({"src/main.py": "def main(): pass\n"})
        worker = _make_worker(self._ticket(), vfs, response_text="No changes needed.")
        result = asyncio.run(worker.run())
        assert result.success
        assert result.proposed_diff == ""

    def test_valid_diff_approved_and_staged(self):
        vfs = _make_vfs({"src/main.py": "def main():\n    pass\n"})
        diff = (
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def main():\n"
            "+    print('hi')\n"
            "     pass\n"
        )
        worker = _make_worker(self._ticket(), vfs, response_text=diff)
        result = asyncio.run(worker.run())
        assert result.success
        assert result.proposed_diff != ""

    def test_missing_vfs_snapshot_logs_and_skips(self):
        vfs = VirtualFileSystem()   # no snapshots loaded
        ticket = self._ticket(files=["missing.py"])
        worker = _make_worker(ticket, vfs, response_text="No changes needed.")
        result = asyncio.run(worker.run())
        # Worker still completes (empty file contents → model says no change)
        assert result.success

    def test_api_error_returns_failure(self):
        vfs = _make_vfs({"src/main.py": "x = 1\n"})
        worker = _make_worker(self._ticket(), vfs, max_api_retries=1)
        worker.client.chat.complete_async = AsyncMock(side_effect=RuntimeError("boom"))
        result = asyncio.run(worker.run())
        assert not result.success
        assert "boom" in result.error


# ---------------------------------------------------------------------------
# Handshake: conflict detection and retry
# ---------------------------------------------------------------------------

class TestHandshake:
    def _two_ticket_vfs(self) -> tuple[VirtualFileSystem, Ticket, Ticket]:
        """VFS with one file, two tickets targeting overlapping lines."""
        vfs = _make_vfs({"f.py": "a\nb\nc\nd\ne\n"})
        t1 = Ticket(id="ticket-1", description="T1", relevant_files=["f.py"])
        t2 = Ticket(id="ticket-2", description="T2", relevant_files=["f.py"])
        return vfs, t1, t2

    def test_second_worker_receives_constraint_on_conflict(self):
        """Stage a diff for ticket-1 first, then run ticket-2 on the same lines."""
        vfs, t1, t2 = self._two_ticket_vfs()

        # Stage ticket-1's diff directly
        diff_t1 = (
            "--- a/f.py\n+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-a\n+A\n b\n"
        )
        vfs.apply_diff("ticket-1", diff_t1)

        # ticket-2 tries to modify the same line 1 on first attempt,
        # then backs off with an empty response on the second attempt.
        diff_t2_conflict = (
            "--- a/f.py\n+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-a\n+Z\n b\n"
        )
        client = MagicMock()
        client.chat.complete_async = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=MagicMock(content=diff_t2_conflict))]),
            MagicMock(choices=[MagicMock(message=MagicMock(content="No changes needed."))]),
        ])
        worker = Worker(
            ticket=t2,
            vfs=vfs,
            client=client,
            model="mistral-large-latest",
            token_manager=_make_token_manager(fits=True),
            max_handshake_attempts=3,
            max_api_retries=1,
        )
        result = asyncio.run(worker.run())
        # After conflict, worker retries and produces empty diff → success
        assert result.success

    def test_exceeds_max_attempts_returns_failure(self):
        """Worker that always proposes a conflicting diff should eventually fail."""
        vfs, t1, t2 = self._two_ticket_vfs()

        # Pre-stage ticket-1
        diff_t1 = (
            "--- a/f.py\n+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-a\n+A\n b\n"
        )
        vfs.apply_diff("ticket-1", diff_t1)

        # ticket-2 always proposes the same conflicting diff
        conflicting_diff = (
            "--- a/f.py\n+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-a\n+Z\n b\n"
        )
        worker = _make_worker(
            t2, vfs,
            response_text=conflicting_diff,
            max_handshake_attempts=2,
        )
        result = asyncio.run(worker.run())
        assert not result.success
        assert "failed" in result.error.lower()


# ---------------------------------------------------------------------------
# Context window pruning
# ---------------------------------------------------------------------------

class TestPruneMessages:
    def _worker(self, fits_sequence: list[bool]) -> Worker:
        vfs = _make_vfs({})
        ticket = Ticket(id="t", description="x", relevant_files=[])
        tm = MagicMock()
        tm.fits.side_effect = fits_sequence + [True] * 100
        return Worker(
            ticket=ticket,
            vfs=vfs,
            client=MagicMock(),
            model="mistral-large-latest",
            token_manager=tm,
        )

    def test_no_pruning_when_fits(self):
        worker = self._worker([True])
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user",   "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user",   "content": "u2"},
        ]
        result = worker._prune_messages(messages)
        assert len(result) == 4

    def test_prunes_oldest_turns_when_overflow(self):
        # First call: doesn't fit; second: fits
        worker = self._worker([False, True])
        messages = [
            {"role": "system",    "content": "sys"},
            {"role": "user",      "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user",      "content": "u2"},
        ]
        result = worker._prune_messages(messages)
        # System prompt must be preserved
        assert result[0]["role"] == "system"
        assert len(result) < 4

    def test_never_drops_below_system_plus_one(self):
        # Always overflows — should stop at [system, last_user]
        worker = self._worker([False] * 20)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user",   "content": "u1"},
        ]
        result = worker._prune_messages(messages)
        assert result[0]["role"] == "system"
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class TestRetry:
    def test_retries_on_retryable_status(self):
        vfs = _make_vfs({"f.py": "x\n"})
        ticket = Ticket(id="t", description="d", relevant_files=["f.py"])

        exc = Exception("rate limit")
        exc.status_code = 429  # type: ignore[attr-defined]

        success_response = MagicMock(
            choices=[MagicMock(message=MagicMock(content="No changes needed."))]
        )
        client = MagicMock()
        client.chat.complete_async = AsyncMock(
            side_effect=[exc, success_response]
        )

        worker = Worker(
            ticket=ticket,
            vfs=vfs,
            client=client,
            model="mistral-large-latest",
            token_manager=_make_token_manager(),
            max_api_retries=3,
        )
        # Patch sleep so tests don't actually wait
        with patch("turbine.worker.asyncio.sleep", new_callable=AsyncMock):
            result = asyncio.run(worker.run())

        assert result.success

    def test_non_retryable_error_fails_immediately(self):
        vfs = _make_vfs({"f.py": "x\n"})
        ticket = Ticket(id="t", description="d", relevant_files=["f.py"])

        exc = ValueError("bad request")
        # No status_code → treated as non-retryable

        client = MagicMock()
        client.chat.complete_async = AsyncMock(side_effect=exc)

        worker = Worker(
            ticket=ticket,
            vfs=vfs,
            client=client,
            model="mistral-large-latest",
            token_manager=_make_token_manager(),
            max_api_retries=3,
        )
        result = asyncio.run(worker.run())
        assert not result.success
        # Should have only called the API once
        assert client.chat.complete_async.call_count == 1
