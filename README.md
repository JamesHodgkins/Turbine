# Turbine

**A parallel agentic code engine. Bring your own API key. Own your tools.**

Turbine takes a natural-language request and makes code changes across your project —
safely, in parallel, with nothing written to disk until you approve it.

Most AI coding agents work sequentially: one file, one change, one round-trip at a time.
Turbine decomposes a task into independent sub-tasks and executes them concurrently,
staging every proposed change in a Virtual File System before a single byte hits disk.
For wide, cross-cutting changes across a codebase, this is meaningfully faster and safer.

> **Status:** Active development. Core pipeline (Phases 1–6) is complete and functional.
> Git integration, new file creation, and the dual-mode router are in progress.
> See [TODO.md](TODO.md) for the full roadmap.

---

## How it works

```
  User request
       │
       ▼
  ┌─────────────────────────────────────────────┐
  │  1. Discovery    Map the project file tree  │
  │  2. Preprocess   LLM selects relevant files │
  │  3. Investigate  Diagnose → decompose into  │
  │                  independent tickets        │
  └──────────────────────┬──────────────────────┘
                         │
              ┌──────────▼──────────┐
              │       ROUTER        │  (Phase 19)
              └────┬──────────┬─────┘
                   │          │
          ┌────────▼───┐  ┌───▼──────────────┐
          │ WIDE MODE  │  │   DEEP MODE       │
          │            │  │                   │
          │ Parallel   │  │ Tool-calling      │
          │ worker     │  │ agent loop:       │
          │ pool —     │  │ read / write /    │
          │ one LLM    │  │ search / test     │
          │ context    │  │ until done        │
          │ per ticket │  │                   │
          └────────┬───┘  └───┬──────────────┘
                   │          │
              ┌────▼──────────▼─────┐
              │  Virtual File System │
              │  Staged in-memory.   │
              │  Conflict-detected.  │
              │  Nothing touches     │
              │  disk until you say. │
              └──────────┬──────────┘
                         │
  ┌──────────────────────▼──────────────────────┐
  │  5. Commit    CommitEngine writes to disk   │
  │               (optional manual review gate) │
  │  6. Verify    TestRunner → repair loop      │
  └─────────────────────────────────────────────┘
```

**Wide Mode** is for tasks with genuine parallelism: applying a pattern across many
independent modules, updating all API endpoints, generating tests for multiple classes.
Workers run concurrently; the VFS detects and resolves any line-range conflicts between them.

**Deep Mode** *(Phase 19)* is for tasks that require sequential reasoning: bug fixes,
features that thread through multiple layers, refactors with shared dependencies.
A single tool-calling agent iterates — reading, writing, searching, running tests — until
the task is complete or a human review is required.

The router selects automatically after Step 3, based on ticket count and task structure.
Both modes write through the same VFS and share the same commit, review, and test infrastructure.

---

## Key properties

**Safe staging.** No file is modified until the VFS is committed. Use `--dry-run` to
simulate the entire run without writing anything, or `--review` to inspect a coloured
unified diff and confirm before any write.

**Conflict detection.** When multiple workers target the same codebase, the VFS tracks
every proposed hunk by line range. Overlapping changes are caught before staging;
the conflicting worker receives constraint feedback and revises.

**Repair loop.** After commit, configured test commands run automatically. Failures are
attributed to the responsible worker, which is re-spawned with the test output as feedback.

**Bring your own key.** Turbine runs locally. The only external call is to your chosen
LLM provider. No telemetry, no cloud sync, no subscription.

---

## Installation

```bash
# From PyPI (once published)
pip install turbine-engine

# From source
git clone https://github.com/your-username/turbine
cd turbine
pip install -e .
```

Add your API key to a `.env` file in the project root:

```
MISTRAL_API_KEY=your_key_here
```

---

## Usage

```bash
# Run on the current directory
turbine . "add input validation to the registration form"

# Specify a different project root
turbine ~/projects/myapp "migrate all logging calls to structlog"

# Verify changes with tests
turbine . "refactor the payment module" --test "pytest tests/" --test "mypy src/"

# Simulate without writing to disk
turbine . "rename UserRecord to UserProfile everywhere" --dry-run

# Review diff before committing
turbine . "update the API client to handle rate limiting" --review

# Suppress the Rich dashboard (e.g. in scripts)
turbine . "add docstrings to all public methods" --no-ui

# Emit newline-delimited JSON events (for IDE and tooling integrations)
turbine . "fix the type errors in the auth module" --json-events
```

