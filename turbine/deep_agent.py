"""DeepAgent — Phase 20 sequential tool-calling agent.

Used when the Manager routes to DEEP mode (single tightly-coupled task).
The agent operates inside a tool-calling loop, reading and writing files
through the VFS, searching the project tree, optionally running tests, and
calling ``done()`` when its work is complete.

Tool set
--------
read_file(path)              Read a file from the VFS (or disk).
write_file(path, content)    Write/overwrite a file in the VFS.
search(pattern)              Grep-style search across VFS snapshots.
run_tests()                  Run the configured test commands (dry-run safe).
done(summary)                Signal completion and exit the loop.

The agent is capped at ``max_iterations`` LLM calls.  If the cap is hit
without a ``done()`` call, the run is marked as exhausted and forces
``--review`` in the calling Manager.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from turbine.manager import Manager, Ticket
    from turbine.cost_tracker import CostTracker
    from turbine.vfs import VirtualFileSystem

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_ITERATIONS = 20

_DEEP_SYSTEM = """\
You are Turbine's Deep Agent — a sequential, tool-calling coding assistant.
You operate in an iterative loop: read files, analyse the problem, write
corrected files, optionally run tests, and call done() when finished.

Available tools
---------------
read_file(path)
    Read the current content of a file.  path is relative to the project root.

write_file(path, content)
    Write (or overwrite) a file's full content.  path is relative to the
    project root.  You MUST provide the complete file content every time —
    never a diff or a partial snippet.

search(pattern)
    Search all loaded files for a regex pattern.  Returns matching lines with
    their file paths and line numbers.

run_tests()
    Run the project test suite.  Returns pass/fail + stdout.  Use this to
    verify your changes before calling done().

done(summary)
    Signal that you are finished.  summary is a short human-readable
    description of what you changed.  ALWAYS call this when the task is
    complete.

Response format
---------------
You may write a brief reasoning note (1-3 sentences) BEFORE the tool call
to explain what you observed and what you intend to do next.  Then close
with EXACTLY ONE tool call in this XML format:

<tool_call>
<name>tool_name</name>
<args>{"arg1": "value1", "arg2": "value2"}</args>
</tool_call>

Do NOT call multiple tools in one turn.
After calling done() the loop ends — do not produce any further output.

Iteration budget
----------------
You have a limited number of turns.  Use read_file and search to gather
context efficiently; write only when you are confident; call done() as soon
as the task is complete.  Do NOT call read_file on the same file twice unless
you have written it in between.
"""

_DEEP_USER_TEMPLATE = """\
Diagnosis: {diagnosis}

Task: {description}

Step-by-step plan (follow this):
{plan}

Files you may read or write (relevant to this task):
{file_list}

