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

## Phase 10 — Read-Only Context Files for Workers

- [x] Add a `context_files` field to `Ticket` (files the worker may read but not write).
- [x] Update the Investigation prompt to populate `context_files` (e.g., shared interfaces, types).
- [x] Pass context file contents in the worker prompt under a clearly labelled "Read-only context" section.
- [x] Ensure `apply_diff()` rejects diffs that target a context-only file.

## Phase 11 — Iterative Investigation

- [x] After Step 3, allow the Manager to emit a `needs_more_files` signal if the diagnosis is uncertain.
- [x] Implement a "follow-up read" loop: Manager requests additional files → TokenManager checks budget → files are loaded and re-investigated (max 2 rounds).
- [x] Inject a note into the investigation prompt when files were truncated out of context, so the LLM can flag uncertainty rather than hallucinating.
- [x] Add a `clarification_hint` field to the `needs_more_files` response — a string the LLM populates when it stops due to the round cap — that gets logged as a visible warning suggesting the user rerun with more context.

## Phase 12 — Reliability Hardening

- [x] Fix the `--review` flag: run the full repair loop after a confirmed manual commit, not before.
- [x] Improve failure attribution in `TestRunner`: propagate failures to the worker that owns the *callee* file (the one that changed the interface), not just the file mentioned in the traceback.
- [x] Increase `MAX_REPAIR_ROUNDS` and add a graduated strategy: constraint-only feedback first, full worker re-run only on the second round.
- [x] Add a `--ignore` / `.turbineignore` file to let users extend `IGNORED_DIRS` without editing source.
- [x] **Surface uncertainty explicitly:** if `truncate_to_fit` drops any files, automatically force `--review` mode and print a prominent warning naming the dropped files — never silently proceed on incomplete context.
- [x] **Unverified-run warning:** if no `--test` commands are configured and no static checker is available, print a clear warning in the final report that changes are unverified; suggest `--dry-run` or `--review` as safer modes.
- [x] **Worker confidence signal:** add an optional `<uncertain/>` tag to the worker response format; if present, flag that worker's result in the UI and force its files through the manual review gate regardless of other flags.

## Phase 13 — Eval Harness *Critical*

- [x] Define a benchmark format: a directory of `task.json` files, each with a repo snapshot, a request string, and a set of expected file diffs or assertions.
- [x] Build an `EvalRunner` that runs Turbine against each task in dry-run mode and scores the result (files changed, lines correct, tests passing).
- [x] Start with 20 representative tasks covering: single-file edits, multi-file refactors, new file creation, and bug fixes with failing tests.
- [x] Add a `turbine eval` CLI subcommand that prints a pass/fail table and an overall score.
- [x] Gate any prompt or model change on running the eval suite — never tune blind.

## Phase 14 — Semantic Post-Commit Validation

- [x] After `CommitEngine` writes to disk, run a fast static check (e.g. `mypy --no-error-summary`, `ruff check`, `tsc --noEmit`) before the test suite.
- [x] Parse the checker output to extract file + line references; attribute to responsible workers the same way `TestRunner` does.
- [x] Feed type/lint errors back into the repair loop as a first, cheap pass before running the full test suite.
- [x] Make the checker command configurable per-project (default: auto-detect from `pyproject.toml`, `tsconfig.json`, etc.).

## Phase 15 — Cost & Token Tracking

- [x] Record input and output token counts for every LLM call (Manager and each Worker).
- [x] Accumulate a per-run total and print a cost estimate in the final report (using a configurable price-per-token table).
- [x] Add a `--budget` flag (in USD) that aborts the run with a clear message if the estimate would be exceeded.
- [x] Expose token counts in the `JsonEventUI` stream so the VS Code extension can display live cost.

## Phase 16 — Streaming Worker Output

- [x] Switch worker LLM calls from `complete_async` to the streaming API; yield tokens as they arrive.
- [x] Pipe streamed tokens to the `TurbineUI` so the worker's row shows a live character count rather than a spinner.
- [x] Buffer the full response for parsing only after the stream closes — no change to downstream logic.
- [x] Update `JsonEventUI` to emit `worker_token` events so the VS Code WebView can show a live preview.

## Phase 17 — Project Configuration File ✓ COMPLETE

- [x] Support a `turbine.toml` (or `.turbine/config.toml`) in the project root.
- [x] Allow config to specify: `model`, `max_workers`, `test_commands`, `ignore_patterns`, `budget`, `static_check`.
- [x] CLI flags override config file values; config file overrides built-in defaults.
- [x] `turbine init` subcommand scaffolds a starter `turbine.toml` for a detected project type (Python, Node, etc.).

## Phase 18 — VS Code Extension: Real IDE Integration ✓ COMPLETE

- [x] Replace the WebView diff display with the native VS Code diff editor (`vscode.diff` command).
- [x] Add accept/reject buttons per worker: accepting stages the worker's files in git, rejecting reverts them.
- [x] Click-to-navigate from a worker status row to the first file it modified.
- [x] Persist run history in extension state; add a "Runs" tree view showing past Turbine sessions.
- [x] Show inline diagnostics (squiggles) for files flagged in the repair loop.

## Phase 19 — Clarification Gate (Interactive Mode)

- [x] Add an optional `clarification` field to the investigation response schema: `{"question": "...", "options": [...]}` — emitted only when the request is genuinely ambiguous, not merely uncertain about files (that is `needs_more_files`'s job).
- [x] In `--interactive` mode, pause the pipeline after Preprocess, print the question and options, read stdin, and inject the answer into the investigation user message before calling the LLM.
- [x] Emit a `clarification_request` JSON event so the VS Code extension can render an inline question widget; the extension sends the user's answer back via the process stdin.
- [x] Guard: if not in `--interactive` mode and a `clarification` is emitted, log it as a `thinking` warning and proceed — the LLM must make its best guess rather than blocking.
- [x] Add `interactive = true` as a supported key in `turbine.toml` (Phase 17) so the flag does not need to be passed on every run.

## Phase 20 — Dual-Mode Engine (Wide vs Deep)

- [x] Add a `PipelineMode` enum: `WIDE` (current parallel workers) and `DEEP` (sequential tool-calling agent).
- [x] Implement routing logic in `Manager`: if ticket count after merging == 1, select `DEEP`; if 2+ with no shared files, select `WIDE`; expose a `--mode` CLI flag to override.
- [x] Add a `mode` field to the Investigation prompt asking the LLM to recommend `wide` or `deep` based on whether sub-tasks are truly independent — use as a soft signal alongside ticket count.
- [x] Build a `DeepAgent` class with a tool-calling loop and the following tools: `read_file(path)`, `write_file(path, content)` (routes through VFS), `search(pattern)`, `run_tests()`, `done()`.
- [x] Cap Deep Mode at a configurable `max_iterations` (default: 20); treat exhaustion as a failed run requiring `--review`.
- [x] `write_file` in Deep Mode must update the VFS snapshot so CommitEngine and the review gate work identically to Wide Mode.
- [x] Update `TurbineUI` and `JsonEventUI` to display the selected mode and, in Deep Mode, show the current iteration count and last tool called.
- [x] Add 10 eval tasks for Deep Mode (bug fixes, tightly coupled refactors) and 10 for Wide Mode (pattern-across-files tasks) — confirm routing sends each to the correct pipeline.

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