# Turbine Development Roadmap

** SEE TODO_COMPLETED.md FOR PREVIOUSLY COMPLETED TASKS **


## Phase 21 — Parallelism Telemetry & Validation

- [ ] Log the actual peak concurrent worker count for every run (not just `max_workers` — the actual observed concurrency).
- [ ] Add a `parallelism_ratio` field to the `JsonEventUI` `done` event: `actual_concurrent / total_workers`.
- [ ] Confirm that Wide Mode eval tasks (from Phase 20) produce a ratio > 1; confirm Deep Mode eval tasks produce ratio == 1.
- [ ] If Wide Mode tasks are consistently collapsing to ratio ≤ 1 despite genuine independence, investigate the merger logic.

## Phase 22 — Deliberative Thinking Loop

The agent currently goes **Investigate → Delegate → Execute** with no pause for reflection. Workers receive a plan and run it; errors are only caught reactively (test failures trigger repair). This phase inserts deliberative checkpoints at three levels:

### 22.1 — Plan Review Step (between Investigate and Delegate) ✓ DONE

- [x] After ticket generation, run a dedicated **Plan Review** LLM call that reads the full ticket set and asks: *"Are these tickets complete, non-conflicting, and sufficient to solve the original request?"*
- [x] The review output should produce a structured `plan_review` object: `{ confidence: 0–1, gaps: string[], risks: string[], verdict: "proceed" | "revise" | "clarify" }`.
- [x] If `verdict == "revise"`, loop back into Investigate with the gap list appended to the prompt (cap at 2 revision rounds).
- [x] If `verdict == "clarify"`, surface the clarification question before dispatching any workers.
- [x] Surface `confidence` and `gaps` in the `JsonEventUI` `plan` event so the VS Code extension can show a pre-flight summary.

### 22.2 — Mid-Execution Reflection (per worker, before writing to VFS) ✓ DONE

- [x] Before a worker writes its first file change, run a lightweight **pre-write reflection** prompt: *"Given your plan and what you've read, does your approach still make sense? List any assumptions that could be wrong."*
- [x] If the reflection flags a blocking assumption, the worker pauses and emits a `worker_blocked` event with the assumption text; the manager can surface this to the user or attempt to resolve it automatically.
- [x] Add a `reflection_skipped` flag (default `false`) that can be set per-ticket when confidence is already high (e.g. trivial one-file edits), to avoid overhead on simple tasks.

### 22.3 — Post-Execution Synthesis (after all workers complete, before commit) ✓ DONE

- [x] Run a **synthesis** LLM call that reads all diffs produced by workers and evaluates: *"Do these changes together solve the original request without introducing regressions?"*
- [x] Synthesis output: `{ verdict: "commit" | "repair" | "abort", reason: string, suspicious_files: string[] }`.
- [x] If `verdict == "repair"`, pass `suspicious_files` and `reason` to the existing repair loop rather than using raw test output alone — this gives the repair worker richer context.
- [x] Emit a `synthesis` event in `JsonEventUI` with the verdict and reason for observability.

### 22.4 — Routing Depth (smarter Wide vs Deep selection)

- [ ] Replace the ticket-count heuristic with a **dependency graph check**: build a light DAG from ticket file assignments and detect true independence.
- [ ] Add a `complexity_score` to each ticket (estimated token cost of its pseudocode plan); if any ticket exceeds a threshold, force Deep mode for that ticket regardless of total count.
- [ ] Log the routing decision reason (count-based vs dependency-based vs complexity-based) in the `plan` event.

## North Star

Turbine is production-ready when:
- [ ] The eval suite has 50+ tasks (split across Wide and Deep mode scenarios) with a documented baseline pass rate.
- [ ] The router correctly selects Wide vs Deep mode without user intervention for representative tasks.
- [ ] Clarification gate is wired up in the VS Code extension so ambiguous requests surface an inline question widget rather than guessing.
- [ ] Git integration, new file creation, and scoped edits are all live.
- [ ] Chat session isolation (`--new-chat`, `--chat-id`, `purge`) and the Disk-as-Truth protocol are fully operational.
- [ ] A `turbine.toml` makes repeated use on a project friction-free.
- [ ] The VS Code extension supports accept/reject per worker and shows live cost.
- [ ] A new user can install, configure, and run their first successful task in under 10 minutes.
