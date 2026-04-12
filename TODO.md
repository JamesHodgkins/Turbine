# Turbine Development Roadmap

** SEE TODO_COMPLETED.md FOR PREVIOUSLY COMPLETED TASKS **

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

- [x] Define a benchmark format: a directory of `task.json` files, each with a repo snapshot, a request string, and a set of expected file diffs or assertions.
- [x] Build an `EvalRunner` that runs Turbine against each task in dry-run mode and scores the result (files changed, lines correct, tests passing).
- [x] Start with 20 representative tasks covering: single-file edits, multi-file refactors, new file creation, and bug fixes with failing tests.
- [x] Add a `turbine eval` CLI subcommand that prints a pass/fail table and an overall score.
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
- [ ] Chat session isolation (`--new-chat`, `--chat-id`, `purge`) and the Disk-as-Truth protocol are fully operational.
- [ ] A `turbine.toml` makes repeated use on a project friction-free.
- [ ] The VS Code extension supports accept/reject per worker and shows live cost.
- [ ] A new user can install, configure, and run their first successful task in under 10 minutes.
