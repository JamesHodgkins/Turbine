"""Tests for Phase 20 — Dual-Mode Engine (Wide vs Deep).

Covers:
  - PipelineMode enum exists with WIDE and DEEP values
  - INVESTIGATE_SYSTEM contains 'mode' field and its rules
  - Manager._llm_mode_hint parsed from "deep"/"wide" in investigation response
  - Manager._resolve_mode(): explicit override wins
  - Manager._resolve_mode(): 1 ticket → DEEP
  - Manager._resolve_mode(): 2+ tickets → WIDE
  - Manager._resolve_mode(): 0 tickets falls back to LLM hint
  - Manager.mode set after routing in run()
  - Deep Mode: _run_deep_mode creates DeepAgent and populates worker_results
  - Deep Mode: exhaustion sets review=True
  - DeepAgent.tool_read_file reads from VFS / disk
  - DeepAgent.tool_write_file writes through VFS apply_diff
  - DeepAgent.tool_search returns matching lines from VFS snapshots
  - DeepAgent.tool_search returns no-match message when nothing found
  - DeepAgent._parse_tool_call parses valid tool_call XML
  - DeepAgent._parse_tool_call returns empty on garbage input
  - DeepAgent done() exits loop and returns success
  - DeepAgent exhaustion returns exhausted=True
  - TurbineUI.on_mode is a no-op when disabled
  - TurbineUI.on_deep_iteration updates worker attempt and detail
  - JsonEventUI.on_mode emits correct event
  - JsonEventUI.on_deep_iteration emits correct event
  - Eval tasks 021-030 are in deep-bug-fix / deep-refactor category
  - Eval tasks 031-040 are in wide-multi-file category
  - Eval tasks 021-030 each have exactly one file in snapshot
  - Eval tasks 031-040 each have 2+ files in snapshot
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turbine.manager import Manager, PipelineMode, INVESTIGATE_SYSTEM, Ticket
from turbine.deep_agent import DeepAgent, DeepAgentResult
from turbine.json_ui import JsonEventUI
from turbine.ui import TurbineUI
from turbine.tree_mapper import FileNode, ProjectTree
from turbine.vfs import VirtualFileSystem


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
    request: str = "Fix it",
    mode_override: PipelineMode | None = None,
    json_ui: object = None,
) -> Manager:
    with patch("turbine.manager.Mistral"):
        mgr = Manager(
            tree=tree,
            user_request=request,
            project_root=root,
            api_key="test-key",
            mode_override=mode_override,
            json_ui=json_ui,
        )
    mgr._client.chat.complete_async = AsyncMock(
        return_value=MagicMock(choices=[MagicMock(message=MagicMock(content=""))])
    )
    return mgr


def _ticket(id: str = "ticket-1", files: list[str] | None = None) -> Ticket:
    return Ticket(
        id=id,
        description="do something",
        relevant_files=files or ["src/main.py"],
    )


def _make_deep_agent(
    vfs: VirtualFileSystem | None = None,
    mgr: Manager | None = None,
    tmp_path: Path | None = None,
    max_iterations: int = 5,
) -> DeepAgent:
    if tmp_path is None:
        tmp_path = Path(".")
    if vfs is None:
        vfs = VirtualFileSystem()
    if mgr is None:
        tree = _make_tree(tmp_path, [])
        mgr = _make_manager(tree, tmp_path)
    ticket = _ticket(files=[])
    return DeepAgent(ticket=ticket, vfs=vfs, manager=mgr, max_iterations=max_iterations)


# ---------------------------------------------------------------------------
# 1. PipelineMode enum
# ---------------------------------------------------------------------------

class TestPipelineModeEnum:
    def test_wide_value(self):
        assert PipelineMode.WIDE.value == "wide"

    def test_deep_value(self):
        assert PipelineMode.DEEP.value == "deep"

    def test_from_string_wide(self):
        assert PipelineMode("wide") == PipelineMode.WIDE

    def test_from_string_deep(self):
        assert PipelineMode("deep") == PipelineMode.DEEP


# ---------------------------------------------------------------------------
# 2. Prompt schema
# ---------------------------------------------------------------------------

class TestPromptSchema:
    def test_mode_field_in_system_prompt(self):
        assert '"mode"' in INVESTIGATE_SYSTEM

    def test_mode_rules_present(self):
        assert 'Rules for "mode"' in INVESTIGATE_SYSTEM

    def test_wide_mentioned(self):
        assert '"wide"' in INVESTIGATE_SYSTEM

    def test_deep_mentioned(self):
        assert '"deep"' in INVESTIGATE_SYSTEM


# ---------------------------------------------------------------------------
# 3. Manager._resolve_mode
# ---------------------------------------------------------------------------

class TestResolveMode:
    def _mgr(self, tmp_path: Path, **kw) -> Manager:
        tree = _make_tree(tmp_path, [])
        return _make_manager(tree, tmp_path, **kw)

    def test_override_wins_over_ticket_count(self, tmp_path):
        mgr = self._mgr(tmp_path, mode_override=PipelineMode.WIDE)
        mgr.tickets = [_ticket()]  # 1 ticket would normally give DEEP
        assert mgr._resolve_mode() == PipelineMode.WIDE

    def test_override_deep_forces_deep(self, tmp_path):
        mgr = self._mgr(tmp_path, mode_override=PipelineMode.DEEP)
        mgr.tickets = [_ticket("t1"), _ticket("t2")]  # 2 tickets would normally give WIDE
        assert mgr._resolve_mode() == PipelineMode.DEEP

    def test_one_ticket_gives_deep(self, tmp_path):
        mgr = self._mgr(tmp_path)
        mgr.tickets = [_ticket()]
        assert mgr._resolve_mode() == PipelineMode.DEEP

    def test_two_tickets_give_wide(self, tmp_path):
        mgr = self._mgr(tmp_path)
        mgr.tickets = [_ticket("t1"), _ticket("t2")]
        assert mgr._resolve_mode() == PipelineMode.WIDE

    def test_zero_tickets_uses_llm_hint_deep(self, tmp_path):
        mgr = self._mgr(tmp_path)
        mgr.tickets = []
        mgr._llm_mode_hint = PipelineMode.DEEP
        assert mgr._resolve_mode() == PipelineMode.DEEP

    def test_zero_tickets_uses_llm_hint_wide(self, tmp_path):
        mgr = self._mgr(tmp_path)
        mgr.tickets = []
        mgr._llm_mode_hint = PipelineMode.WIDE
        assert mgr._resolve_mode() == PipelineMode.WIDE

    def test_no_override_none(self, tmp_path):
        mgr = self._mgr(tmp_path)
        assert mgr.mode_override is None


# ---------------------------------------------------------------------------
# 4. investigate() parses mode hint
# ---------------------------------------------------------------------------

class TestInvestigateParsesMode:
    def test_parses_deep_hint(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["main.py"])
        mgr = _make_manager(tree, tmp_path)

        body = json.dumps({
            "diagnosis": "bug",
            "mode": "deep",
            "tickets": [{
                "id": "ticket-1", "description": "fix", "relevant_files": ["main.py"],
                "new_files": [], "context_files": [], "context": "1. fix"
            }]
        })
        mgr._chat = AsyncMock(return_value=body)
        asyncio.run(mgr.investigate())
        assert mgr._llm_mode_hint == PipelineMode.DEEP

    def test_parses_wide_hint(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["main.py"])
        mgr = _make_manager(tree, tmp_path)

        body = json.dumps({
            "diagnosis": "task",
            "mode": "wide",
            "tickets": [{
                "id": "ticket-1", "description": "fix", "relevant_files": ["main.py"],
                "new_files": [], "context_files": [], "context": "1. fix"
            }]
        })
        mgr._chat = AsyncMock(return_value=body)
        asyncio.run(mgr.investigate())
        assert mgr._llm_mode_hint == PipelineMode.WIDE

    def test_unknown_mode_defaults_to_wide(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["main.py"])
        mgr = _make_manager(tree, tmp_path)

        body = json.dumps({
            "diagnosis": "task",
            "mode": "turbo",
            "tickets": [{
                "id": "ticket-1", "description": "fix", "relevant_files": ["main.py"],
                "new_files": [], "context_files": [], "context": "1. fix"
            }]
        })
        mgr._chat = AsyncMock(return_value=body)
        asyncio.run(mgr.investigate())
        assert mgr._llm_mode_hint == PipelineMode.WIDE

    def test_missing_mode_defaults_to_wide(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1")
        tree = _make_tree(tmp_path, ["main.py"])
        mgr = _make_manager(tree, tmp_path)

        body = json.dumps({
            "diagnosis": "task",
            "tickets": [{
                "id": "ticket-1", "description": "fix", "relevant_files": ["main.py"],
                "new_files": [], "context_files": [], "context": "1. fix"
            }]
        })
        mgr._chat = AsyncMock(return_value=body)
        asyncio.run(mgr.investigate())
        assert mgr._llm_mode_hint == PipelineMode.WIDE


# ---------------------------------------------------------------------------
# 5. DeepAgent._parse_tool_call
# ---------------------------------------------------------------------------

class TestParseToolCall:
    def _agent(self, tmp_path: Path) -> DeepAgent:
        return _make_deep_agent(tmp_path=tmp_path)

    def test_parses_read_file(self, tmp_path):
        agent = self._agent(tmp_path)
        text = '<tool_call><name>read_file</name><args>{"path": "foo.py"}</args></tool_call>'
        name, args = agent._parse_tool_call(text)
        assert name == "read_file"
        assert args == {"path": "foo.py"}

    def test_parses_write_file(self, tmp_path):
        agent = self._agent(tmp_path)
        text = '<tool_call><name>write_file</name><args>{"path": "x.py", "content": "x=1"}</args></tool_call>'
        name, args = agent._parse_tool_call(text)
        assert name == "write_file"
        assert args["path"] == "x.py"

    def test_parses_done(self, tmp_path):
        agent = self._agent(tmp_path)
        text = '<tool_call><name>done</name><args>{"summary": "finished"}</args></tool_call>'
        name, args = agent._parse_tool_call(text)
        assert name == "done"
        assert args["summary"] == "finished"

    def test_returns_empty_on_no_match(self, tmp_path):
        agent = self._agent(tmp_path)
        name, args = agent._parse_tool_call("just some prose text")
        assert name == ""
        assert args == {}

    def test_returns_empty_on_bad_json_args(self, tmp_path):
        agent = self._agent(tmp_path)
        text = '<tool_call><name>read_file</name><args>{bad json}</args></tool_call>'
        name, args = agent._parse_tool_call(text)
        assert name == "read_file"
        assert args == {}

    def test_empty_args_allowed(self, tmp_path):
        agent = self._agent(tmp_path)
        text = '<tool_call><name>run_tests</name><args></args></tool_call>'
        name, args = agent._parse_tool_call(text)
        assert name == "run_tests"
        assert args == {}


# ---------------------------------------------------------------------------
# 6. DeepAgent tool implementations
# ---------------------------------------------------------------------------

class TestDeepAgentTools:
    def test_read_file_from_vfs(self, tmp_path):
        vfs = VirtualFileSystem()
        vfs.load_text("foo.py", "x = 1\ny = 2")
        agent = _make_deep_agent(vfs=vfs, tmp_path=tmp_path)
        result = agent._tool_read_file("foo.py")
        assert "x = 1" in result
        assert "foo.py" in result

    def test_read_file_from_disk(self, tmp_path):
        (tmp_path / "bar.py").write_text("z = 99")
        vfs = VirtualFileSystem()
        tree = _make_tree(tmp_path, ["bar.py"])
        mgr = _make_manager(tree, tmp_path)
        agent = DeepAgent(ticket=_ticket(files=["bar.py"]), vfs=vfs, manager=mgr, max_iterations=5)
        result = agent._tool_read_file("bar.py")
        assert "z = 99" in result

    def test_read_file_not_found(self, tmp_path):
        agent = _make_deep_agent(tmp_path=tmp_path)
        result = agent._tool_read_file("missing.py")
        assert "not found" in result.lower() or "error" in result.lower()

    def test_read_file_no_path(self, tmp_path):
        agent = _make_deep_agent(tmp_path=tmp_path)
        result = agent._tool_read_file("")
        assert "error" in result.lower()

    def test_write_file_updates_vfs(self, tmp_path):
        vfs = VirtualFileSystem()
        vfs.load_text("out.py", "x = 0")
        agent = _make_deep_agent(vfs=vfs, tmp_path=tmp_path)
        result = agent._tool_write_file("out.py", "x = 42\n")
        assert "written" in result
        snapshot = vfs.get_snapshot("out.py")
        assert snapshot is not None
        assert any("42" in line for line in snapshot)

    def test_write_file_unchanged_content(self, tmp_path):
        vfs = VirtualFileSystem()
        vfs.load_text("same.py", "x = 1")
        agent = _make_deep_agent(vfs=vfs, tmp_path=tmp_path)
        result = agent._tool_write_file("same.py", "x = 1")
        assert "unchanged" in result

    def test_write_file_adds_to_files_written(self, tmp_path):
        vfs = VirtualFileSystem()
        vfs.load_text("a.py", "a = 0")
        agent = _make_deep_agent(vfs=vfs, tmp_path=tmp_path)
        agent._tool_write_file("a.py", "a = 1\n")
        assert "a.py" in agent._files_written

    def test_search_finds_match(self, tmp_path):
        vfs = VirtualFileSystem()
        vfs.load_text("code.py", "def foo():\n    return 42\n")
        agent = _make_deep_agent(vfs=vfs, tmp_path=tmp_path)
        result = agent._tool_search("def foo")
        assert "code.py" in result
        assert "def foo" in result

    def test_search_no_match(self, tmp_path):
        vfs = VirtualFileSystem()
        vfs.load_text("code.py", "x = 1\n")
        agent = _make_deep_agent(vfs=vfs, tmp_path=tmp_path)
        result = agent._tool_search("definitely_not_here_xyz")
        assert "no matches" in result.lower()

    def test_search_invalid_regex(self, tmp_path):
        agent = _make_deep_agent(tmp_path=tmp_path)
        result = agent._tool_search("[invalid")
        assert "error" in result.lower()

    def test_search_no_pattern(self, tmp_path):
        agent = _make_deep_agent(tmp_path=tmp_path)
        result = agent._tool_search("")
        assert "error" in result.lower()


# ---------------------------------------------------------------------------
# 7. DeepAgent run loop — done() exits
# ---------------------------------------------------------------------------

class TestDeepAgentRun:
    def _agent_with_llm(self, tmp_path: Path, responses: list[str], max_iter: int = 10) -> DeepAgent:
        vfs = VirtualFileSystem()
        tree = _make_tree(tmp_path, [])
        mgr = _make_manager(tree, tmp_path)
        mgr.ui = TurbineUI(enabled=False)
        mgr._json_ui = None
        mgr.diagnosis = ""

        async def fake_llm(*_, **__):
            return MagicMock(
                choices=[MagicMock(message=MagicMock(content=responses.pop(0)))],
                usage=None,
            )
        mgr._client.chat.complete_async = fake_llm

        ticket = _ticket(files=[])
        return DeepAgent(ticket=ticket, vfs=vfs, manager=mgr, max_iterations=max_iter)

    def test_done_exits_loop(self, tmp_path):
        done_response = '<tool_call><name>done</name><args>{"summary": "all done"}</args></tool_call>'
        agent = self._agent_with_llm(tmp_path, [done_response])
        result = asyncio.run(agent.run())
        assert result.success is True
        assert result.summary == "all done"
        assert result.iterations_used == 1
        assert result.exhausted is False

    def test_exhaustion_returns_failed(self, tmp_path):
        # Return read_file on every iteration — never calls done
        read_response = '<tool_call><name>read_file</name><args>{"path": "x.py"}</args></tool_call>'
        agent = self._agent_with_llm(tmp_path, [read_response] * 3, max_iter=3)
        result = asyncio.run(agent.run())
        assert result.success is False
        assert result.exhausted is True
        assert result.iterations_used == 3

    def test_files_written_included_in_result(self, tmp_path):
        (tmp_path / "x.py").write_text("a = 1")
        vfs = VirtualFileSystem()
        tree = _make_tree(tmp_path, ["x.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.ui = TurbineUI(enabled=False)
        mgr._json_ui = None
        mgr.diagnosis = ""

        responses = [
            '<tool_call><name>write_file</name><args>{"path": "x.py", "content": "a = 2\\n"}</args></tool_call>',
            '<tool_call><name>done</name><args>{"summary": "wrote x.py"}</args></tool_call>',
        ]

        async def fake_llm(*_, **__):
            return MagicMock(
                choices=[MagicMock(message=MagicMock(content=responses.pop(0)))],
                usage=None,
            )
        mgr._client.chat.complete_async = fake_llm

        ticket = _ticket(files=["x.py"])
        agent = DeepAgent(ticket=ticket, vfs=vfs, manager=mgr, max_iterations=5)
        result = asyncio.run(agent.run())
        assert "x.py" in result.files_written


# ---------------------------------------------------------------------------
# 8. Manager._run_deep_mode — worker_results populated
# ---------------------------------------------------------------------------

class TestRunDeepMode:
    def test_deep_mode_populates_worker_results(self, tmp_path):
        tree = _make_tree(tmp_path, [])
        mgr = _make_manager(tree, tmp_path)
        mgr.tickets = [_ticket()]
        mgr.diagnosis = "bug"
        mgr.ui = TurbineUI(enabled=False)
        mgr._json_ui = None

        done_response = '<tool_call><name>done</name><args>{"summary": "fixed"}</args></tool_call>'

        async def fake_llm(*_, **__):
            return MagicMock(
                choices=[MagicMock(message=MagicMock(content=done_response))],
                usage=None,
            )
        mgr._client.chat.complete_async = fake_llm

        asyncio.run(mgr._run_deep_mode())
        assert len(mgr.worker_results) == 1
        assert mgr.worker_results[0].success is True

    def test_exhaustion_forces_review(self, tmp_path):
        tree = _make_tree(tmp_path, [])
        mgr = _make_manager(tree, tmp_path)
        mgr.tickets = [_ticket()]
        mgr.diagnosis = ""
        mgr.ui = TurbineUI(enabled=False)
        mgr._json_ui = None
        mgr.dry_run = False
        mgr._deep_max_iterations = 2

        read_resp = '<tool_call><name>read_file</name><args>{"path": "x.py"}</args></tool_call>'

        async def fake_llm(*_, **__):
            return MagicMock(
                choices=[MagicMock(message=MagicMock(content=read_resp))],
                usage=None,
            )
        mgr._client.chat.complete_async = fake_llm

        asyncio.run(mgr._run_deep_mode())
        assert mgr.review is True


# ---------------------------------------------------------------------------
# 9. UI hooks
# ---------------------------------------------------------------------------

class TestUIHooks:
    def test_turbineui_on_mode_noop_when_disabled(self):
        ui = TurbineUI(enabled=False)
        # Should not raise
        ui.on_mode(PipelineMode.DEEP)

    def test_turbineui_on_deep_iteration_noop_when_disabled(self):
        ui = TurbineUI(enabled=False)
        ui.on_deep_iteration("ticket-1", 3, last_tool="read_file")

    def test_json_ui_on_mode_emits_event(self, capsys):
        ui = JsonEventUI()
        ui.on_mode(PipelineMode.DEEP)
        captured = capsys.readouterr()
        event = json.loads(captured.out.strip())
        assert event["event"] == "mode"
        assert event["mode"] == "deep"

    def test_json_ui_on_mode_wide(self, capsys):
        ui = JsonEventUI()
        ui.on_mode(PipelineMode.WIDE)
        captured = capsys.readouterr()
        event = json.loads(captured.out.strip())
        assert event["mode"] == "wide"

    def test_json_ui_on_deep_iteration(self, capsys):
        ui = JsonEventUI()
        ui.on_deep_iteration("ticket-1", 5, last_tool="search")
        captured = capsys.readouterr()
        event = json.loads(captured.out.strip())
        assert event["event"] == "deep_iteration"
        assert event["ticket_id"] == "ticket-1"
        assert event["iteration"] == 5
        assert event["last_tool"] == "search"


# ---------------------------------------------------------------------------
# 10. Eval task categories and structure
# ---------------------------------------------------------------------------

EVALS_DIR = Path(__file__).parent.parent / "evals" / "tasks"


def _load_task(filename: str) -> dict:
    return json.loads((EVALS_DIR / filename).read_text(encoding="utf-8"))


class TestEvalTasksDeep:
    @pytest.mark.parametrize("num", range(21, 31))
    def test_deep_task_exists(self, num):
        matches = list(EVALS_DIR.glob(f"{num:03d}_deep_*.json"))
        assert len(matches) == 1, f"Expected exactly one task-{num:03d} deep file, found {matches}"

    @pytest.mark.parametrize("num", range(21, 31))
    def test_deep_task_category(self, num):
        matches = list(EVALS_DIR.glob(f"{num:03d}_deep_*.json"))
        task = json.loads(matches[0].read_text(encoding="utf-8"))
        assert task["category"].startswith("deep-"), (
            f"{matches[0].name}: expected category starting with 'deep-', got {task['category']!r}"
        )

    @pytest.mark.parametrize("num", range(21, 31))
    def test_deep_task_single_file_snapshot(self, num):
        matches = list(EVALS_DIR.glob(f"{num:03d}_deep_*.json"))
        task = json.loads(matches[0].read_text(encoding="utf-8"))
        assert len(task["snapshot"]) == 1, (
            f"{matches[0].name}: deep task should have exactly 1 file in snapshot"
        )

    @pytest.mark.parametrize("num", range(21, 31))
    def test_deep_task_has_assertions(self, num):
        matches = list(EVALS_DIR.glob(f"{num:03d}_deep_*.json"))
        task = json.loads(matches[0].read_text(encoding="utf-8"))
        assert len(task["assertions"]) >= 2


class TestEvalTasksWide:
    @pytest.mark.parametrize("num", range(31, 41))
    def test_wide_task_exists(self, num):
        matches = list(EVALS_DIR.glob(f"{num:03d}_wide_*.json"))
        assert len(matches) == 1, f"Expected exactly one task-{num:03d} wide file, found {matches}"

    @pytest.mark.parametrize("num", range(31, 41))
    def test_wide_task_category(self, num):
        matches = list(EVALS_DIR.glob(f"{num:03d}_wide_*.json"))
        task = json.loads(matches[0].read_text(encoding="utf-8"))
        assert task["category"].startswith("wide-"), (
            f"{matches[0].name}: expected category starting with 'wide-', got {task['category']!r}"
        )

    @pytest.mark.parametrize("num", range(31, 41))
    def test_wide_task_multi_file_snapshot(self, num):
        matches = list(EVALS_DIR.glob(f"{num:03d}_wide_*.json"))
        task = json.loads(matches[0].read_text(encoding="utf-8"))
        assert len(task["snapshot"]) >= 2, (
            f"{matches[0].name}: wide task should have 2+ files in snapshot"
        )

    @pytest.mark.parametrize("num", range(31, 41))
    def test_wide_task_has_assertions(self, num):
        matches = list(EVALS_DIR.glob(f"{num:03d}_wide_*.json"))
        task = json.loads(matches[0].read_text(encoding="utf-8"))
        assert len(task["assertions"]) >= 2
