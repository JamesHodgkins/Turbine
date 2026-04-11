"""Turbine entry point — runs the 6-step agentic engine loop."""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Force UTF-8 stdout/stderr so Rich unicode characters don't crash on Windows
# when the process is spawned without a console (e.g. from VS Code extension).
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from turbine.commit_engine import CommitEngine
from turbine.json_ui import JsonEventUI
from turbine.logger import TurbineLogger
from turbine.manager import Manager
from turbine.tree_mapper import TreeMapper
from turbine.ui import TurbineUI

load_dotenv()


async def run(
    target: str,
    request: str,
    test_commands: list[str] | None = None,
    dry_run: bool = False,
    review: bool = False,
    no_ui: bool = False,
    json_events: bool = False,
    verbose: bool = False,
) -> None:
    log = TurbineLogger()
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise EnvironmentError("MISTRAL_API_KEY not set. Add it to your .env file.")

    # Step 1: Discovery
    log.thinking(f"Mapping project tree at: {target}")
    mapper = TreeMapper(target)
    tree = mapper.map()
    log.action(f"Tree mapped — {len(tree.files)} files found.")
    log.debug(tree.summary())

    # --json-events: use structured JSON emitter; otherwise use Rich dashboard
    if json_events:
        ui: TurbineUI | JsonEventUI = JsonEventUI()
    else:
        ui_enabled = not no_ui and sys.stdout.isatty()
        ui = TurbineUI(title=target, enabled=ui_enabled)

    # Steps 2–5: Preprocess → Investigate → Delegate → Commit & Verify
    # When --review is set we defer the actual disk write to after the gate,
    # so we run with dry_run=True and call review_and_commit() manually.
    effective_dry_run = dry_run or review

    manager = Manager(
        tree=tree,
        user_request=request,
        project_root=target,
        api_key=api_key,
        test_commands=[] if review else (test_commands or []),
        dry_run=effective_dry_run,
        ui=ui,
        verbose=verbose,
    )

    with ui:
        await manager.run()

    # Phase 6: Manual review gate
    if review and not dry_run:
        engine = CommitEngine(
            vfs=manager.vfs,
            project_root=target,
            dry_run=False,
            original_snapshots=manager._original_snapshots,
        )
        commit_result = engine.review_and_commit()
        manager.commit_result = commit_result

        # Run tests after confirmed commit
        if commit_result.written_count and test_commands:
            from turbine.test_runner import TestRunner
            worker_file_map = {t.id: t.relevant_files for t in manager.tickets}
            ticket_descriptions = {t.id: t.description for t in manager.tickets}
            runner = TestRunner(
                project_root=target,
                commands=test_commands,
                worker_file_map=worker_file_map,
                ticket_descriptions=ticket_descriptions,
            )
            manager.test_results, manager.repair_tasks = await runner.run()

    # Final report
    if manager.commit_result:
        log.action(f"Commit: {manager.commit_result.summary()}")

    if manager.repair_tasks:
        log.error(
            f"{len(manager.repair_tasks)} worker(s) need repair after test failures:"
        )
        for task in manager.repair_tasks:
            log.error(f"  [{task.worker_id}] {task.ticket_description}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Turbine — Parallel Agentic Code Engine")
    parser.add_argument("target", nargs="?", default=".", help="Path to the project to analyse")
    parser.add_argument("request", help="The task or change request for Turbine to perform")
    parser.add_argument(
        "--test", metavar="CMD", action="append", dest="test_commands",
        help="Test command to run after commit (may be repeated)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate file writes without touching disk",
    )
    parser.add_argument(
        "--review", action="store_true",
        help="Show a diff preview and prompt for confirmation before writing",
    )
    parser.add_argument(
        "--no-ui", action="store_true",
        help="Disable the rich live dashboard",
    )
    parser.add_argument(
        "--json-events", action="store_true", dest="json_events",
        help="Emit newline-delimited JSON events instead of the Rich dashboard (for IDE integrations)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print LLM responses and extra reasoning detail",
    )
    args = parser.parse_args()

    asyncio.run(run(
        args.target,
        args.request,
        args.test_commands,
        args.dry_run,
        args.review,
        args.no_ui,
        args.json_events,
        args.verbose,
    ))


if __name__ == "__main__":
    main()
