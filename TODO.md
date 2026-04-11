# Turbine Development Roadmap

## Phase 1 — Foundation & Scaffolding

- [x] Initialize Python environment (3.12+) and `.env` for Mistral API keys.
- [x] Implement Token Manager using `mistral-common` (Tekken tokenizer).
- [x] Build Step 1: Tree Mapper using Python’s `os` or an MCP filesystem server.
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