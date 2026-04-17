"""Tests for Phase 10 — Read-Only Context Files for Workers.

Covers:
  - Ticket.context_files field and to_dict() serialisation
  - Manager.investigate() parses context_files from LLM response
  - Manager.investigate() loads context_file snapshots into the VFS
  - _merge_overlapping_tickets() preserves / deduplicates context_files
  - Worker._build_context_file_contents() renders a read-only section
  - Worker injects context_section into the initial user message
  - Worker._check_and_stage() rejects diffs that target context-only files
"""

from __future__ import annotations

import asyncio
import difflib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turbine.manager import Manager, Ticket, WorkerResult, _merge_overlapping_tickets
from turbine.tree_mapper import FileNode, ProjectTree
from turbine.vfs import VirtualFileSystem
from turbine.worker import Worker, WORKER_USER_TEMPLATE


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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
    max_handshake_attempts: int = 3,
    max_api_retries: int = 1,
) -> Worker:
    return Worker(
        ticket=ticket,
        vfs=vfs,
        client=_make_client(response_text),
        model="mistral-large-latest",
        token_manager=_make_token_manager(),
        max_handshake_attempts=max_handshake_attempts,
        max_api_retries=max_api_retries,
    )


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


# ---------------------------------------------------------------------------
# Ticket dataclass
# ---------------------------------------------------------------------------

class TestTicketContextFiles:
    def test_default_empty(self):
        t = Ticket(id="t1", description="x", relevant_files=["a.py"])
        assert t.context_files == []

    def test_to_dict_includes_context_files(self):
        t = Ticket(
            id="t1",
            description="x",
            relevant_files=["a.py"],
            context_files=["shared/types.py"],
        )
        d = t.to_dict()
        assert d["context_files"] == ["shared/types.py"]

    def test_to_dict_empty_context_files(self):
        t = Ticket(id="t1", description="x", relevant_files=["a.py"])
        assert t.to_dict()["context_files"] == []


# ---------------------------------------------------------------------------
# _merge_overlapping_tickets — context_files handling
# ---------------------------------------------------------------------------

class TestMergeContextFiles:
    def test_context_files_combined_on_merge(self):
        """When two tickets are merged (shared relevant file), their context_files
        should be unioned in the result."""
        t1 = Ticket(
            id="ticket-1",
            description="A",
            relevant_files=["shared.py"],
            context_files=["types.py"],
        )
        t2 = Ticket(
            id="ticket-2",
            description="B",
            relevant_files=["shared.py"],  # same file → will be merged
            context_files=["constants.py"],
        )
        merged = _merge_overlapping_tickets([t1, t2])
        assert len(merged) == 1
        ctx = set(merged[0].context_files)
        assert ctx == {"types.py", "constants.py"}

    def test_context_files_deduped_on_merge(self):
        """Duplicate context_files entries should appear only once after merge."""
        t1 = Ticket(
            id="ticket-1",
            description="A",
            relevant_files=["shared.py"],
            context_files=["types.py"],
        )
        t2 = Ticket(
            id="ticket-2",
            description="B",
            relevant_files=["shared.py"],
            context_files=["types.py"],  # same context file
        )
        merged = _merge_overlapping_tickets([t1, t2])
        assert merged[0].context_files.count("types.py") == 1

    def test_context_file_promoted_to_relevant_is_removed_from_context(self):
        """If a file appears in context_files on one ticket but in relevant_files
        on the merged group, it must NOT appear in context_files of the result."""
        t1 = Ticket(
            id="ticket-1",
            description="A",
            relevant_files=["main.py"],
            context_files=["utils.py"],
        )
        t2 = Ticket(
            id="ticket-2",
            description="B",
            relevant_files=["main.py", "utils.py"],  # utils.py is writable here
            context_files=[],
        )
        merged = _merge_overlapping_tickets([t1, t2])
        assert "utils.py" not in merged[0].context_files
        assert "utils.py" in merged[0].relevant_files

    def test_no_merge_context_files_preserved(self):
        """Independent tickets keep their own context_files unchanged."""
        t1 = Ticket(
            id="ticket-1",
            description="A",
            relevant_files=["a.py"],
            context_files=["shared.py"],
        )
        t2 = Ticket(
            id="ticket-2",
            description="B",
            relevant_files=["b.py"],
            context_files=["other.py"],
        )
        merged = _merge_overlapping_tickets([t1, t2])
        assert len(merged) == 2
        by_id = {t.id: t for t in merged}
        assert by_id["ticket-1"].context_files == ["shared.py"]
        assert by_id["ticket-2"].context_files == ["other.py"]


# ---------------------------------------------------------------------------
# Manager.investigate — parsing and VFS loading
# ---------------------------------------------------------------------------

