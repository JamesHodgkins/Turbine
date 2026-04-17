"""Tests for turbine.worker — Phase 4 Worker loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turbine.manager import Ticket, WorkerResult
from turbine.vfs import VirtualFileSystem
from turbine.worker import Worker, _extract_file_blocks, CONSTRAINT_TEMPLATE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SIMPLE_FILE_BLOCK = """\
<file path="src/main.py">
def main():
    print("hello")
    pass
</file>
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
    client.chat.stream_async = None  # disable streaming path in tests
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
# _extract_file_blocks
# ---------------------------------------------------------------------------

class TestExtractFileBlocks:
    def test_single_block(self):
        result = _extract_file_blocks(SIMPLE_FILE_BLOCK)
        assert "src/main.py" in result
        assert 'print("hello")' in result["src/main.py"]

    def test_multiple_blocks(self):
        text = (
            '<file path="a.py">x = 1\n</file>\n'
            '<file path="b.py">y = 2\n</file>\n'
        )
        result = _extract_file_blocks(text)
        assert set(result.keys()) == {"a.py", "b.py"}

    def test_block_wrapped_in_prose(self):
        text = "Here is my change:\n" + SIMPLE_FILE_BLOCK + "\nDone."
        result = _extract_file_blocks(text)
        assert "src/main.py" in result

    def test_no_blocks_returns_empty(self):
        assert _extract_file_blocks("No changes needed.") == {}

    def test_path_is_stripped(self):
        text = '<file path="  spaced/path.py  ">content\n</file>'
        result = _extract_file_blocks(text)
        assert "spaced/path.py" in result


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

    def test_valid_file_block_approved_and_staged(self):
        vfs = _make_vfs({"src/main.py": "def main():\n    pass\n"})
        file_block = (
            '<file path="src/main.py">\n'
            "def main():\n"
            "    print('hi')\n"
            "    pass\n"
            "</file>\n"
        )
        worker = _make_worker(self._ticket(), vfs, response_text=file_block)
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

        # Stage ticket-1's diff directly (replaces line 1 "a" → "A")
        diff_t1 = (
            "--- a/f.py\n+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-a\n+A\n b\n"
        )
        vfs.apply_diff("ticket-1", diff_t1)

        # ticket-2 tries to replace line 1 (same hunk) → conflict.
        # On second attempt it backs off with no changes.
        file_block_conflict = (
            '<file path="f.py">\nZ\nb\nc\nd\ne\n</file>\n'
        )
        client = MagicMock()
        client.chat.complete_async = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=MagicMock(content=file_block_conflict))]),
            MagicMock(choices=[MagicMock(message=MagicMock(content="No changes needed."))]),
        ])
        client.chat.stream_async = None  # disable streaming path in tests
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
        """Worker that always proposes a conflicting file block should eventually fail."""
        vfs, t1, t2 = self._two_ticket_vfs()

        # Pre-stage ticket-1 (replaces line 1 "a" → "A")
        diff_t1 = (
            "--- a/f.py\n+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-a\n+A\n b\n"
        )
        vfs.apply_diff("ticket-1", diff_t1)

        # ticket-2 always proposes the same conflicting rewrite of line 1
        conflicting_block = '<file path="f.py">\nZ\nb\nc\nd\ne\n</file>\n'
        worker = _make_worker(
            t2, vfs,
            response_text=conflicting_block,
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
        client.chat.stream_async = None  # disable streaming path in tests

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
        client.chat.stream_async = None  # disable streaming path in tests

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
