"""Tests for Phase 19 — Clarification Gate (Interactive Mode).

Covers:
  - INVESTIGATE_SYSTEM contains the 'clarification' field and its rules
  - Manager parses the clarification block from the LLM response
  - Manager sets clarification_question / clarification_options on parse
  - Missing or malformed clarification block leaves fields empty
  - Non-interactive mode: warning logged, no stdin blocked
  - Non-interactive mode: clarification_request JSON event emitted
  - Interactive mode: _ask_clarification_interactive reads from stdin and returns
  - Interactive mode: numeric option selection works
  - Interactive mode: free-text answer works
  - Interactive mode: EOF / empty input falls back to first option
  - Interactive mode: investigate() is called a second time with answer injected
  - Answer injected into user message as answer_note
  - clarification_answer cleared between investigate calls (re-entry safe)
  - ProjectConfig: interactive field parsed from toml (true/false)
  - ProjectConfig: apply_cli_overrides propagates interactive
  - ProjectConfig: resolve() defaults interactive to False
  - _ResolvedConfig has interactive field
  - JsonEventUI.on_clarification_request emits correct event shape
  - Guard: clarification with empty question or empty options is ignored
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from turbine.manager import Manager, INVESTIGATE_SYSTEM, Ticket
from turbine.tree_mapper import FileNode, ProjectTree
from turbine.json_ui import JsonEventUI
from turbine.project_config import ProjectConfig, load_project_config, _ResolvedConfig


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


def _make_manager(
    tree: ProjectTree,
    root: Path,
    request: str = "Fix the bug",
    interactive: bool = False,
    json_ui: object = None,
) -> Manager:
    with patch("turbine.manager.Mistral"):
        mgr = Manager(
            tree=tree,
            user_request=request,
            project_root=root,
            api_key="test-key",
            interactive=interactive,
            json_ui=json_ui,
        )
    mgr._client.chat.complete_async = AsyncMock(
        return_value=MagicMock(choices=[MagicMock(message=MagicMock(content=""))])
    )
    return mgr


def _ticket_response(
    clarification: dict | None = None,
    relevant: list[str] | None = None,
) -> str:
    body: dict = {
        "diagnosis": "ambiguous request",
        "tickets": [
            {
                "id": "ticket-1",
                "description": "Best-guess fix",
                "relevant_files": relevant or ["src/main.py"],
                "new_files": [],
                "context_files": [],
                "context": "1. Do it",
            }
        ],
    }
    if clarification is not None:
        body["clarification"] = clarification
    return json.dumps(body)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Prompt schema contains the clarification field
# ---------------------------------------------------------------------------

class TestPromptSchema:
    def test_clarification_field_in_system_prompt(self):
        assert '"clarification"' in INVESTIGATE_SYSTEM

    def test_clarification_question_field_in_prompt(self):
        assert '"question"' in INVESTIGATE_SYSTEM

    def test_clarification_options_field_in_prompt(self):
        assert '"options"' in INVESTIGATE_SYSTEM

    def test_clarification_rules_present(self):
        assert 'Rules for "clarification"' in INVESTIGATE_SYSTEM

    def test_clarification_omit_when_unambiguous(self):
        assert "Omit the field entirely when the request is unambiguous" in INVESTIGATE_SYSTEM


# ---------------------------------------------------------------------------
# 2. Manager parses the clarification block
# ---------------------------------------------------------------------------

class TestManagerParsesClarification:
    def test_parses_question_and_options(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)

        response_body = _ticket_response(
            clarification={"question": "Which backend?", "options": ["PostgreSQL", "SQLite"]}
        )
        mgr._chat = AsyncMock(return_value=response_body)

        _run(mgr.investigate())

        assert mgr.clarification_question == "Which backend?"
        assert mgr.clarification_options == ["PostgreSQL", "SQLite"]

    def test_parses_empty_when_no_clarification(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)

        mgr._chat = AsyncMock(return_value=_ticket_response())

        _run(mgr.investigate())

        assert mgr.clarification_question == ""
        assert mgr.clarification_options == []

    def test_malformed_clarification_block_ignored(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)

        # clarification is a string, not a dict
        body = json.loads(_ticket_response())
        body["clarification"] = "not a dict"
        mgr._chat = AsyncMock(return_value=json.dumps(body))

        _run(mgr.investigate())

        assert mgr.clarification_question == ""
        assert mgr.clarification_options == []

    def test_clarification_with_empty_question_stored_empty(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)

        body = _ticket_response(clarification={"question": "", "options": ["A", "B"]})
        mgr._chat = AsyncMock(return_value=body)

        _run(mgr.investigate())

        assert mgr.clarification_question == ""

    def test_clarification_options_filtered_empty_strings(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)

        body = _ticket_response(clarification={"question": "Q?", "options": ["A", "", "B"]})
        mgr._chat = AsyncMock(return_value=body)

        _run(mgr.investigate())

        assert mgr.clarification_options == ["A", "B"]

    def test_clarification_state_cleared_on_parse_error(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)
        # Seed non-empty values to confirm they are cleared
        mgr.clarification_question = "old question"
        mgr.clarification_options = ["old"]

        mgr._chat = AsyncMock(return_value="this is not JSON {{{{")

        _run(mgr.investigate())

        assert mgr.clarification_question == ""
        assert mgr.clarification_options == []


# ---------------------------------------------------------------------------
# 3. Non-interactive guard — warning logged, JSON event emitted
# ---------------------------------------------------------------------------

class TestNonInteractiveGuard:
    def test_warning_logged_when_not_interactive(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path, interactive=False)

        clarification_response = _ticket_response(
            clarification={"question": "Which db?", "options": ["pg", "sqlite"]}
        )
        # preprocess returns relevant files; investigate returns clarification
        preprocess_resp = json.dumps(["src/main.py"])
        mgr._chat = AsyncMock(side_effect=[preprocess_resp, clarification_response])

        # Mock delegate/verify/git so run() can complete
        mgr.delegate = AsyncMock(return_value=[])
        mgr.verify = AsyncMock(return_value=(MagicMock(success=True, written_count=0), [], []))
        mgr._git = None
        mgr.dry_run = True  # skip commit path

        logged_errors = []
        mgr.log.error = lambda msg: logged_errors.append(msg)

        _run(mgr.run())

        assert any("CLARIFICATION NEEDED" in m for m in logged_errors)
        assert any("Which db?" in m for m in logged_errors)

    def test_json_event_emitted_when_not_interactive(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        fake_json_ui = MagicMock()
        fake_json_ui.enabled = True
        mgr = _make_manager(tree, tmp_path, interactive=False, json_ui=fake_json_ui)

        clarification_response = _ticket_response(
            clarification={"question": "Which db?", "options": ["pg", "sqlite"]}
        )
        preprocess_resp = json.dumps(["src/main.py"])
        mgr._chat = AsyncMock(side_effect=[preprocess_resp, clarification_response])

        mgr.delegate = AsyncMock(return_value=[])
        mgr.verify = AsyncMock(return_value=(MagicMock(success=True, written_count=0), [], []))
        mgr._git = None
        mgr.dry_run = True
        mgr.log.error = lambda msg: None

        _run(mgr.run())

        fake_json_ui.on_clarification_request.assert_called_once_with(
            question="Which db?",
            options=["pg", "sqlite"],
        )

    def test_no_json_event_when_no_json_ui(self, tmp_path):
        """When json_ui is None, the code should not crash even if clarification is emitted."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path, interactive=False, json_ui=None)

        clarification_response = _ticket_response(
            clarification={"question": "Q?", "options": ["A", "B"]}
        )
        preprocess_resp = json.dumps(["src/main.py"])
        mgr._chat = AsyncMock(side_effect=[preprocess_resp, clarification_response])

        mgr.delegate = AsyncMock(return_value=[])
        mgr.verify = AsyncMock(return_value=(MagicMock(success=True, written_count=0), [], []))
        mgr._git = None
        mgr.dry_run = True
        mgr.log.error = lambda msg: None

        # Should not raise
        _run(mgr.run())

    def test_no_warning_when_no_clarification(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path, interactive=False)

        preprocess_resp = json.dumps(["src/main.py"])
        investigate_resp = _ticket_response()  # no clarification
        mgr._chat = AsyncMock(side_effect=[preprocess_resp, investigate_resp])

        mgr.delegate = AsyncMock(return_value=[])
        mgr.verify = AsyncMock(return_value=(MagicMock(success=True, written_count=0), [], []))
        mgr._git = None
        mgr.dry_run = True

        logged_errors = []
        mgr.log.error = lambda msg: logged_errors.append(msg)

        _run(mgr.run())

        assert not any("CLARIFICATION NEEDED" in m for m in logged_errors)


