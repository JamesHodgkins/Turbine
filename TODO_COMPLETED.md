# Turbine Development Roadmap (Completed)

** SEE TODO_COMPLETED.md FOR OUTSTANDING TASKS **

## Phase 1 — Foundation & Scaffolding

- [x] Initialize Python environment (3.12+) and `.env` for Mistral API keys.
- [x] Implement Token Manager using `mistral-common` (Tekken tokenizer).
- [x] Build Step 1: Tree Mapper using Python's `os` or an MCP filesystem server.
- [x] Create a basic logging system to track "Thinking" and "Action" phases.

## Phase 2 — The Virtual File System (VFS)

- [x] Design the `VirtualFileSystem` class to hold in-memory file states.
- [x] Implement `apply_diff()` to stage changes without touching the disk.
- [x] Build a Conflict Detector to identify overlapping line ranges between multiple worker diffs.

## Phase 3 — The Manager (Dynamo)

- [x] Develop the Manager agent logic.
- [x] Prompt for Step 2: Preprocess (pruning the tree).
- [x] Prompt for Step 3: Investigation (issue diagnosis).
- [x] Task decomposition: break the prompt into JSON "tickets".
- [x] Implement an `asyncio` loop to spawn and manage multiple worker tasks.

## Phase 4 — Worker Loop (Step 4)

- [x] Create the Worker agent prompt template (domain isolation: only relevant files).
- [x] Implement the "Ready to Proceed" handshake: worker proposes change → manager checks VFS → manager approves or returns constraints.
- [x] Handle Mistral API retries and context-window monitoring.

## Phase 5 — Implementation & Verification (Steps 5 & 6)

- [x] Build the Commit Engine: write VFS changes to disk once "Ready to Proceed" is clear.
- [x] Implement the Test Runner to execute shell commands (e.g., `pytest`, `make`, `npm test`).
- [x] Capture `stderr`/`stdout` and feed failures back to the specific worker's context.

## Phase 6 — Refinement & UI

- [x] Create a rich CLI showing the status of each worker in real time.
- [x] Add a Manual Review gate before the VFS writes to disk.
- [x] Add `JsonEventUI` + `--json-events` flag to Python backend
- [x] Scaffold VS Code extension (TypeScript)
- [x] Wire subprocess runner + WebView panel
- [x] Package Python side as installable CLI (`pip install`)

## Phase 7 — Git Integration *Critical*

- [x] Check for dirty working tree before any run; warn or abort if uncommitted changes exist.
- [x] Create a feature branch (`turbine/run-<timestamp>`) before making disk changes.
- [x] Auto-commit written files with a structured message (diagnosis summary + ticket list).
- [x] Add `--no-branch` flag to skip branch creation for users who manage git themselves.
- [x] Expose `git diff HEAD` as an undo hint in the final report.

## Phase 8 — New File Creation

- [x] Extend the `Ticket` schema with an optional `new_files` list.
- [x] Update the Investigation prompt to allow the LLM to declare new files alongside existing ones.
- [x] Load new-file stubs into the VFS (`load_text(key, "")`) during delegation so workers can propose content.
- [x] Ensure `CommitEngine` creates parent directories and writes new files cleanly.
- [x] Add conflict detection for new files (two workers targeting the same new path).

## Phase 9 — Scoped Edit Format for Large Files

- [x] Add a file-size heuristic: files over ~200 lines switch from "return complete content" to "return a list of scoped edits".
- [x] Define a structured edit schema: `{ "function": "name", "replacement": "..." }` or `{ "lines": [start, end], "replacement": "..." }`.
- [x] Build a `ScopedEditApplicator` that validates the edit target exists before applying.
- [x] Fall back to complete-content mode if scoped edit parsing fails.
- [x] **Definition integrity check (anti-hallucination guard):** before staging any diff, compare `def`/`class` definition counts in the original vs proposed output. If definitions present in the original have vanished without appearing as `-` lines in the diff, reject the proposal and send it back to the worker with an explicit list of what was lost. This catches the most common silent-deletion hallucination pattern before it reaches disk.

## Phase 21 — Chat Session Management

- [x] Add `--chat-id` and `--new-chat` arguments to the `run` function and argument parser in `main.py`; generate a UUID when `--new-chat` is flagged.
- [x] Pass `chat_id` into the `Manager` constructor; `Manager` forwards it to `GitIntegration` on init.
- [x] Update `GitIntegration.create_branch` to use the naming convention `turbine/chat-{chat_id}` instead of a timestamp.
- [x] When `--new-chat` is flagged, branch from the current HEAD of the project's base branch, ensuring full isolation from other chat branches.
- [x] Add a `purge` subcommand to `main.py` that runs independently of the standard agentic lifecycle.
- [x] Implement `purge_history()` in `git_integration.py`: list all local `turbine/` prefix branches, force-delete them with `git branch -D`, and verify the user is returned to the original branch recorded during preflight.
- [x] `purge` command: delete any `.turbine.lock` files and local log directories to remove all residual on-disk state.

## Phase 22 — Git Isolation & Disk-as-Truth Protocol

- [x] Before any operation, `Manager` calls `GitIntegration.preflight()`; if dirty files are detected, skip all previous chat branches and branch immediately from the current disk state, capturing manual edits as the new baseline.
- [x] If the tree is clean, check for an existing branch matching `chat_id`; resume on it if found, otherwise create a new branch from HEAD.
- [x] Implement a `.turbine.lock` file in the project root; abort with a clear error if a second Turbine process starts while the lock exists; remove the lock on clean exit or crash recovery.
- [x] Update `auto_commit` to use `git add -- <files>` (selective staging) so only the specific relative paths Turbine modified are included, preventing pre-existing working-tree changes from leaking into the agent's commit.
- [x] Maintain the `_detect_repo` subdirectory guard: disable Git integration entirely if the target path is a subdirectory of a repo root, preventing wrong-scope branches or commits.
- [x] Expose only `merge_hint` and `undo_hint` commands in the final report; Turbine never performs merges directly, and Git hooks / user identity from the system git binary are respected automatically.