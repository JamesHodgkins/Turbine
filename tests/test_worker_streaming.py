"""Tests for Phase 16 — Streaming Worker Output.

Covers:
* ``_stream_response`` buffers all chunks into a single string
* ``on_worker_token`` is called with an incrementing cumulative char count
* Token usage is recorded from the stream's final usage datum
* Empty and None delta chunks are skipped gracefully
* Fallback to ``complete_async`` when the client has no ``stream_async``
* ``_call_with_retry`` retries on transient (retryable) errors
* ``_call_with_retry`` raises immediately on non-retryable errors
* Works correctly when ``ui=None`` or ``cost_tracker=None``
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from turbine.manager import Ticket, WorkerResult
from turbine.vfs import VirtualFileSystem
from turbine.worker import Worker, _RETRYABLE_STATUS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(tid: str = "t1") -> Ticket:
    return Ticket(
        id=tid,
        description="Test task",
        relevant_files=["src/main.py"],
    )


def _make_vfs(files: dict[str, str] | None = None) -> VirtualFileSystem:
    vfs = VirtualFileSystem()
    for path, content in (files or {"src/main.py": "# existing"}).items():
        vfs.load_text(path, content)
    return vfs


def _make_token_manager(fits: bool = True) -> MagicMock:
    tm = MagicMock()
    tm.fits.return_value = fits
    return tm


class FakeUsage:
    """Mimics the usage object attached to a Mistral stream's final chunk."""

    def __init__(self, prompt_tokens: int = 10, completion_tokens: int = 5) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeChunk:
    """Mimics a single chunk yielded by the Mistral streaming API.

    ``content`` — delta text (empty string means no delta on this event).
    ``usage``   — if not None, becomes the usage datum on the final chunk.
    """

    def __init__(self, content: str = "", usage: FakeUsage | None = None) -> None:
        self.data = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=content))],
            usage=usage,
        )


class FakeStream:
    """Async context manager that yields a fixed sequence of ``FakeChunk``s."""

    def __init__(self, chunks: list[FakeChunk]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> "FakeStream":
        self._iter = iter(self._chunks)
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def __aiter__(self) -> "FakeStream":
        return self

    async def __anext__(self) -> FakeChunk:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def _make_streaming_client(chunks: list[FakeChunk]) -> MagicMock:
    """Return a client whose ``chat.stream_async`` yields *chunks*.

    ``stream_async`` is an async function (coroutine) in the real SDK, so we
    use ``AsyncMock`` so that ``await stream_fn(...)`` returns the ``FakeStream``
    async context manager that the implementation expects.
    """
    client = MagicMock()
    client.chat.stream_async = AsyncMock(return_value=FakeStream(chunks))
    # Remove complete_async to ensure streaming path is chosen
    del client.chat.complete_async
    return client


def _make_nonstreaming_client(response_text: str) -> MagicMock:
    """Return a client with only ``complete_async`` (no ``stream_async``)."""
    client = MagicMock()
    client.chat.complete_async = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content=response_text))],
            usage=None,
        )
    )
    # Ensure stream_async does not exist so the fallback path is taken
    del client.chat.stream_async
    return client


def _make_worker(
    *,
    chunks: list[FakeChunk] | None = None,
    response_text: str = "",
    ui: object | None = None,
    cost_tracker: object | None = None,
    max_api_retries: int = 1,
    client: object | None = None,
) -> Worker:
    if client is None:
        if chunks is not None:
            client = _make_streaming_client(chunks)
        else:
            client = _make_nonstreaming_client(response_text)

    return Worker(
        ticket=_make_ticket(),
        vfs=_make_vfs(),
        client=client,
        model="mistral-large-latest",
        token_manager=_make_token_manager(),
        ui=ui,
        cost_tracker=cost_tracker,
        max_api_retries=max_api_retries,
    )


# ---------------------------------------------------------------------------
# _stream_response — content buffering
# ---------------------------------------------------------------------------