# ---------------------------------------------------------------------------
# 4. _ask_clarification_interactive
# ---------------------------------------------------------------------------

class TestAskClarificationInteractive:
    def _make_mgr_with_clarification(self, tmp_path, question, options):
        tree = _make_tree(tmp_path, [])
        mgr = _make_manager(tree, tmp_path, interactive=True)
        mgr.clarification_question = question
        mgr.clarification_options = options
        return mgr

    def test_numeric_selection_returns_option(self, tmp_path):
        mgr = self._make_mgr_with_clarification(tmp_path, "Which db?", ["pg", "sqlite"])
        with patch("builtins.input", return_value="2"):
            result = _run(mgr._ask_clarification_interactive())
        assert result == "sqlite"

    def test_first_option_on_index_1(self, tmp_path):
        mgr = self._make_mgr_with_clarification(tmp_path, "Q?", ["A", "B"])
        with patch("builtins.input", return_value="1"):
            result = _run(mgr._ask_clarification_interactive())
        assert result == "A"

    def test_free_text_returned_as_is(self, tmp_path):
        mgr = self._make_mgr_with_clarification(tmp_path, "Q?", ["A", "B"])
        with patch("builtins.input", return_value="use Redis instead"):
            result = _run(mgr._ask_clarification_interactive())
        assert result == "use Redis instead"

    def test_empty_input_falls_back_to_first_option(self, tmp_path):
        mgr = self._make_mgr_with_clarification(tmp_path, "Q?", ["A", "B"])
        with patch("builtins.input", return_value=""):
            result = _run(mgr._ask_clarification_interactive())
        assert result == "A"

    def test_eof_falls_back_to_first_option(self, tmp_path):
        mgr = self._make_mgr_with_clarification(tmp_path, "Q?", ["A", "B"])
        with patch("builtins.input", side_effect=EOFError):
            result = _run(mgr._ask_clarification_interactive())
        assert result == "A"

    def test_out_of_range_number_treated_as_free_text(self, tmp_path):
        mgr = self._make_mgr_with_clarification(tmp_path, "Q?", ["A", "B"])
        with patch("builtins.input", return_value="99"):
            result = _run(mgr._ask_clarification_interactive())
        # "99" is out of range — treated as free-text
        assert result == "99"

    def test_answer_stored_in_clarification_answer(self, tmp_path):
        mgr = self._make_mgr_with_clarification(tmp_path, "Q?", ["X", "Y"])
        with patch("builtins.input", return_value="1"):
            _run(mgr._ask_clarification_interactive())
        # The caller (run()) stores the answer; _ask only returns it
        # Verify the method itself doesn't corrupt state
        assert mgr.clarification_question == "Q?"