Start by reading the files you need, then make your changes, then call done().
"""

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class DeepAgentResult:
    """Outcome of a DeepAgent run."""
    success: bool
    summary: str = ""
    iterations_used: int = 0
    exhausted: bool = False        # True when max_iterations hit without done()
    files_written: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DeepAgent
# ---------------------------------------------------------------------------

class DeepAgent:
    """Tool-calling loop agent for DEEP pipeline mode.

    Parameters
    ----------
    ticket:
        The single Ticket this agent is responsible for.
    vfs:
        The shared Virtual File System.  All writes go through VFS so
        CommitEngine and the review gate work identically to Wide Mode.
    manager:
        The parent Manager (used for LLM client, model, cost tracker,
        project_root, test_commands, UI hooks, and logging).
    max_iterations:
        Hard cap on the number of LLM turns.
    """

    def __init__(
        self,
        ticket: "Ticket",
        vfs: "VirtualFileSystem",
        manager: "Manager",
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> None:
        self.ticket = ticket
        self.vfs = vfs
        self.manager = manager
        self.max_iterations = max_iterations
        self.log = manager.log
        self._client = manager._client
        self._model = manager.model
        self._cost_tracker: "CostTracker" = manager._cost_tracker
        self._project_root = manager.project_root
        self._test_commands: list[str] = manager.test_commands
        self._ui = manager.ui
        self._json_ui = manager._json_ui

        # Conversation history for this agent
        self._messages: list[dict[str, str]] = []
        # Files written so far (relative paths)
        self._files_written: list[str] = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> DeepAgentResult:
        """Execute the tool-calling loop until done() or max_iterations."""
        ticket = self.ticket
        self.log.thinking(
            f"DeepAgent [{ticket.id}] starting — max {self.max_iterations} iterations"
        )
        # Notify UI that this "worker" is starting
        self._ui.on_worker_start(ticket.id, ticket.description)

        file_list = "\n".join(
            f"  - {f}"
            for f in (ticket.relevant_files + ticket.new_files + ticket.context_files)
        ) or "  (none specified — infer from context)"

        user_start = _DEEP_USER_TEMPLATE.format(
            diagnosis=self.manager.diagnosis or "(no diagnosis provided)",
            description=ticket.description,
            plan=ticket.context or "(no plan provided)",
            file_list=file_list,
        )

        self._messages = [{"role": "user", "content": user_start}]

        for iteration in range(1, self.max_iterations + 1):
            self.log.thinking(f"DeepAgent [{ticket.id}] iteration {iteration}/{self.max_iterations}")
            # Notify UI of iteration
            self._ui.on_deep_iteration(ticket.id, iteration, last_tool="")
            if self._json_ui is not None:
                self._json_ui.on_deep_iteration(ticket.id, iteration, last_tool="")

            response_text = await self._llm_turn()

            # Log any reasoning prose the LLM wrote before the tool call
            preamble = self._extract_preamble(response_text)
            if preamble:
                self.log.thinking(f"DeepAgent [{ticket.id}]: {preamble}")
                if self._json_ui is not None:
                    self._json_ui.log("thinking", f"DeepAgent [{ticket.id}]: {preamble}")

            tool_name, tool_args = self._parse_tool_call(response_text)
            if not tool_name:
                # No parseable tool call — treat as done with current state
                self.log.error(
                    f"DeepAgent [{ticket.id}]: could not parse tool call on iteration "
                    f"{iteration}, treating as done."
                )
                break

            log_args = self._sanitize_args_for_log(tool_name, tool_args)
            self.log.thinking(f"DeepAgent [{ticket.id}] tool: {tool_name}({log_args})")
            if self._json_ui is not None:
                self._json_ui.log("thinking", f"DeepAgent [{ticket.id}] tool: {tool_name}({log_args})")
            # Update UI with the last tool called
            self._ui.on_deep_iteration(ticket.id, iteration, last_tool=tool_name)
            if self._json_ui is not None:
                self._json_ui.on_deep_iteration(ticket.id, iteration, last_tool=tool_name)

            # Dispatch tool
            if tool_name == "done":
                summary = str(tool_args.get("summary", ""))
                self.log.action(f"DeepAgent [{ticket.id}] done: {summary}")
                self._ui.on_worker_done(
                    ticket.id, success=True, detail=summary, files=self._files_written
                )
                if self._json_ui is not None:
                    self._json_ui.on_worker_done(
                        ticket.id, success=True, detail=summary, files=self._files_written
                    )
                return DeepAgentResult(
                    success=True,
                    summary=summary,
                    iterations_used=iteration,
                    files_written=list(self._files_written),
                )

            tool_result = await self._dispatch(tool_name, tool_args)
            # Append tool result as a user turn so LLM sees the output
            self._messages.append({"role": "assistant", "content": response_text})
            self._messages.append({"role": "user", "content": f"[Tool result]\n{tool_result}"})

        # Exhausted
        self.log.error(
            f"DeepAgent [{ticket.id}] exhausted after {self.max_iterations} iteration(s) "
            "without calling done() — forcing --review."
        )
        self._ui.on_worker_done(
            ticket.id, success=False,
            detail=f"exhausted after {self.max_iterations} iterations",
        )
        if self._json_ui is not None:
            self._json_ui.on_worker_done(
                ticket.id, success=False,
                detail=f"exhausted after {self.max_iterations} iterations",
            )
        return DeepAgentResult(
            success=False,
            summary="",
            iterations_used=self.max_iterations,
            exhausted=True,
            files_written=list(self._files_written),
        )

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _llm_turn(self) -> str:
        """Send the current conversation to the LLM and return the response text."""
        import httpx

        messages = [{"role": "system", "content": _DEEP_SYSTEM}] + self._messages
        last_exc: BaseException | None = None
        for attempt in range(3):
            try:
                response = await self._client.chat.complete_async(
                    model=self._model,
                    messages=messages,
                )
                break
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        else:
            raise last_exc  # type: ignore[misc]
        usage = getattr(response, "usage", None)
        if usage and self._cost_tracker:
            self._cost_tracker.record(
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            )
        text = response.choices[0].message.content or ""
        return text

    # ------------------------------------------------------------------
    # Tool call parser
    # ------------------------------------------------------------------

    _TOOL_CALL_RE = re.compile(
        r"<tool_call>\s*<name>([\w_]+)</name>\s*<args>(.*?)</args>\s*</tool_call>",
        re.DOTALL | re.IGNORECASE,
    )

    def _sanitize_args_for_log(self, tool_name: str, args: dict[str, Any]) -> str:
        """Return a compact, human-readable representation of tool args.

        Long values (e.g. file content) are replaced with a size summary so
        the log stays readable without printing entire file contents.
        """
        parts: list[str] = []
        for key, value in args.items():
            if isinstance(value, str) and len(value) > 120:
                lines = value.count("\n") + 1
                parts.append(f"{key}=<{lines} lines>")
            else:
                parts.append(f"{key}={value!r}")
        return ", ".join(parts)

    def _extract_preamble(self, text: str) -> str:
        """Return any reasoning prose written *before* the <tool_call> block."""
        m = self._TOOL_CALL_RE.search(text)
        if not m:
            return ""
        preamble = text[:m.start()].strip()
        # Collapse internal whitespace runs to a single space for concise logging
        return " ".join(preamble.split()) if preamble else ""

    def _parse_tool_call(self, text: str) -> tuple[str, dict[str, Any]]:
        """Extract the first tool call from the LLM response."""
        import json as _json
        m = self._TOOL_CALL_RE.search(text)
        if not m:
            return "", {}
        name = m.group(1).strip()
        args_raw = m.group(2).strip()
        try:
            args = _json.loads(args_raw) if args_raw else {}
        except Exception:
            args = {}
        return name, args if isinstance(args, dict) else {}

    # ------------------------------------------------------------------
    # Tool dispatcher
    # ------------------------------------------------------------------

    async def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name == "read_file":
            return self._tool_read_file(str(args.get("path", "")))
        if name == "write_file":
            return self._tool_write_file(
                str(args.get("path", "")),
                str(args.get("content", "")),
            )
        if name == "search":
            return self._tool_search(str(args.get("pattern", "")))
        if name == "run_tests":
            return await self._tool_run_tests()
        return f"[Unknown tool: {name!r}]"

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _tool_read_file(self, path: str) -> str:
        if not path:
            return "[read_file error: no path provided]"
        # Try VFS first (may have been written earlier this session)
        snapshot = self.vfs.get_snapshot(path)
        if snapshot is not None:
            content = "\n".join(snapshot)
            return f"[{path}]\n{content}"
        # Fall back to disk
        abs_path = self._project_root / path
        if not abs_path.is_file():
            return f"[read_file error: {path!r} not found]"
        content = abs_path.read_text(encoding="utf-8", errors="replace")
        # Load into VFS so future write_file calls have a baseline
        self.vfs.load_from_disk(abs_path, relative_key=path)
        return f"[{path}]\n{content}"

    def _tool_write_file(self, path: str, content: str) -> str:
        """Write content to the VFS (not to disk — CommitEngine handles that)."""
        if not path:
            return "[write_file error: no path provided]"

        import difflib

        # Ensure there is a VFS snapshot to diff against
        if self.vfs.get_snapshot(path) is None:
            abs_path = self._project_root / path
            if abs_path.is_file():
                self.vfs.load_from_disk(abs_path, relative_key=path)
            else:
                # New file — create an empty baseline via load_text
                self.vfs.load_text(path, "")

        old_lines = self.vfs.get_snapshot(path) or []
        # Normalise to lines-with-newlines for unified_diff
        old_nl = [l + "\n" if not l.endswith("\n") else l for l in old_lines]
        new_nl = [l + "\n" if not l.endswith("\n") else l for l in content.splitlines()]

        diff = "".join(difflib.unified_diff(
            old_nl,
            new_nl,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        ))
        if not diff:
            return f"[write_file: {path!r} unchanged (content identical)]"

        try:
            self.vfs.apply_diff("deep-agent", diff)
        except (ValueError, Exception) as exc:
            return f"[write_file error applying diff to {path!r}: {exc}]"

        if path not in self._files_written:
            self._files_written.append(path)
        return f"[write_file: {path!r} written ({len(content.splitlines())} lines)]"

    def _tool_search(self, pattern: str) -> str:
        """Search all VFS snapshots for lines matching *pattern*."""
        if not pattern:
            return "[search error: no pattern provided]"
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"[search error: invalid pattern {pattern!r} — {exc}]"

        results: list[str] = []
        for rel, lines in self.vfs.all_snapshots().items():
            for lineno, line in enumerate(lines, 1):
                if regex.search(line):
                    results.append(f"{rel}:{lineno}: {line.rstrip()}")
                    if len(results) >= 50:
                        results.append("… (truncated at 50 matches)")
                        break
            if len(results) >= 50:
                break

        if not results:
            return f"[search: no matches for {pattern!r}]"
        return "\n".join(results)

    async def _tool_run_tests(self) -> str:
        """Run the project test suite and return a brief pass/fail summary."""
        if not self._test_commands:
            return "[run_tests: no test commands configured]"

        import asyncio as _asyncio
        results: list[str] = []
        for cmd in self._test_commands:
            try:
                proc = await _asyncio.create_subprocess_shell(
                    cmd,
                    cwd=self._project_root,
                    stdout=_asyncio.subprocess.PIPE,
                    stderr=_asyncio.subprocess.STDOUT,
                )
                try:
                    stdout, _ = await _asyncio.wait_for(proc.communicate(), timeout=60)
                except _asyncio.TimeoutError:
                    results.append(f"[{cmd}] TIMEOUT after 60s")
                    continue
                rc = proc.returncode
                output = stdout.decode("utf-8", errors="replace")[:500]
                status = "PASSED" if rc == 0 else f"FAILED (rc={rc})"
                results.append(f"[{cmd}] {status}\n{output}")
            except Exception as exc:
                results.append(f"[{cmd}] ERROR: {exc}")

        return "\n---\n".join(results)
