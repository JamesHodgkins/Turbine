"""Tests for turbine.manager — Steps 2, 3, and 4.

The Mistral client is patched so no real API calls are made.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turbine.manager import Manager, Ticket, WorkerResult
from turbine.tree_mapper import FileNode, ProjectTree


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tree(root: Path, files: list[str]) -> ProjectTree:
    tree = ProjectTree(root=root)
    for rel in files:
        tree.files.append(
            FileNode(
                path=root / rel,
                relative=rel,
                size_bytes=0,
                extension=Path(rel).suffix,
            )
        )
    return tree


def _make_manager(tree: ProjectTree, root: Path, request: str = "Fix the bug") -> Manager:
    """Return a Manager with the Mistral client replaced by a MagicMock.

    The client's chat.complete_async is an AsyncMock so Worker coroutines
    can await it without error.
    """
    with patch("turbine.manager.Mistral"):
        mgr = Manager(
            tree=tree,
            user_request=request,
            project_root=root,
            api_key="test-key",
        )
    # Ensure the worker can await the client's chat method
    mgr._client.chat.complete_async = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content="No changes needed."))]
        )
    )
    return mgr


def _mock_chat(text: str) -> AsyncMock:
    """Return an AsyncMock that, when awaited, yields *text* (the final string).

    This replaces ``mgr._chat`` directly — so the mock returns the string
    that ``_chat`` itself would return, not the raw Mistral response object.
    """
    return AsyncMock(return_value=text)


# ---------------------------------------------------------------------------
# Ticket dataclass
# ---------------------------------------------------------------------------

class TestTicket:
    def test_to_dict_roundtrip(self):
        t = Ticket(
            id="ticket-1",
            description="Add logging",
            relevant_files=["src/main.py"],
            context="Use structlog",
        )
        d = t.to_dict()
        assert d["id"] == "ticket-1"
        assert d["relevant_files"] == ["src/main.py"]
        assert d["context"] == "Use structlog"


# ---------------------------------------------------------------------------
# Step 2: Preprocess
# ---------------------------------------------------------------------------

class TestPreprocess:
    def setup_method(self):
        self.root = Path("/fake/project")
        self.tree = _make_tree(self.root, ["src/main.py", "src/utils.py", "README.md"])

    def test_returns_llm_selected_files(self, tmp_path):
        mgr = _make_manager(self.tree, self.root)
        llm_answer = json.dumps(["src/main.py", "src/utils.py"])
        mgr._chat = _mock_chat(llm_answer)

        result = asyncio.run(mgr.preprocess())

        assert result == ["src/main.py", "src/utils.py"]
        assert mgr.relevant_files == ["src/main.py", "src/utils.py"]

    def test_falls_back_to_all_files_on_bad_json(self):
        mgr = _make_manager(self.tree, self.root)
        mgr._chat = _mock_chat("not json at all")

        result = asyncio.run(mgr.preprocess())

        # Fallback: all files in tree
        assert set(result) == {"src/main.py", "src/utils.py", "README.md"}

    def test_falls_back_on_non_array_json(self):
        mgr = _make_manager(self.tree, self.root)
        mgr._chat = _mock_chat('{"key": "value"}')

        result = asyncio.run(mgr.preprocess())

        assert set(result) == {"src/main.py", "src/utils.py", "README.md"}


# ---------------------------------------------------------------------------
# Step 3: Investigate
# ---------------------------------------------------------------------------

class TestInvestigate:
    def setup_method(self):
        self.root = Path("/fake/project")
        self.tree = _make_tree(self.root, ["src/main.py"])

    def _valid_investigation(self) -> str:
        return json.dumps({
            "diagnosis": "Missing error handler",
            "tickets": [
                {
                    "id": "ticket-1",
                    "description": "Add error handler to main",
                    "relevant_files": ["src/main.py"],
                    "context": "Use try/except",
                }
            ],
        })

    def test_parses_diagnosis_and_tickets(self, tmp_path):
        # Write a real file so investigate can read it
        src = tmp_path / "src" / "main.py"
        src.parent.mkdir(parents=True)
        src.write_text("def main(): pass\n")

        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        mgr._chat = _mock_chat(self._valid_investigation())

        tickets = asyncio.run(mgr.investigate())

        assert mgr.diagnosis == "Missing error handler"
        assert len(tickets) == 1
        assert tickets[0].id == "ticket-1"
        assert tickets[0].relevant_files == ["src/main.py"]

    def test_missing_file_is_skipped(self, tmp_path):
        tree = _make_tree(tmp_path, ["nonexistent.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["nonexistent.py"]
        mgr._chat = _mock_chat(self._valid_investigation())

        tickets = asyncio.run(mgr.investigate())
        # Passes without raising; LLM still produces tickets
        assert isinstance(tickets, list)

    def test_bad_json_returns_empty_tickets(self, tmp_path):
        src = tmp_path / "a.py"
        src.write_text("x = 1\n")

        tree = _make_tree(tmp_path, ["a.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["a.py"]
        mgr._chat = _mock_chat("totally wrong")

        tickets = asyncio.run(mgr.investigate())
        assert tickets == []

    def test_loads_files_into_vfs(self, tmp_path):
        src = tmp_path / "a.py"
        src.write_text("x = 1\n")

        tree = _make_tree(tmp_path, ["a.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["a.py"]
        mgr._chat = _mock_chat(self._valid_investigation())

        asyncio.run(mgr.investigate())

        snapshot = mgr.vfs.get_snapshot("a.py")
        assert snapshot is not None
        assert snapshot[0] == "x = 1"


# ---------------------------------------------------------------------------
# Step 4: Delegate
# ---------------------------------------------------------------------------

class TestDelegate:
    def setup_method(self):
        self.root = Path("/fake/project")
        self.tree = _make_tree(self.root, [])

    def _manager_with_tickets(self, n: int) -> Manager:
        mgr = _make_manager(self.tree, self.root)
        mgr.tickets = [
            Ticket(id=f"ticket-{i+1}", description=f"Task {i+1}", relevant_files=[])
            for i in range(n)
        ]
        return mgr

    def test_no_tickets_returns_empty(self):
        mgr = _make_manager(self.tree, self.root)
        results = asyncio.run(mgr.delegate())
        assert results == []

    def test_one_result_per_ticket(self):
        mgr = self._manager_with_tickets(3)
        results = asyncio.run(mgr.delegate())
        assert len(results) == 3

    def test_all_stub_workers_succeed(self):
        mgr = self._manager_with_tickets(2)
        results = asyncio.run(mgr.delegate())
        assert all(r.success for r in results)

    def test_ticket_ids_preserved(self):
        mgr = self._manager_with_tickets(3)
        results = asyncio.run(mgr.delegate())
        ids = {r.ticket_id for r in results}
        assert ids == {"ticket-1", "ticket-2", "ticket-3"}

    def test_max_workers_respected(self):
        """Semaphore should not cause a deadlock or crash with many tickets."""
        mgr = self._manager_with_tickets(10)
        mgr.max_workers = 2
        results = asyncio.run(mgr.delegate())
        assert len(results) == 10

    def test_stores_results_on_instance(self):
        mgr = self._manager_with_tickets(2)
        asyncio.run(mgr.delegate())
        assert len(mgr.worker_results) == 2


# ---------------------------------------------------------------------------
# WorkerResult dataclass
# ---------------------------------------------------------------------------

class TestWorkerResult:
    def test_defaults(self):
        r = WorkerResult(ticket_id="ticket-1", success=True)
        assert r.proposed_diff == ""
        assert r.error == ""

    def test_failure_fields(self):
        r = WorkerResult(ticket_id="ticket-1", success=False, error="timeout")
        assert not r.success
        assert r.error == "timeout"