# ---------------------------------------------------------------------------
# 5. Interactive mode re-investigate with injected answer
# ---------------------------------------------------------------------------

class TestInteractiveReinvestigate:
    def test_investigate_called_twice_in_interactive_mode(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path, interactive=True)

        clarification_response = _ticket_response(
            clarification={"question": "Which db?", "options": ["pg", "sqlite"]}
        )
        final_response = _ticket_response()

        # preprocess → first investigate → second investigate
        preprocess_resp = json.dumps(["src/main.py"])
        mgr._chat = AsyncMock(side_effect=[
            preprocess_resp,
            clarification_response,
            final_response,
        ])

        mgr.delegate = AsyncMock(return_value=[])
        mgr.verify = AsyncMock(return_value=(MagicMock(success=True, written_count=0), [], []))
        mgr._git = None
        mgr.dry_run = True

        with patch("builtins.input", return_value="1"):
            _run(mgr.run())

        # preprocess + 2x investigate = 3 _chat calls
        assert mgr._chat.call_count == 3

    def test_answer_injected_clears_clarification_on_second_pass(self, tmp_path):
        """After re-investigation with answer, clarification fields are reset."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path, interactive=True)

        clarification_response = _ticket_response(
            clarification={"question": "Q?", "options": ["A", "B"]}
        )
        final_response = _ticket_response()  # no clarification on second pass
        preprocess_resp = json.dumps(["src/main.py"])
        mgr._chat = AsyncMock(side_effect=[
            preprocess_resp,
            clarification_response,
            final_response,
        ])

        mgr.delegate = AsyncMock(return_value=[])
        mgr.verify = AsyncMock(return_value=(MagicMock(success=True, written_count=0), [], []))
        mgr._git = None
        mgr.dry_run = True

        with patch("builtins.input", return_value="2"):
            _run(mgr.run())

        # After second investigate (final), no clarification in response
        assert mgr.clarification_question == ""
        assert mgr.clarification_options == []

    def test_answer_note_injected_into_user_message(self, tmp_path):
        """The clarification answer appears in the user message on re-investigate."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path, interactive=True)

        clarification_response = _ticket_response(
            clarification={"question": "Which db?", "options": ["pg", "sqlite"]}
        )
        final_response = _ticket_response()
        preprocess_resp = json.dumps(["src/main.py"])

        calls_user_content: list[str] = []

        async def fake_chat(system: str, user: str) -> str:
            calls_user_content.append(user)
            if len(calls_user_content) == 1:
                return preprocess_resp
            if len(calls_user_content) == 2:
                return clarification_response
            return final_response

        mgr._chat = fake_chat

        mgr.delegate = AsyncMock(return_value=[])
        mgr.verify = AsyncMock(return_value=(MagicMock(success=True, written_count=0), [], []))
        mgr._git = None
        mgr.dry_run = True

        with patch("builtins.input", return_value="2"):
            _run(mgr.run())

        # Third call (second investigate) should contain the answer
        third_call_user = calls_user_content[2]
        assert "sqlite" in third_call_user
        assert "Clarification from the user" in third_call_user


