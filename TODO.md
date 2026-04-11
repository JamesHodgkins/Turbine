# Turbine Development Roadmap

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

- [ ] Check for dirty working tree before any run; warn or abort if uncommitted changes exist.
- [ ] Create a feature branch (`turbine/run-<timestamp>`) before making disk changes.
- [ ] Auto-commit written files with a structured message (diagnosis summary + ticket list).
- [ ] Add `--no-branch` flag to skip branch creation for users who manage git themselves.
- [ ] Expose `git diff HEAD` as an undo hint in the final report.

## Phase 8 — New File Creation

- [ ] Extend the `Ticket` schema with an optional `new_files` list.
- [ ] Update the Investigation prompt to allow the LLM to declare new files alongside existing ones.
- [ ] Load new-file stubs into the VFS (`load_text(key, "")`) during delegation so workers can propose content.
- [ ] Ensure `CommitEngine` creates parent directories and writes new files cleanly.
- [ ] Add conflict detection for new files (two workers targeting the same new path).

## Phase 9 — Scoped Edit Format for Large Files

- [ ] Add a file-size heuristic: files over ~200 lines switch from "return complete content" to "return a list of scoped edits".
- [ ] Define a structured edit schema: `{ "function": "name", "replacement": "..." }` or `{ "lines": [start, end], "replacement": "..." }`.
- [ ] Build a `ScopedEditApplicator` that validates the edit target exists before applying.
- [ ] Fall back to complete-content mode if scoped edit parsing fails.
- [ ] **Definition integrity check (anti-hallucination guard):** before staging any diff, compare `def`/`class` definition counts in the original vs proposed output. If definitions present in the original have vanished without appearing as `-` lines in the diff, reject the proposal and send it back to the worker with an explicit list of what was lost. This catches the most common silent-deletion hallucination pattern before it reaches disk.

## Phase 10 — Read-Only Context Files for Workers

- [ ] Add a `context_files` field to `Ticket` (files the worker may read but not write).
- [ ] Update the Investigation prompt to populate `context_files` (e.g., shared interfaces, types).
- [ ] Pass context file contents in the worker prompt under a clearly labelled "Read-only context" section.
- [ ] Ensure `apply_diff()` rejects diffs that target a context-only file.

## Phase 11 — Iterative Investigation

- [ ] After Step 3, allow the Manager to emit a `needs_more_files` signal if the diagnosis is uncertain.
- [ ] Implement a "follow-up read" loop: Manager requests additional files → TokenManager checks budget → files are loaded and re-investigated (max 2 rounds).
- [ ] Inject a note into the investigation prompt when files were truncated out of context, so the LLM can flag uncertainty rather than hallucinating.

## Phase 12 — Reliability Hardening

- [ ] Fix the `--review` flag: run the full repair loop after a confirmed manual commit, not before.
- [ ] Improve failure attribution in `TestRunner`: propagate failures to the worker that owns the *callee* file (the one that changed the interface), not just the file mentioned in the traceback.
- [ ] Increase `MAX_REPAIR_ROUNDS` and add a graduated strategy: constraint-only feedback first, full worker re-run only on the second round.
- [ ] Add a `--ignore` / `.turbineignore` file to let users extend `IGNORED_DIRS` without editing source.
- [ ] **Surface uncertainty explicitly:** if `truncate_to_fit` drops any files, automatically force `--review` mode and print a prominent warning naming the dropped files — never silently proceed on incomplete context.
- [ ] **Unverified-run warning:** if no `--test` commands are configured and no static checker is available, print a clear warning in the final report that changes are unverified; suggest `--dry-run` or `--review` as safer modes.
- [ ] **Worker confidence signal:** add an optional `<uncertain/>` tag to the worker response format; if present, flag that worker's result in the UI and force its files through the manual review gate regardless of other flags.

## Phase 13 — Eval Harness *Critical*

- [ ] Define a benchmark format: a directory of `task.json` files, each with a repo snapshot, a request string, and a set of expected file diffs or assertions.
- [ ] Build an `EvalRunner` that runs Turbine against each task in dry-run mode and scores the result (files changed, lines correct, tests passing).
- [ ] Start with 20 representative tasks covering: single-file edits, multi-file refactors, new file creation, and bug fixes with failing tests.
- [ ] Add a `turbine eval` CLI subcommand that prints a pass/fail table and an overall score.
- [ ] Gate any prompt or model change on running the eval suite — never tune blind.

## Phase 14 — Semantic Post-Commit Validation

- [ ] After `CommitEngine` writes to disk, run a fast static check (e.g. `mypy --no-error-summary`, `ruff check`, `tsc --noEmit`) before the test suite.
- [ ] Parse the checker output to extract file + line references; attribute to responsible workers the same way `TestRunner` does.
- [ ] Feed type/lint errors back into the repair loop as a first, cheap pass before running the full test suite.
- [ ] Make the checker command configurable per-project (default: auto-detect from `pyproject.toml`, `tsconfig.json`, etc.).