### CLI reference

| Flag | Description |
|---|---|
| `--test CMD` | Test command to run after commit. Repeatable. |
| `--dry-run` | Simulate the full pipeline; no disk writes. |
| `--review` | Show a unified diff and prompt for confirmation before writing. |
| `--no-ui` | Disable the Rich live dashboard. |
| `--json-events` | Emit structured JSON events instead of the Rich UI. |
| `--verbose` | Print LLM responses and extra reasoning detail. |

---

## VS Code Extension

The `vscode-turbine/` directory contains a VS Code extension that spawns Turbine as a
subprocess and streams progress into a WebView panel.

```bash
cd vscode-turbine
npm install && npm run compile
# Press F5 to launch the Extension Development Host
# Run "Turbine: Run" or "Turbine: Dry Run" from the Command Palette
```

Full IDE integration — native diff viewer, per-worker accept/reject, inline diagnostics —
is planned in Phase 18.

---

## Architecture

```
turbine/
├── main.py           Entry point and CLI argument parser
├── manager.py        Steps 2–5: Preprocess, Investigate, Delegate, Verify
├── worker.py         LLM loop for a single ticket; handshake + conflict retry
├── vfs.py            VirtualFileSystem, ConflictDetector, Hunk primitives
├── commit_engine.py  Writes VFS snapshots to disk; unified diff review gate
├── test_runner.py    Async subprocess runner; failure-to-worker attribution
├── tree_mapper.py    Step 1: project file tree discovery
├── token_manager.py  Tekken tokenizer; context-window budget management
├── logger.py         Thinking / Action / Error log levels
├── ui.py             Rich live dashboard (TurbineUI, WorkerStatus, PipelineStep)
└── json_ui.py        Newline-delimited JSON event emitter (JsonEventUI)
```

The VFS is the central safety primitive. It holds an immutable **baseline** (original file
contents) and a mutable **snapshot** (post-staged state). Workers diff against the
baseline; the commit engine writes the snapshot. A rollback restores the snapshot from
the baseline by replaying only the surviving staged hunks — the disk is never involved.

---

## Current status

| Capability | Status |
|---|---|
| Core 6-step pipeline | Complete |
| Parallel workers with VFS conflict detection | Complete |
| Manual review gate (`--review`) | Complete |
| Rich live dashboard | Complete |
| Test runner + repair loop | Complete |
| VS Code extension (basic WebView) | Complete |
| Git integration | Planned — Phase 7 |
| New file creation | Planned — Phase 8 |
| Scoped edits for large files | Planned — Phase 9 |
| Dual-mode router (Wide / Deep) | Planned — Phase 19 |
| Eval harness | Planned — Phase 13 |

**Known limitations before Phase 7:**
Turbine writes directly to disk with no git branch management. Until Phase 7 ships,
always run on a clean working tree and keep a manual backup strategy. `--dry-run` and
`--review` are the safe defaults.

---

## Roadmap

See [TODO.md](TODO.md) for the full phased plan.

**Near-term (trust and safety):**
Phase 7 — Git integration · Phase 8 — New file creation · Phase 9 — Scoped edits · Phase 12 — Reliability hardening

**Medium-term (quality):**
Phase 13 — Eval harness · Phase 14 — Semantic post-commit validation · Phase 11 — Iterative investigation

**Longer-term (capability):**
Phase 19 — Dual-mode router (Wide / Deep) · Phase 18 — Full IDE integration

**North Star:** the eval suite covers 50+ tasks across Wide and Deep scenarios with a
documented baseline pass rate, git integration is live, and a new user can run their
first successful task in under 10 minutes.

---

## Contributing

Turbine is free and open-source software. Contributions are welcome.

The highest-value areas right now are:
- **Phase 7 (Git integration)** — the most important trust and safety gap
- **Phase 13 (Eval harness)** — the feedback loop the whole project depends on
- **Additional LLM providers** — the `_chat` interface in `manager.py` and `_call_with_retry` in `worker.py` are the seam points; an abstract `LLMProvider` base class would make community provider contributions clean

Before contributing a prompt change or model tweak, please run the eval suite (once it
exists) so regressions are visible. The rule is: never tune blind.

---

## License

MIT. See [LICENSE](LICENSE).