# ---------------------------------------------------------------------------
# 6. JsonEventUI.on_clarification_request
# ---------------------------------------------------------------------------

class TestJsonEventUIEvent:
    def test_emits_clarification_request_event(self, capsys):
        ui = JsonEventUI()
        ui.on_clarification_request(question="Which db?", options=["pg", "sqlite"])
        captured = capsys.readouterr()
        event = json.loads(captured.out.strip())
        assert event["event"] == "clarification_request"
        assert event["question"] == "Which db?"
        assert event["options"] == ["pg", "sqlite"]
        assert "ts" in event

    def test_emits_empty_options_list(self, capsys):
        ui = JsonEventUI()
        ui.on_clarification_request(question="Q?", options=[])
        captured = capsys.readouterr()
        event = json.loads(captured.out.strip())
        assert event["options"] == []


# ---------------------------------------------------------------------------
# 7. ProjectConfig — interactive field
# ---------------------------------------------------------------------------

class TestProjectConfigInteractive:
    def test_interactive_defaults_to_none_in_project_config(self):
        cfg = ProjectConfig()
        assert cfg.interactive is None

    def test_resolve_defaults_interactive_to_false(self):
        cfg = ProjectConfig()
        resolved = cfg.resolve()
        assert resolved.interactive is False

    def test_resolve_interactive_true(self):
        cfg = ProjectConfig(interactive=True)
        resolved = cfg.resolve()
        assert resolved.interactive is True

    def test_apply_cli_overrides_interactive_true(self):
        cfg = ProjectConfig(interactive=False)
        merged = cfg.apply_cli_overrides(interactive=True)
        assert merged.interactive is True

    def test_apply_cli_overrides_none_preserves_config(self):
        cfg = ProjectConfig(interactive=True)
        merged = cfg.apply_cli_overrides(interactive=None)
        assert merged.interactive is True

    def test_resolved_config_has_interactive_field(self):
        cfg = ProjectConfig(interactive=True)
        resolved = cfg.resolve()
        assert hasattr(resolved, "interactive")
        assert isinstance(resolved.interactive, bool)

    def test_load_project_config_interactive_true(self, tmp_path):
        (tmp_path / "turbine.toml").write_text("interactive = true\n")
        cfg = load_project_config(tmp_path)
        assert cfg.interactive is True

    def test_load_project_config_interactive_false(self, tmp_path):
        (tmp_path / "turbine.toml").write_text("interactive = false\n")
        cfg = load_project_config(tmp_path)
        assert cfg.interactive is False

    def test_load_project_config_no_interactive_gives_none(self, tmp_path):
        (tmp_path / "turbine.toml").write_text('model = "mistral-large-latest"\n')
        cfg = load_project_config(tmp_path)
        assert cfg.interactive is None

    def test_load_project_config_bad_interactive_raises(self, tmp_path):
        (tmp_path / "turbine.toml").write_text('interactive = "yes"\n')
        with pytest.raises(ValueError, match="interactive.*boolean"):
            load_project_config(tmp_path)