class TestStreamingBuffer:
    """The full response text is assembled from all chunk deltas."""

    @pytest.mark.asyncio
    async def test_single_chunk(self):
        chunks = [FakeChunk("Hello")]
        worker = _make_worker(chunks=chunks)
        result = await worker._stream_response([])
        assert result == "Hello"

    @pytest.mark.asyncio
    async def test_multiple_chunks_concatenated(self):
        chunks = [FakeChunk("Hello"), FakeChunk(","), FakeChunk(" world")]
        worker = _make_worker(chunks=chunks)
        result = await worker._stream_response([])
        assert result == "Hello, world"

    @pytest.mark.asyncio
    async def test_empty_stream_returns_empty_string(self):
        worker = _make_worker(chunks=[])
        result = await worker._stream_response([])
        assert result == ""

    @pytest.mark.asyncio
    async def test_empty_delta_chunks_skipped(self):
        chunks = [FakeChunk("A"), FakeChunk(""), FakeChunk("B")]
        worker = _make_worker(chunks=chunks)
        result = await worker._stream_response([])
        assert result == "AB"

    @pytest.mark.asyncio
    async def test_usage_only_chunk_does_not_add_content(self):
        usage = FakeUsage(prompt_tokens=8, completion_tokens=3)
        chunks = [FakeChunk("text"), FakeChunk("", usage=usage)]
        worker = _make_worker(chunks=chunks)
        result = await worker._stream_response([])
        assert result == "text"


# ---------------------------------------------------------------------------
# _stream_response — on_worker_token notifications
# ---------------------------------------------------------------------------


class TestOnWorkerToken:
    """UI is notified after each content chunk with the cumulative char count."""

    @pytest.mark.asyncio
    async def test_token_events_emitted_with_cumulative_count(self):
        ui = MagicMock()
        chunks = [FakeChunk("Hi"), FakeChunk(" there")]
        worker = _make_worker(chunks=chunks, ui=ui)

        await worker._stream_response([])

        assert ui.on_worker_token.call_args_list == [
            call("t1", 2),   # "Hi" → 2 chars
            call("t1", 8),   # "Hi there" → 8 chars
        ]

    @pytest.mark.asyncio
    async def test_no_event_for_empty_delta(self):
        ui = MagicMock()
        chunks = [FakeChunk("A"), FakeChunk("")]
        worker = _make_worker(chunks=chunks, ui=ui)

        await worker._stream_response([])

        # Only one call — the empty chunk must not trigger an event
        assert ui.on_worker_token.call_count == 1
        ui.on_worker_token.assert_called_once_with("t1", 1)

    @pytest.mark.asyncio
    async def test_no_crash_when_ui_is_none(self):
        chunks = [FakeChunk("hello")]
        worker = _make_worker(chunks=chunks, ui=None)
        result = await worker._stream_response([])
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_no_crash_when_ui_lacks_on_worker_token(self):
        ui = MagicMock(spec=[])  # ui with no attributes
        chunks = [FakeChunk("hello")]
        worker = _make_worker(chunks=chunks, ui=ui)
        result = await worker._stream_response([])
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_ticket_id_forwarded_to_ui(self):
        ui = MagicMock()
        chunks = [FakeChunk("X")]
        worker = _make_worker(chunks=chunks, ui=ui)

        await worker._stream_response([])

        args, _ = ui.on_worker_token.call_args
        assert args[0] == "t1"


# ---------------------------------------------------------------------------
# _stream_response — cost / usage recording
# ---------------------------------------------------------------------------


class TestUsageRecording:
    """Token usage from the stream's final chunk is recorded on the cost tracker."""

    @pytest.mark.asyncio
    async def test_usage_recorded_from_final_chunk(self):
        tracker = MagicMock()
        usage = FakeUsage(prompt_tokens=20, completion_tokens=10)
        chunks = [FakeChunk("hello"), FakeChunk("", usage=usage)]
        worker = _make_worker(chunks=chunks, cost_tracker=tracker)

        await worker._stream_response([])

        tracker.record.assert_called_once_with(input_tokens=20, output_tokens=10)

    @pytest.mark.asyncio
    async def test_usage_not_recorded_when_no_usage_chunk(self):
        tracker = MagicMock()
        chunks = [FakeChunk("hello")]  # no usage datum
        worker = _make_worker(chunks=chunks, cost_tracker=tracker)

        await worker._stream_response([])

        tracker.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_usage_not_recorded_when_cost_tracker_none(self):
        usage = FakeUsage()
        chunks = [FakeChunk("hello"), FakeChunk("", usage=usage)]
        worker = _make_worker(chunks=chunks, cost_tracker=None)
        # Must not crash
        result = await worker._stream_response([])
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_last_usage_wins_when_multiple_usage_chunks(self):
        """Only the latest non-None usage datum should be recorded."""
        tracker = MagicMock()
        chunks = [
            FakeChunk("a", usage=FakeUsage(1, 1)),
            FakeChunk("b", usage=FakeUsage(50, 25)),
        ]
        worker = _make_worker(chunks=chunks, cost_tracker=tracker)

        await worker._stream_response([])

        tracker.record.assert_called_once_with(input_tokens=50, output_tokens=25)


