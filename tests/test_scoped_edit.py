"""Tests for turbine.scoped_edit — Phase 9."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from turbine.scoped_edit import (
    LARGE_FILE_THRESHOLD,
    ScopedEdit,
    ScopedEditApplicator,
    ScopedEditResult,
    check_definition_integrity,
    parse_scoped_edits,
)
from turbine.manager import Ticket
from turbine.vfs import VirtualFileSystem
from turbine.worker import Worker, _extract_scoped_edit_blocks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lines(n: int, prefix: str = "line") -> list[str]:
    """Return *n* simple numbered lines."""
    return [f"{prefix} {i}" for i in range(1, n + 1)]


def _make_vfs(files: dict[str, list[str]]) -> VirtualFileSystem:
    vfs = VirtualFileSystem()
    for key, lines in files.items():
        vfs.load_text(key, "\n".join(lines))
    return vfs


def _make_worker(
    ticket: Ticket,
    vfs: VirtualFileSystem,
    response_text: str = "",
    max_handshake_attempts: int = 3,
    max_api_retries: int = 1,
) -> Worker:
    tm = MagicMock()
    tm.fits.return_value = True
    client = MagicMock()
    client.chat.complete_async = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content=response_text))]
        )
    )
    client.chat.stream_async = None  # disable streaming path in tests
    return Worker(
        ticket=ticket,
        vfs=vfs,
        client=client,
        model="mistral-large-latest",
        token_manager=tm,
        max_handshake_attempts=max_handshake_attempts,
        max_api_retries=max_api_retries,
    )


# ---------------------------------------------------------------------------
# parse_scoped_edits
# ---------------------------------------------------------------------------

class TestParseScopedEdits:
    def test_function_edit(self):
        edits = parse_scoped_edits([{"function": "foo", "replacement": "def foo(): pass"}])
        assert len(edits) == 1
        assert edits[0].function == "foo"
        assert edits[0].replacement == "def foo(): pass"
        assert edits[0].is_function_scoped()

    def test_line_edit(self):
        edits = parse_scoped_edits([{"lines": [5, 10], "replacement": "new content"}])
        assert len(edits) == 1
        assert edits[0].lines == (5, 10)
        assert edits[0].is_line_scoped()

    def test_wrapped_in_edits_key(self):
        data = {"edits": [{"function": "bar", "replacement": "def bar(): ..."}]}
        edits = parse_scoped_edits(data)
        assert len(edits) == 1
        assert edits[0].function == "bar"

    def test_single_dict_wraps_to_list(self):
        edits = parse_scoped_edits({"function": "baz", "replacement": ""})
        assert len(edits) == 1

    def test_empty_replacement_allowed(self):
        edits = parse_scoped_edits([{"function": "old", "replacement": ""}])
        assert edits[0].replacement == ""

    def test_invalid_lines_raises(self):
        with pytest.raises(ValueError, match="invalid line range"):
            parse_scoped_edits([{"lines": [10, 5], "replacement": "x"}])

    def test_line_start_zero_raises(self):
        with pytest.raises(ValueError, match="start must be"):
            parse_scoped_edits([{"lines": [0, 3], "replacement": "x"}])

    def test_missing_function_and_lines_raises(self):
        with pytest.raises(ValueError, match="must have either"):
            parse_scoped_edits([{"replacement": "x"}])

    def test_non_list_raises(self):
        with pytest.raises(ValueError, match="list"):
            parse_scoped_edits("not a list")

    def test_multiple_edits(self):
        edits = parse_scoped_edits([
            {"function": "a", "replacement": "def a(): pass"},
            {"lines": [20, 22], "replacement": "x = 1"},
        ])
        assert len(edits) == 2


# ---------------------------------------------------------------------------
# ScopedEditApplicator — line-range edits
# ---------------------------------------------------------------------------

class TestScopedEditApplicatorLineRange:
    def _lines(self) -> list[str]:
        return ["alpha", "beta", "gamma", "delta", "epsilon"]

    def test_replace_middle_lines(self):
        lines = self._lines()
        edits = [ScopedEdit(lines=(2, 3), replacement="NEW_BETA\nNEW_GAMMA")]
        result = ScopedEditApplicator(lines).apply(edits)
        assert result.success
        assert result.lines == ["alpha", "NEW_BETA", "NEW_GAMMA", "delta", "epsilon"]

    def test_delete_lines(self):
        lines = self._lines()
        edits = [ScopedEdit(lines=(2, 2), replacement="")]
        result = ScopedEditApplicator(lines).apply(edits)
        assert result.success
        assert result.lines == ["alpha", "gamma", "delta", "epsilon"]

    def test_replace_first_line(self):
        lines = self._lines()
        edits = [ScopedEdit(lines=(1, 1), replacement="FIRST")]
        result = ScopedEditApplicator(lines).apply(edits)
        assert result.success
        assert result.lines[0] == "FIRST"

    def test_replace_last_line(self):
        lines = self._lines()
        edits = [ScopedEdit(lines=(5, 5), replacement="LAST")]
        result = ScopedEditApplicator(lines).apply(edits)
        assert result.success
        assert result.lines[-1] == "LAST"

    def test_out_of_range_fails(self):
        lines = self._lines()
        edits = [ScopedEdit(lines=(4, 99), replacement="x")]
        result = ScopedEditApplicator(lines).apply(edits)
        assert not result.success
        assert "exceeds file length" in result.error

    def test_overlapping_edits_fail(self):
        lines = self._lines()
        edits = [
            ScopedEdit(lines=(2, 4), replacement="x"),
            ScopedEdit(lines=(3, 5), replacement="y"),
        ]
        result = ScopedEditApplicator(lines).apply(edits)
        assert not result.success
        assert "overlap" in result.error

    def test_non_overlapping_multiple_edits_applied_correctly(self):
        lines = ["a", "b", "c", "d", "e"]
        edits = [
            ScopedEdit(lines=(1, 1), replacement="A"),
            ScopedEdit(lines=(5, 5), replacement="E"),
        ]
        result = ScopedEditApplicator(lines).apply(edits)
        assert result.success
        assert result.lines == ["A", "b", "c", "d", "E"]


# ---------------------------------------------------------------------------
# ScopedEditApplicator — function-scoped edits
# ---------------------------------------------------------------------------

SAMPLE_MODULE = """\
def greet(name):
    return f"hello {name}"


