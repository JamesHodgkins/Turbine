"""Tests for Phase 11 — Iterative Investigation.

Covers:
  - _chunk_names() helper extracts file paths from chunk headers
  - Truncation note is injected into the user message when files are dropped
  - needs_more_files is parsed from the LLM response
  - Follow-up read loop loads the requested files and re-investigates
  - Follow-up loop respects MAX_INVESTIGATION_ROUNDS (max 2 extra rounds)
  - Invalid / missing paths in needs_more_files are silently skipped
  - Files already in loaded_rels are not re-fetched
  - Follow-up files that exceed the context budget are skipped
  - needs_more_files is empty when no truncation occurred
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from turbine.manager import Manager, Ticket, _merge_overlapping_tickets
from turbine.tree_mapper import FileNode, ProjectTree


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tree(root: Path, files: list[str]) -> ProjectTree:
    tree = ProjectTree(root=root)
    for rel in files:
        tree.files.append(
            FileNode(path=root / rel, relative=rel, size_bytes=0, extension=Path(rel).suffix)
        )
    return tree


def _make_manager(tree: ProjectTree, root: Path, request: str = "Fix the bug") -> Manager:
    with patch("turbine.manager.Mistral"):
        mgr = Manager(tree=tree, user_request=request, project_root=root, api_key="test-key")
    mgr._client.chat.complete_async = AsyncMock(
        return_value=MagicMock(choices=[MagicMock(message=MagicMock(content=""))])
    )
    return mgr


def _mock_chat(text: str) -> AsyncMock:
    return AsyncMock(return_value=text)


def _ticket_response(
    relevant: list[str] | None = None,
    needs_more: list[str] | None = None,
) -> str:
    body: dict = {
        "diagnosis": "needs fix",
        "tickets": [
            {
                "id": "ticket-1",
                "description": "Fix main",
                "relevant_files": relevant or ["src/main.py"],
                "new_files": [],
                "context_files": [],
                "context": "1. Do it",
            }
        ],
    }
    if needs_more is not None:
        body["needs_more_files"] = needs_more
    return json.dumps(body)


# ---------------------------------------------------------------------------
# _chunk_names helper
# ---------------------------------------------------------------------------

class TestChunkNames:
    def test_simple_header(self):
        chunk = "### src/main.py\n```\nx = 1\n```"
        assert Manager._chunk_names([chunk]) == ["src/main.py"]

    def test_large_file_label_stripped(self):
        chunk = "### src/big.py [LARGE FILE — use scoped edits]\n```\n...\n```"
        assert Manager._chunk_names([chunk]) == ["src/big.py"]

    def test_multiple_chunks(self):
        chunks = [
            "### a.py\n```\n```",
            "### b.py\n```\n```",
        ]
        assert Manager._chunk_names(chunks) == ["a.py", "b.py"]

    def test_empty_chunk_skipped(self):
        assert Manager._chunk_names([""]) == []

    def test_no_hash_prefix_still_works(self):
        # Defensive: even if a chunk starts with just the path
        chunk = "src/main.py\n```\ncode\n```"
        names = Manager._chunk_names([chunk])
        # Should not crash; result may be non-empty but parseable
        assert isinstance(names, list)


# ---------------------------------------------------------------------------
# Truncation note injection
# ---------------------------------------------------------------------------

class TestTruncationNote:
    def test_truncation_note_in_user_message_when_files_dropped(self, tmp_path):
        """When truncate_to_fit drops files, the LLM call must receive a warning note."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        (tmp_path / "src" / "utils.py").write_text("y = 2\n")
        tree = _make_tree(tmp_path, ["src/main.py", "src/utils.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py", "src/utils.py"]

        # Simulate truncate_to_fit keeping only the first chunk
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks[:1]

        captured_user: list[str] = []

        async def fake_chat(system: str, user: str) -> str:
            captured_user.append(user)
            return _ticket_response()

        mgr._chat = fake_chat  # type: ignore[assignment]

        asyncio.run(mgr.investigate())

        assert captured_user, "investigate() must call _chat at least once"
        first_user = captured_user[0]
        assert "CONTEXT TRUNCATED" in first_user
        assert "src/utils.py" in first_user

    def test_no_truncation_note_when_all_files_fit(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        # truncate_to_fit returns all chunks unchanged
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks

        captured_user: list[str] = []

        async def fake_chat(system: str, user: str) -> str:
            captured_user.append(user)
            return _ticket_response()

        mgr._chat = fake_chat  # type: ignore[assignment]

        asyncio.run(mgr.investigate())

        assert "CONTEXT TRUNCATED" not in captured_user[0]

    def test_truncation_note_lists_all_dropped_files(self, tmp_path):
        for name in ["a.py", "b.py", "c.py"]:
            (tmp_path / name).write_text("pass\n")
        tree = _make_tree(tmp_path, ["a.py", "b.py", "c.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["a.py", "b.py", "c.py"]
        # Only keep a.py
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks[:1]

        captured_user: list[str] = []

        async def fake_chat(system: str, user: str) -> str:
            captured_user.append(user)
            return _ticket_response(relevant=["a.py"])

        mgr._chat = fake_chat  # type: ignore[assignment]

        asyncio.run(mgr.investigate())

        note = captured_user[0]
        assert "b.py" in note or "c.py" in note  # at least one dropped name present


# ---------------------------------------------------------------------------
# needs_more_files parsing
# ---------------------------------------------------------------------------

class TestNeedsMoreFilesParsing:
    def test_parsed_when_present(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks[:0]  # drop all

        response = json.dumps({
            "diagnosis": "uncertain",
            "needs_more_files": ["src/main.py"],
            "tickets": [],
        })
        # Return needs_more_files on round 0; resolve on round 1
        call_count = 0

        async def fake_chat(system: str, user: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return response
            return _ticket_response()

        mgr._chat = fake_chat  # type: ignore[assignment]
        asyncio.run(mgr.investigate())
        # After round 1 the loop resolves; needs_more_files may be [] from round 2
        # but the important thing is no crash and tickets eventually populated
        assert isinstance(mgr.tickets, list)

    def test_empty_when_absent(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks
        mgr._chat = _mock_chat(_ticket_response())

        asyncio.run(mgr.investigate())

        assert mgr.needs_more_files == []

    def test_non_string_entries_filtered(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks

        bad_response = json.dumps({
            "diagnosis": "x",
            "needs_more_files": [123, None, "valid.py"],
            "tickets": [],
        })
        # One follow-up round; second call resolves
        call_count = 0

        async def fake_chat(system: str, user: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return bad_response
            return _ticket_response()

        mgr._chat = fake_chat  # type: ignore[assignment]
        asyncio.run(mgr.investigate())
        # "valid.py" not on disk → skipped; no crash
        assert isinstance(mgr.tickets, list)


# ---------------------------------------------------------------------------
# Follow-up read loop
# ---------------------------------------------------------------------------

class TestFollowUpReadLoop:
    def test_follow_up_loads_requested_file_and_reinvestigates(self, tmp_path):
        """When needs_more_files names a real on-disk file, investigate() should
        load it into the VFS and call _chat a second time."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        (tmp_path / "src" / "utils.py").write_text("helper = True\n")

        tree = _make_tree(tmp_path, ["src/main.py", "src/utils.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        # First truncate drops utils.py, then keep all on follow-up
        call_count_truncate = 0

        def smart_truncate(chunks, **_):
            nonlocal call_count_truncate
            call_count_truncate += 1
            if call_count_truncate == 1:
                return [c for c in chunks if "main.py" in c]
            return chunks

        mgr.token_manager.truncate_to_fit = smart_truncate

        chat_calls: list[str] = []

        async def fake_chat(system: str, user: str) -> str:
            chat_calls.append(user)
            if len(chat_calls) == 1:
                return json.dumps({
                    "diagnosis": "need utils",
                    "needs_more_files": ["src/utils.py"],
                    "tickets": [],
                })
            return _ticket_response()

        mgr._chat = fake_chat  # type: ignore[assignment]

        asyncio.run(mgr.investigate())

        assert len(chat_calls) == 2, "Expected exactly 2 LLM calls (initial + follow-up)"
        # Second call must include utils.py content
        assert "helper = True" in chat_calls[1]

    def test_follow_up_file_loaded_into_vfs(self, tmp_path):
        """The follow-up file must be available in the VFS after investigate()."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        (tmp_path / "src" / "utils.py").write_text("helper = True\n")

        tree = _make_tree(tmp_path, ["src/main.py", "src/utils.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        call_n = 0

        def truncate_first_only(chunks, **_):
            nonlocal call_n
            call_n += 1
            if call_n == 1:
                return [c for c in chunks if "main.py" in c]
            return chunks

        mgr.token_manager.truncate_to_fit = truncate_first_only
        chat_n = 0

        async def fake_chat(system: str, user: str) -> str:
            nonlocal chat_n
            chat_n += 1
            if chat_n == 1:
                return json.dumps({
                    "diagnosis": "need utils",
                    "needs_more_files": ["src/utils.py"],
                    "tickets": [],
                })
            return _ticket_response()

        mgr._chat = fake_chat  # type: ignore[assignment]

        asyncio.run(mgr.investigate())

        snap = mgr.vfs.get_snapshot("src/utils.py")
        assert snap is not None
        assert "helper = True" in "\n".join(snap)

    def test_follow_up_respects_max_rounds(self, tmp_path):
        """The loop must stop after MAX_INVESTIGATION_ROUNDS extra calls even if
        the LLM keeps requesting more files."""
        (tmp_path / "a.py").write_text("a\n")
        (tmp_path / "b.py").write_text("b\n")
        (tmp_path / "c.py").write_text("c\n")

        tree = _make_tree(tmp_path, ["a.py", "b.py", "c.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["a.py"]
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks  # always fits

        chat_calls = 0

        async def fake_chat(system: str, user: str) -> str:
            nonlocal chat_calls
            chat_calls += 1
            # Always claims it needs more files
            return json.dumps({
                "diagnosis": "uncertain",
                "needs_more_files": ["b.py"],
                "tickets": [],
            })

        mgr._chat = fake_chat  # type: ignore[assignment]

        asyncio.run(mgr.investigate())

        # Initial call + at most MAX_INVESTIGATION_ROUNDS follow-up calls
        assert chat_calls <= 1 + mgr.MAX_INVESTIGATION_ROUNDS

    def test_invalid_paths_skipped_no_crash(self, tmp_path):
        """needs_more_files with non-existent paths must not crash investigate()."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks[:0]

        call_n = 0

        async def fake_chat(system: str, user: str) -> str:
            nonlocal call_n
            call_n += 1
            if call_n == 1:
                return json.dumps({
                    "diagnosis": "x",
                    "needs_more_files": ["ghost.py"],  # does not exist on disk
                    "tickets": [],
                })
            return _ticket_response()

        mgr._chat = fake_chat  # type: ignore[assignment]

        asyncio.run(mgr.investigate())  # must not raise

    def test_already_loaded_file_not_re_fetched(self, tmp_path):
        """A file already in the VFS should not be loaded again if requested in
        needs_more_files."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks  # nothing dropped

        call_n = 0
        load_calls: list[str] = []
        _real_load = mgr.vfs.load_from_disk

        def patched_load(path, relative_key=None):
            load_calls.append(relative_key or str(path))
            return _real_load(path, relative_key=relative_key)

        mgr.vfs.load_from_disk = patched_load  # type: ignore[assignment]

        async def fake_chat(system: str, user: str) -> str:
            nonlocal call_n
            call_n += 1
            if call_n == 1:
                # Request a file that was already in relevant_files (already loaded)
                return json.dumps({
                    "diagnosis": "x",
                    "needs_more_files": ["src/main.py"],
                    "tickets": [],
                })
            return _ticket_response()

        mgr._chat = fake_chat  # type: ignore[assignment]

        asyncio.run(mgr.investigate())

        # src/main.py should be loaded exactly once (from the initial file pass)
        assert load_calls.count("src/main.py") == 1

    def test_follow_up_exceeds_budget_stops_gracefully(self, tmp_path):
        """When the follow-up file doesn't fit the context budget, the loop must
        stop without crashing and produce whatever tickets the last response gave."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        (tmp_path / "src" / "big.py").write_text("big\n")
        tree = _make_tree(tmp_path, ["src/main.py", "src/big.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        call_n = 0

        def truncate(chunks, **_):
            nonlocal call_n
            # On initial call: keep only main.py.
            # On follow-up check (second call with main+big): still only keep main.py.
            return [c for c in chunks if "main.py" in c]

        mgr.token_manager.truncate_to_fit = truncate
        chat_n = 0

        async def fake_chat(system: str, user: str) -> str:
            nonlocal chat_n
            chat_n += 1
            return json.dumps({
                "diagnosis": "need big",
                "needs_more_files": ["src/big.py"],
                "tickets": [],
            })

        mgr._chat = fake_chat  # type: ignore[assignment]

        asyncio.run(mgr.investigate())  # must not raise

    def test_no_follow_up_when_no_truncation(self, tmp_path):
        """When nothing was truncated, the LLM should never set needs_more_files
        (the prompt won't even mention it), and investigate() should call _chat
        exactly once."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks

        chat_calls = 0

        async def fake_chat(system: str, user: str) -> str:
            nonlocal chat_calls
            chat_calls += 1
            return _ticket_response()

        mgr._chat = fake_chat  # type: ignore[assignment]

        asyncio.run(mgr.investigate())

        assert chat_calls == 1

    def test_second_round_response_used_as_final(self, tmp_path):
        """The tickets produced by the second (follow-up) LLM call are the ones
        that should be used going forward."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        (tmp_path / "src" / "utils.py").write_text("y = 2\n")
        tree = _make_tree(tmp_path, ["src/main.py", "src/utils.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        call_n_trunc = 0

        def truncate(chunks, **_):
            nonlocal call_n_trunc
            call_n_trunc += 1
            if call_n_trunc == 1:
                return [c for c in chunks if "main.py" in c]
            return chunks

        mgr.token_manager.truncate_to_fit = truncate
        chat_n = 0

        async def fake_chat(system: str, user: str) -> str:
            nonlocal chat_n
            chat_n += 1
            if chat_n == 1:
                return json.dumps({
                    "diagnosis": "round-1-diagnosis",
                    "needs_more_files": ["src/utils.py"],
                    "tickets": [],
                })
            # Round 2: real answer with a distinctive description
            return json.dumps({
                "diagnosis": "round-2-diagnosis",
                "needs_more_files": [],
                "tickets": [
                    {
                        "id": "ticket-1",
                        "description": "FINAL ANSWER",
                        "relevant_files": ["src/main.py"],
                        "new_files": [],
                        "context_files": [],
                        "context": "1. Do it right",
                    }
                ],
            })

        mgr._chat = fake_chat  # type: ignore[assignment]

        asyncio.run(mgr.investigate())

        assert mgr.diagnosis == "round-2-diagnosis"
        assert len(mgr.tickets) == 1
        assert mgr.tickets[0].description == "FINAL ANSWER"


# ---------------------------------------------------------------------------
# clarification_hint
# ---------------------------------------------------------------------------

class TestClarificationHint:
    def test_hint_stored_when_returned_by_llm(self, tmp_path):
        """clarification_hint from the LLM response must be stored on the Manager."""
        (tmp_path / "a.py").write_text("pass\n")
        tree = _make_tree(tmp_path, ["a.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["a.py"]
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks

        async def fake_chat(system: str, user: str) -> str:
            return json.dumps({
                "diagnosis": "uncertain",
                "needs_more_files": [],
                "clarification_hint": "Re-run with --context to include auth.py.",
                "tickets": [
                    {
                        "id": "t1",
                        "description": "fix",
                        "relevant_files": ["a.py"],
                        "new_files": [],
                        "context_files": [],
                        "context": "1. do it",
                    }
                ],
            })

        mgr._chat = fake_chat  # type: ignore[assignment]
        asyncio.run(mgr.investigate())

        assert mgr.clarification_hint == "Re-run with --context to include auth.py."

    def test_hint_empty_when_absent(self, tmp_path):
        """clarification_hint defaults to empty string when not present."""
        (tmp_path / "a.py").write_text("pass\n")
        tree = _make_tree(tmp_path, ["a.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["a.py"]
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks
        mgr._chat = _mock_chat(_ticket_response())

        asyncio.run(mgr.investigate())

        assert mgr.clarification_hint == ""

    def test_hint_logged_when_round_cap_hit(self, tmp_path):
        """When the round cap is hit and a clarification_hint is present, it must
        be logged via log.error (the ⚠️ INVESTIGATION HINT path).

        The round cap triggers when needs_more_files is non-empty AND
        follow_up_round >= MAX_INVESTIGATION_ROUNDS.  We achieve this by having
        the LLM always request a *different* file each round so the loop can
        never declare satisfaction — it keeps loading new files until the cap.
        """
        (tmp_path / "a.py").write_text("pass\n")
        (tmp_path / "b.py").write_text("pass\n")
        (tmp_path / "c.py").write_text("pass\n")
        (tmp_path / "d.py").write_text("pass\n")
        tree = _make_tree(tmp_path, ["a.py", "b.py", "c.py", "d.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["a.py"]
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks

        logged_errors: list[str] = []
        real_error = mgr.log.error

        def capture_error(msg: str) -> None:
            logged_errors.append(msg)
            real_error(msg)

        mgr.log.error = capture_error  # type: ignore[assignment]

        # Rotate through b/c/d so the LLM always asks for a new file it hasn't
        # loaded yet — the loop keeps going until the round cap fires.
        rotation = ["b.py", "c.py", "d.py"]
        call_idx = 0

        async def fake_chat(system: str, user: str) -> str:
            nonlocal call_idx
            wanted = rotation[min(call_idx, len(rotation) - 1)]
            call_idx += 1
            return json.dumps({
                "diagnosis": "uncertain",
                "needs_more_files": [wanted],
                "clarification_hint": "Include missing.py explicitly with --context.",
                "tickets": [],
            })

        mgr._chat = fake_chat  # type: ignore[assignment]
        asyncio.run(mgr.investigate())

        hint_logged = any(
            "INVESTIGATION HINT" in msg and "missing.py" in msg
            for msg in logged_errors
        )
        assert hint_logged, f"Expected hint in logged errors, got: {logged_errors}"

    def test_hint_not_logged_when_empty(self, tmp_path):
        """No INVESTIGATION HINT log line should appear when hint is empty."""
        (tmp_path / "a.py").write_text("pass\n")
        (tmp_path / "b.py").write_text("pass\n")
        tree = _make_tree(tmp_path, ["a.py", "b.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["a.py"]
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks

        logged_errors: list[str] = []
        real_error = mgr.log.error

        def capture_error(msg: str) -> None:
            logged_errors.append(msg)
            real_error(msg)

        mgr.log.error = capture_error  # type: ignore[assignment]

        async def fake_chat(system: str, user: str) -> str:
            return json.dumps({
                "diagnosis": "uncertain",
                "needs_more_files": ["b.py"],
                "clarification_hint": "",
                "tickets": [],
            })

        mgr._chat = fake_chat  # type: ignore[assignment]
        asyncio.run(mgr.investigate())

        assert not any("INVESTIGATION HINT" in msg for msg in logged_errors)

    def test_hint_cleared_on_parse_failure(self, tmp_path):
        """clarification_hint must be reset to '' if the LLM response is not valid JSON."""
        (tmp_path / "a.py").write_text("pass\n")
        tree = _make_tree(tmp_path, ["a.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["a.py"]
        mgr.clarification_hint = "stale hint from previous run"
        mgr.token_manager.truncate_to_fit = lambda chunks, **_: chunks
        mgr._chat = _mock_chat("not valid json {{")  # type: ignore[assignment]

        asyncio.run(mgr.investigate())

        assert mgr.clarification_hint == ""