class TestInvestigateContextFiles:
    def setup_method(self):
        self.root = Path("/fake/project")
        self.tree = _make_tree(self.root, ["src/main.py", "shared/types.py"])

    def _llm_response(
        self,
        relevant: list[str] | None = None,
        context_files: list[str] | None = None,
    ) -> str:
        return json.dumps({
            "diagnosis": "needs update",
            "tickets": [
                {
                    "id": "ticket-1",
                    "description": "Update main",
                    "relevant_files": relevant or ["src/main.py"],
                    "new_files": [],
                    "context_files": context_files or [],
                    "context": "1. Do the thing",
                }
            ],
        })

    def test_context_files_parsed(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "shared").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        (tmp_path / "shared" / "types.py").write_text("MyType = str\n")
        tree = _make_tree(tmp_path, ["src/main.py", "shared/types.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        mgr._chat = _mock_chat(self._llm_response(context_files=["shared/types.py"]))

        asyncio.run(mgr.investigate())

        assert len(mgr.tickets) == 1
        assert mgr.tickets[0].context_files == ["shared/types.py"]

    def test_context_file_loaded_into_vfs(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "shared").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        (tmp_path / "shared" / "types.py").write_text("MyType = str\n")
        tree = _make_tree(tmp_path, ["src/main.py", "shared/types.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        mgr._chat = _mock_chat(self._llm_response(context_files=["shared/types.py"]))

        asyncio.run(mgr.investigate())

        snap = mgr.vfs.get_snapshot("shared/types.py")
        assert snap is not None
        assert "MyType = str" in "\n".join(snap)

    def test_missing_context_file_logs_error(self, tmp_path, capsys):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        mgr._chat = _mock_chat(
            self._llm_response(context_files=["nonexistent/types.py"])
        )

        asyncio.run(mgr.investigate())
        # The ticket still parses; the missing file is simply skipped in VFS load
        assert mgr.tickets[0].context_files == ["nonexistent/types.py"]
        assert mgr.vfs.get_snapshot("nonexistent/types.py") is None

    def test_already_loaded_relevant_file_not_reloaded(self, tmp_path):
        """A file appearing in both relevant_files (already loaded) and context_files
        should not be double-loaded — the existing VFS snapshot is reused."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        tree = _make_tree(tmp_path, ["src/main.py"])
        mgr = _make_manager(tree, tmp_path)
        mgr.relevant_files = ["src/main.py"]
        # LLM mistakenly also lists main.py as a context file
        mgr._chat = _mock_chat(
            self._llm_response(relevant=["src/main.py"], context_files=["src/main.py"])
        )

        asyncio.run(mgr.investigate())
        # Should not crash; snapshot still valid
        assert mgr.vfs.get_snapshot("src/main.py") is not None


# ---------------------------------------------------------------------------
# Worker._build_context_file_contents
# ---------------------------------------------------------------------------

class TestBuildContextFileContents:
    def _ticket(self, context_files: list[str]) -> Ticket:
        return Ticket(
            id="ticket-1",
            description="Fix it",
            relevant_files=["src/main.py"],
            context_files=context_files,
        )

    def test_empty_when_no_context_files(self):
        vfs = _make_vfs({"src/main.py": "x = 1"})
        worker = _make_worker(self._ticket([]), vfs)
        assert worker._build_context_file_contents() == ""

    def test_section_header_present(self):
        vfs = _make_vfs({"src/main.py": "x = 1", "shared/types.py": "T = str"})
        worker = _make_worker(self._ticket(["shared/types.py"]), vfs)
        section = worker._build_context_file_contents()
        assert "Read-only context" in section
        assert "shared/types.py" in section
        assert "T = str" in section

    def test_read_only_label_in_section(self):
        vfs = _make_vfs({"src/main.py": "x = 1", "shared/types.py": "T = str"})
        worker = _make_worker(self._ticket(["shared/types.py"]), vfs)
        section = worker._build_context_file_contents()
        assert "read-only" in section.lower() or "do NOT modify" in section

    def test_multiple_context_files(self):
        vfs = _make_vfs({
            "src/main.py": "x = 1",
            "types.py": "T = str",
            "constants.py": "MAX = 10",
        })
        worker = _make_worker(self._ticket(["types.py", "constants.py"]), vfs)
        section = worker._build_context_file_contents()
        assert "types.py" in section
        assert "constants.py" in section
        assert "T = str" in section
        assert "MAX = 10" in section

    def test_missing_vfs_snapshot_skipped(self):
        vfs = _make_vfs({"src/main.py": "x = 1"})
        # "missing.py" is not in the VFS
        worker = _make_worker(self._ticket(["missing.py"]), vfs)
        section = worker._build_context_file_contents()
        # Should not crash; missing file simply omitted
        assert section == ""


# ---------------------------------------------------------------------------
# Worker prompt injection
# ---------------------------------------------------------------------------

class TestWorkerPromptContextSection:
    def test_context_section_injected_in_user_message(self):
        """The initial user message must contain the read-only context section
        when context_files are present."""
        vfs = _make_vfs({
            "src/main.py": "x = 1",
            "shared/types.py": "MyType = int",
        })
        ticket = Ticket(
            id="ticket-1",
            description="Fix it",
            relevant_files=["src/main.py"],
            context_files=["shared/types.py"],
        )
        # The worker will return empty → no diff, success
        worker = _make_worker(ticket, vfs, response_text="")

        captured_messages: list = []

        async def fake_call(messages: list) -> str:
            captured_messages.extend(messages)
            return ""

        worker._call_with_retry = fake_call  # type: ignore[assignment]

        asyncio.run(worker.run())

        user_msg = next(m["content"] for m in captured_messages if m["role"] == "user")
        assert "Read-only context" in user_msg
        assert "shared/types.py" in user_msg
        assert "MyType = int" in user_msg

    def test_no_context_section_when_no_context_files(self):
        vfs = _make_vfs({"src/main.py": "x = 1"})
        ticket = Ticket(
            id="ticket-1",
            description="Fix it",
            relevant_files=["src/main.py"],
            context_files=[],
        )
        worker = _make_worker(ticket, vfs, response_text="")

        captured_messages: list = []

        async def fake_call(messages: list) -> str:
            captured_messages.extend(messages)
            return ""

        worker._call_with_retry = fake_call  # type: ignore[assignment]

        asyncio.run(worker.run())

        user_msg = next(m["content"] for m in captured_messages if m["role"] == "user")
        assert "Read-only context" not in user_msg


# ---------------------------------------------------------------------------
# Worker._check_and_stage — reject diffs targeting context-only files
# ---------------------------------------------------------------------------

class TestContextFileWriteProtection:
    def _make_diff(self, path: str, old: str, new: str) -> str:
        old_lines = [l + "\n" for l in old.splitlines()]
        new_lines = [l + "\n" for l in new.splitlines()]
        return "".join(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path}", tofile=f"b/{path}",
        ))

    def test_diff_targeting_context_file_is_rejected(self):
        vfs = _make_vfs({
            "src/main.py": "x = 1",
            "shared/types.py": "T = str",
        })
        ticket = Ticket(
            id="ticket-1",
            description="Fix it",
            relevant_files=["src/main.py"],
            context_files=["shared/types.py"],
        )
        worker = _make_worker(ticket, vfs, response_text="")

        diff = self._make_diff("shared/types.py", "T = str", "T = int")
        approved, msg = asyncio.run(worker._check_and_stage(diff))

        assert not approved
        assert "read-only" in msg.lower() or "context" in msg.lower()
        assert "shared/types.py" in msg

    def test_diff_targeting_relevant_file_is_allowed(self):
        vfs = _make_vfs({
            "src/main.py": "x = 1\n",
            "shared/types.py": "T = str",
        })
        ticket = Ticket(
            id="ticket-1",
            description="Fix it",
            relevant_files=["src/main.py"],
            context_files=["shared/types.py"],
        )
        worker = _make_worker(ticket, vfs, response_text="")

        diff = self._make_diff("src/main.py", "x = 1", "x = 2")
        approved, msg = asyncio.run(worker._check_and_stage(diff))

        assert approved
        assert msg == ""

    def test_context_file_snapshot_unchanged_after_rejection(self):
        """After a rejected diff, the context file's VFS snapshot must be unmodified."""
        original_content = "T = str"
        vfs = _make_vfs({
            "src/main.py": "x = 1",
            "shared/types.py": original_content,
        })
        ticket = Ticket(
            id="ticket-1",
            description="Fix it",
            relevant_files=["src/main.py"],
            context_files=["shared/types.py"],
        )
        worker = _make_worker(ticket, vfs, response_text="")

        diff = self._make_diff("shared/types.py", "T = str", "T = int")
        asyncio.run(worker._check_and_stage(diff))

        snap = vfs.get_snapshot("shared/types.py")
        assert snap is not None
        assert "\n".join(snap) == original_content

    def test_no_context_files_no_false_rejection(self):
        """Workers without context_files should have no write restrictions from Phase 10."""
        vfs = _make_vfs({"src/main.py": "x = 1\n"})
        ticket = Ticket(
            id="ticket-1",
            description="Fix it",
            relevant_files=["src/main.py"],
            context_files=[],
        )
        worker = _make_worker(ticket, vfs, response_text="")

        diff = self._make_diff("src/main.py", "x = 1", "x = 99")
        approved, _ = asyncio.run(worker._check_and_stage(diff))

        assert approved

    def test_empty_diff_always_approved(self):
        """An empty diff must still be approved even when context_files are set."""
        vfs = _make_vfs({"src/main.py": "x = 1", "shared/types.py": "T = str"})
        ticket = Ticket(
            id="ticket-1",
            description="Fix it",
            relevant_files=["src/main.py"],
            context_files=["shared/types.py"],
        )
        worker = _make_worker(ticket, vfs, response_text="")

        approved, _ = asyncio.run(worker._check_and_stage(""))
        assert approved