def farewell(name):
    return f"goodbye {name}"


class Greeter:
    def __init__(self):
        self.x = 1
""".splitlines()


class TestScopedEditApplicatorFunctionScope:
    def test_replace_function(self):
        edits = [ScopedEdit(function="greet", replacement="def greet(name):\n    return 'hi'")]
        result = ScopedEditApplicator(SAMPLE_MODULE).apply(edits)
        assert result.success
        combined = "\n".join(result.lines)
        assert "return 'hi'" in combined
        assert "farewell" in combined  # untouched

    def test_replace_second_function(self):
        edits = [ScopedEdit(function="farewell", replacement="def farewell(name):\n    return 'bye'")]
        result = ScopedEditApplicator(SAMPLE_MODULE).apply(edits)
        assert result.success
        combined = "\n".join(result.lines)
        assert "return 'bye'" in combined
        assert "greet" in combined  # untouched

    def test_unknown_function_fails(self):
        edits = [ScopedEdit(function="nonexistent", replacement="def nonexistent(): pass")]
        result = ScopedEditApplicator(SAMPLE_MODULE).apply(edits)
        assert not result.success
        assert "nonexistent" in result.error

    def test_replace_class(self):
        edits = [ScopedEdit(function="Greeter", replacement="class Greeter:\n    pass")]
        result = ScopedEditApplicator(SAMPLE_MODULE).apply(edits)
        assert result.success
        combined = "\n".join(result.lines)
        assert "class Greeter" in combined


# ---------------------------------------------------------------------------
# check_definition_integrity
# ---------------------------------------------------------------------------

class TestDefinitionIntegrity:
    def test_no_loss_returns_empty(self):
        original = ["def foo(): pass", "def bar(): pass"]
        proposed = ["def foo(): pass", "def bar(): pass"]
        assert check_definition_integrity(original, proposed) == []

    def test_missing_def_detected(self):
        original = ["def foo(): pass", "def bar(): pass"]
        proposed = ["def foo(): pass"]
        lost = check_definition_integrity(original, proposed)
        assert "bar" in lost

    def test_new_def_in_proposed_not_flagged(self):
        original = ["def foo(): pass"]
        proposed = ["def foo(): pass", "def baz(): pass"]
        assert check_definition_integrity(original, proposed) == []

    def test_class_loss_detected(self):
        original = ["class MyClass:", "    pass"]
        proposed = []
        lost = check_definition_integrity(original, proposed)
        assert "MyClass" in lost

    def test_empty_original_no_loss(self):
        assert check_definition_integrity([], ["def foo(): pass"]) == []


# ---------------------------------------------------------------------------
# _extract_scoped_edit_blocks
# ---------------------------------------------------------------------------

class TestExtractScopedEditBlocks:
    def test_single_block(self):
        text = (
            'Some preamble\n'
            '<scoped_edits path="src/foo.py">\n'
            '[{"function": "bar", "replacement": "def bar(): pass"}]\n'
            '</scoped_edits>\n'
        )
        result = _extract_scoped_edit_blocks(text)
        assert "src/foo.py" in result
        assert '"function"' in result["src/foo.py"]

    def test_multiple_blocks(self):
        text = (
            '<scoped_edits path="a.py">[{"lines": [1,2], "replacement": "x"}]</scoped_edits>'
            '<scoped_edits path="b.py">[{"function": "f", "replacement": "def f(): pass"}]</scoped_edits>'
        )
        result = _extract_scoped_edit_blocks(text)
        assert set(result.keys()) == {"a.py", "b.py"}

    def test_no_blocks_returns_empty(self):
        assert _extract_scoped_edit_blocks("No changes here.") == {}

    def test_path_normalised(self):
        text = '<scoped_edits path="./src/util.py">[{"function":"f","replacement":""}]</scoped_edits>'
        result = _extract_scoped_edit_blocks(text)
        assert "src/util.py" in result


# ---------------------------------------------------------------------------
# Worker._is_large_file
# ---------------------------------------------------------------------------

class TestWorkerIsLargeFile:
    def _ticket(self, files: list[str]) -> Ticket:
        return Ticket(id="t1", description="d", relevant_files=files)

    def test_small_file_not_large(self):
        small = _lines(LARGE_FILE_THRESHOLD - 1)
        vfs = _make_vfs({"small.py": small})
        worker = _make_worker(self._ticket(["small.py"]), vfs)
        assert "small.py" not in worker._large_files

    def test_exactly_threshold_is_large(self):
        big = _lines(LARGE_FILE_THRESHOLD)
        vfs = _make_vfs({"big.py": big})
        worker = _make_worker(self._ticket(["big.py"]), vfs)
        assert "big.py" in worker._large_files

    def test_above_threshold_is_large(self):
        huge = _lines(LARGE_FILE_THRESHOLD + 50)
        vfs = _make_vfs({"huge.py": huge})
        worker = _make_worker(self._ticket(["huge.py"]), vfs)
        assert "huge.py" in worker._large_files


# ---------------------------------------------------------------------------
# Worker._build_file_contents — large file labelling
# ---------------------------------------------------------------------------

class TestWorkerBuildFileContents:
    def test_large_file_labelled(self):
        big_lines = _lines(LARGE_FILE_THRESHOLD)
        vfs = _make_vfs({"big.py": big_lines})
        ticket = Ticket(id="t", description="d", relevant_files=["big.py"])
        worker = _make_worker(ticket, vfs)
        contents = worker._build_file_contents()
        assert "[LARGE FILE — use scoped edits]" in contents

    def test_small_file_not_labelled(self):
        small_lines = _lines(LARGE_FILE_THRESHOLD - 1)
        vfs = _make_vfs({"small.py": small_lines})
        ticket = Ticket(id="t", description="d", relevant_files=["small.py"])
        worker = _make_worker(ticket, vfs)
        contents = worker._build_file_contents()
        assert "[LARGE FILE" not in contents


# ---------------------------------------------------------------------------
# Worker scoped-edit end-to-end (via run())
# ---------------------------------------------------------------------------

class TestWorkerScopedEditRun:
    def _big_file_lines(self) -> list[str]:
        lines = [f"line_{i}" for i in range(LARGE_FILE_THRESHOLD + 10)]
        # Inject a real function definition near the top
        lines[5] = "def target_fn():"
        lines[6] = "    return 'old'"
        return lines

    def test_scoped_edit_applied_and_staged(self):
        """Worker returns a scoped_edits block; result should be staged."""
        big = self._big_file_lines()
        vfs = _make_vfs({"big.py": big})
        ticket = Ticket(id="t", description="d", relevant_files=["big.py"])

        edit_json = json.dumps([
            {"lines": [6, 7], "replacement": "def target_fn():\n    return 'new'"}
        ])
        response = f'<scoped_edits path="big.py">\n{edit_json}\n</scoped_edits>'

        tm = MagicMock()
        tm.fits.return_value = True
        client = MagicMock()
        client.chat.complete_async = AsyncMock(
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content=response))]
            )
        )
        client.chat.stream_async = None  # disable streaming path in tests
        worker = Worker(
            ticket=ticket,
            vfs=vfs,
            client=client,
            model="m",
            token_manager=tm,
            max_api_retries=1,
        )
        result = asyncio.run(worker.run())
        assert result.success
        assert result.proposed_diff != ""
        # VFS snapshot should contain the new content
        snap = vfs.get_snapshot("big.py")
        assert snap is not None
        assert "return 'new'" in "\n".join(snap)

    def test_bad_scoped_edit_json_falls_back_to_no_change(self):
        """Malformed JSON in scoped_edits block → fallback → no complete block → no changes."""
        big = self._big_file_lines()
        vfs = _make_vfs({"big.py": big})
        ticket = Ticket(id="t", description="d", relevant_files=["big.py"])

        response = '<scoped_edits path="big.py">NOT VALID JSON</scoped_edits>'
        tm = MagicMock()
        tm.fits.return_value = True
        client = MagicMock()
        client.chat.complete_async = AsyncMock(
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content=response))]
            )
        )
        client.chat.stream_async = None  # disable streaming path in tests
        worker = Worker(
            ticket=ticket,
            vfs=vfs,
            client=client,
            model="m",
            token_manager=tm,
            max_api_retries=1,
        )
        result = asyncio.run(worker.run())
        # Scoped edit fails, no complete file block → treated as no changes
        assert result.success
        assert result.proposed_diff == ""


# ---------------------------------------------------------------------------
# Worker integrity check end-to-end
# ---------------------------------------------------------------------------

class TestWorkerIntegrityCheck:
    def test_integrity_failure_triggers_retry(self):
        """First response drops a def; second response is clean → success."""
        src = "def keep(): pass\ndef change(): pass\n"
        vfs = _make_vfs({"f.py": src.splitlines()})
        ticket = Ticket(id="t", description="d", relevant_files=["f.py"])

        # First response: drops 'keep'
        bad_response = '<file path="f.py">def change(): return 1\n</file>'
        # Second response: keeps both
        good_response = (
            '<file path="f.py">def keep(): pass\ndef change(): return 1\n</file>'
        )

        tm = MagicMock()
        tm.fits.return_value = True
        client = MagicMock()
        client.chat.complete_async = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=MagicMock(content=bad_response))]),
            MagicMock(choices=[MagicMock(message=MagicMock(content=good_response))]),
        ])
        client.chat.stream_async = None  # disable streaming path in tests
        worker = Worker(
            ticket=ticket,
            vfs=vfs,
            client=client,
            model="m",
            token_manager=tm,
            max_handshake_attempts=3,
            max_api_retries=1,
        )
        result = asyncio.run(worker.run())
        assert result.success
        # The API should have been called twice (bad → retry → good)
        assert client.chat.complete_async.call_count == 2

    def test_integrity_check_passes_when_no_defs_lost(self):
        """No defs dropped → no rejection → single API call."""
        src = "def foo(): pass\n"
        vfs = _make_vfs({"f.py": src.splitlines()})
        ticket = Ticket(id="t", description="d", relevant_files=["f.py"])

        good_response = '<file path="f.py">def foo(): return 1\n</file>'

        tm = MagicMock()
        tm.fits.return_value = True
        client = MagicMock()
        client.chat.complete_async = AsyncMock(
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content=good_response))]
            )
        )
        client.chat.stream_async = None  # disable streaming path in tests
        worker = Worker(
            ticket=ticket,
            vfs=vfs,
            client=client,
            model="m",
            token_manager=tm,
            max_api_retries=1,
        )
        result = asyncio.run(worker.run())
        assert result.success
        assert client.chat.complete_async.call_count == 1