# ---------------------------------------------------------------------------
# Fallback path — complete_async when stream_async unavailable
# ---------------------------------------------------------------------------


class TestFallback:
    """Falls back to complete_async when stream_async is not on the client."""

    @pytest.mark.asyncio
    async def test_fallback_returns_response_content(self):
        worker = _make_worker(response_text="fallback response")
        result = await worker._stream_response([])
        assert result == "fallback response"

    @pytest.mark.asyncio
    async def test_fallback_records_usage(self):
        tracker = MagicMock()
        client = _make_nonstreaming_client("text")
        usage_obj = SimpleNamespace(prompt_tokens=5, completion_tokens=3)
        client.chat.complete_async.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="text"))],
            usage=usage_obj,
        )
        worker = _make_worker(client=client, cost_tracker=tracker)

        await worker._stream_response([])

        tracker.record.assert_called_once_with(input_tokens=5, output_tokens=3)

    @pytest.mark.asyncio
    async def test_fallback_no_usage_does_not_crash(self):
        client = _make_nonstreaming_client("ok")
        client.chat.complete_async.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="ok"))],
            usage=None,
        )
        worker = _make_worker(client=client, cost_tracker=MagicMock())
        # Must not raise
        result = await worker._stream_response([])
        assert result == "ok"


# ---------------------------------------------------------------------------
# _call_with_retry — retry logic
# ---------------------------------------------------------------------------


class TestRetryLogic:
    """_call_with_retry retries on transient errors and fails fast otherwise."""

    def _make_error(self, status_code: int) -> Exception:
        exc = Exception(f"HTTP {status_code}")
        exc.status_code = status_code  # type: ignore[attr-defined]
        return exc

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        chunks = [FakeChunk("ok")]
        worker = _make_worker(chunks=chunks, max_api_retries=3)
        result = await worker._call_with_retry([])
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retries_on_retryable_error_then_succeeds(self):
        attempts = []

        async def fake_stream_response(_messages):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise self._make_error(429)
            return "recovered"

        chunks = [FakeChunk("ok")]
        worker = _make_worker(chunks=chunks, max_api_retries=3)
        worker._stream_response = fake_stream_response  # type: ignore[method-assign]

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await worker._call_with_retry([])

        assert result == "recovered"
        assert len(attempts) == 2

    @pytest.mark.asyncio
    async def test_raises_immediately_on_non_retryable_error(self):
        attempts = []

        async def fake_stream_response(_messages):
            attempts.append(1)
            raise self._make_error(400)

        worker = _make_worker(chunks=[], max_api_retries=3)
        worker._stream_response = fake_stream_response  # type: ignore[method-assign]

        with pytest.raises(Exception, match="HTTP 400"):
            await worker._call_with_retry([])

        assert len(attempts) == 1

    @pytest.mark.asyncio
    async def test_raises_last_exception_after_all_retries(self):
        call_count = 0

        async def fake_stream_response(_messages):
            nonlocal call_count
            call_count += 1
            raise self._make_error(503)

        worker = _make_worker(chunks=[], max_api_retries=2)
        worker._stream_response = fake_stream_response  # type: ignore[method-assign]

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(Exception, match="HTTP 503"):
                await worker._call_with_retry([])

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_all_retryable_codes_trigger_retry(self):
        """Every status code in _RETRYABLE_STATUS causes a retry."""
        for code in _RETRYABLE_STATUS:
            attempts = []

            async def fake_stream_response(_messages, _code=code):
                attempts.append(1)
                if len(attempts) == 1:
                    raise self._make_error(_code)
                return "ok"

            worker = _make_worker(chunks=[], max_api_retries=2)
            worker._stream_response = fake_stream_response  # type: ignore[method-assign]

            with patch("asyncio.sleep", new=AsyncMock()):
                result = await worker._call_with_retry([])

            assert result == "ok", f"Expected retry and recovery for status {code}"
