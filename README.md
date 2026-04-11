# Turbine: Parallel Agentic Code Engine

Turbine is a Python-based agentic coding engine designed to handle complex, multi-file
code modifications through a "Manager–Worker" architecture. Unlike sequential agents,
Turbine parallelizes tasks and implements a Virtual File System (VFS) for proactive
clash detection and conflict resolution.

## Architecture & Routine

The engine follows a strict 6-step lifecycle for every request:

1. **Discovery** — Map project file structure and symbol relationships (using LSP or MCP).
2. **Preprocess** — Identify relevant files and prune the context window for efficiency.
3. **Investigate** — Analyze selected files to diagnose issues and define specific sub-tasks.
4. **Delegate** — Spawn independent worker contexts for each sub-task. Workers report proposed changes to the main thread.
5. **Interference Checking (Manager)** — The main thread performs clash detection between workers and resolves conflicts.
6. **Implementation & Verification** — Consolidate changes into a Virtual File System (VFS), apply to disk once consensus is reached, run automated build and tests, and feed failures back to specific workers for repair.

## Tech Stack (Phase 1: CLI)

- **Orchestrator:** Python 3.12+
- **LLM Provider:** Mistral Cloud API (using `mistral-common` for token management)
- **Protocol:** Model Context Protocol (MCP) for filesystem and tool interaction
- **Conflict Resolution:** Custom logic for diff-collision detection and LLM-assisted merging

## Immediate Objectives for Claude Code

- Create project scaffolding: a clean Python project structure
- Implement the core state-machine loop (Steps 1–6) as an async Python framework
- Implement a VFS layer to handle in-memory file modifications before disk commit
- Integrate Mistral client with precise token counting for context pruning

## Future Roadmap

- Transition the CLI into a VS Code extension / background service with a sidebar UI
- Add domain-specific "Clerk" agents to verify logic against mechanical and safety standards
