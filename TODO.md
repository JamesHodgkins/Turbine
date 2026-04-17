# Turbine Development Roadmap

** SEE TODO_COMPLETED.md FOR PREVIOUSLY COMPLETED TASKS **


## Phase 21 — Parallelism Telemetry & Validation

- [ ] Log the actual peak concurrent worker count for every run (not just `max_workers` — the actual observed concurrency).
- [ ] Add a `parallelism_ratio` field to the `JsonEventUI` `done` event: `actual_concurrent / total_workers`.
- [ ] Confirm that Wide Mode eval tasks (from Phase 20) produce a ratio > 1; confirm Deep Mode eval tasks produce ratio == 1.
- [ ] If Wide Mode tasks are consistently collapsing to ratio ≤ 1 despite genuine independence, investigate the merger logic.

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