## Phase 15 — Cost & Token Tracking

- [ ] Record input and output token counts for every LLM call (Manager and each Worker).
- [ ] Accumulate a per-run total and print a cost estimate in the final report (using a configurable price-per-token table).
- [ ] Add a `--budget` flag (in USD) that aborts the run with a clear message if the estimate would be exceeded.
- [ ] Expose token counts in the `JsonEventUI` stream so the VS Code extension can display live cost.

## Phase 16 — Streaming Worker Output

- [ ] Switch worker LLM calls from `complete_async` to the streaming API; yield tokens as they arrive.
- [ ] Pipe streamed tokens to the `TurbineUI` so the worker's row shows a live character count rather than a spinner.
- [ ] Buffer the full response for parsing only after the stream closes — no change to downstream logic.
- [ ] Update `JsonEventUI` to emit `worker_token` events so the VS Code WebView can show a live preview.

## Phase 17 — Project Configuration File

- [ ] Support a `turbine.toml` (or `.turbine/config.toml`) in the project root.
- [ ] Allow config to specify: `model`, `max_workers`, `test_commands`, `ignore_patterns`, `budget`, `static_check`.
- [ ] CLI flags override config file values; config file overrides built-in defaults.
- [ ] `turbine init` subcommand scaffolds a starter `turbine.toml` for a detected project type (Python, Node, etc.).

## Phase 18 — VS Code Extension: Real IDE Integration

- [ ] Replace the WebView diff display with the native VS Code diff editor (`vscode.diff` command).
- [ ] Add accept/reject buttons per worker: accepting stages the worker's files in git, rejecting reverts them.
- [ ] Click-to-navigate from a worker status row to the first file it modified.
- [ ] Persist run history in extension state; add a "Runs" tree view showing past Turbine sessions.
- [ ] Show inline diagnostics (squiggles) for files flagged in the repair loop.

## Phase 19 — Dual-Mode Engine (Wide vs Deep)

The router selects a pipeline after Step 3 based on ticket count and task structure.
Both modes share VFS, CommitEngine, TestRunner, and all UI infrastructure.

- [ ] Add a `PipelineMode` enum: `WIDE` (current parallel workers) and `DEEP` (sequential tool-calling agent).
- [ ] Implement routing logic in `Manager`: if ticket count after merging == 1, select `DEEP`; if 2+ with no shared files, select `WIDE`; expose a `--mode` CLI flag to override.
- [ ] Add a `mode` field to the Investigation prompt asking the LLM to recommend `wide` or `deep` based on whether sub-tasks are truly independent — use as a soft signal alongside ticket count.
- [ ] Build a `DeepAgent` class with a tool-calling loop and the following tools: `read_file(path)`, `write_file(path, content)` (routes through VFS), `search(pattern)`, `run_tests()`, `done()`.
- [ ] Cap Deep Mode at a configurable `max_iterations` (default: 20); treat exhaustion as a failed run requiring `--review`.
- [ ] `write_file` in Deep Mode must update the VFS snapshot so CommitEngine and the review gate work identically to Wide Mode.
- [ ] Update `TurbineUI` and `JsonEventUI` to display the selected mode and, in Deep Mode, show the current iteration count and last tool called.
- [ ] Add 10 eval tasks for Deep Mode (bug fixes, tightly coupled refactors) and 10 for Wide Mode (pattern-across-files tasks) — confirm routing sends each to the correct pipeline.

## Phase 20 — Parallelism Telemetry & Validation

- [ ] Log the actual peak concurrent worker count for every run (not just `max_workers` — the actual observed concurrency).
- [ ] Add a `parallelism_ratio` field to the `JsonEventUI` `done` event: `actual_concurrent / total_workers`.
- [ ] Confirm that Wide Mode eval tasks (from Phase 19) produce a ratio > 1; confirm Deep Mode eval tasks produce ratio == 1.
- [ ] If Wide Mode tasks are consistently collapsing to ratio ≤ 1 despite genuine independence, investigate the merger logic.

## North Star

Turbine is production-ready when:
- [ ] The eval suite has 50+ tasks (split across Wide and Deep mode scenarios) with a documented baseline pass rate.
- [ ] The router correctly selects Wide vs Deep mode without user intervention for representative tasks.
- [ ] Git integration, new file creation, and scoped edits are all live.
- [ ] A `turbine.toml` makes repeated use on a project friction-free.
- [ ] The VS Code extension supports accept/reject per worker and shows live cost.
- [ ] A new user can install, configure, and run their first successful task in under 10 minutes.
