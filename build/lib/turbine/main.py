"""Turbine entry point — runs the 6-step agentic engine loop."""

import asyncio
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

# Force UTF-8 stdout/stderr so Rich unicode characters don't crash on Windows
# when the process is spawned without a console (e.g. from VS Code extension).
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from turbine.git_integration import GitIntegration
from turbine.json_ui import JsonEventUI
from turbine.logger import TurbineLogger
from turbine.manager import Manager
from turbine.tree_mapper import TreeMapper
from turbine.ui import TurbineUI

load_dotenv()


# ---------------------------------------------------------------------------
# Config file — stores API key in the platform user-config directory
# ---------------------------------------------------------------------------

def _config_path() -> Path:
    """Return the path to turbine's config file, platform-appropriate."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "turbine" / "config.json"


def _load_config() -> None:
    """Inject keys from the turbine config file into os.environ if not already set."""
    import json
    cfg = _config_path()
    if not cfg.exists():
        return
    try:
        data: dict = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return
    for key, value in data.items():
        if key not in os.environ:
            os.environ[key] = str(value)


def _config_main() -> None:
    """Entry point for `turbine config`."""
    import getpass
    import json

    args = sys.argv[2:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: turbine config set-key")
        print("       turbine config show")
        return

    subcmd = args[0]

    if subcmd == "set-key":
        cfg = _config_path()
        existing: dict = {}
        if cfg.exists():
            try:
                existing = json.loads(cfg.read_text(encoding="utf-8"))
            except Exception:
                pass
        key = getpass.getpass("Mistral API key: ").strip()
        if not key:
            print("Aborted — no key entered.")
            return
        existing["MISTRAL_API_KEY"] = key
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        print(f"Key saved to {cfg}")

    elif subcmd == "show":
        cfg = _config_path()
        if not cfg.exists():
            print(f"No config file found at {cfg}")
            return
        try:
            data: dict = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Could not read config: {exc}")
            return
        for k, v in data.items():
            # Mask all but the last 4 chars of secret-looking values
            display = f"...{str(v)[-4:]}" if len(str(v)) > 8 else "****"
            print(f"  {k} = {display}")
        print(f"  (stored at {cfg})")

    else:
        print(f"Unknown config subcommand: {subcmd!r}")
        print("Usage: turbine config set-key | show")
        sys.exit(1)


async def run(
    target: str,
    request: str,
    test_commands: list[str] | None = None,
    dry_run: bool = False,
    review: bool = False,
    no_ui: bool = False,
    json_events: bool = False,
    verbose: bool = False,
    no_branch: bool = False,
    chat_id: str | None = None,
    new_chat: bool = False,
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

    # Phase 21: --new-chat generates a fresh UUID so a new branch is always created
    if new_chat:
        chat_id = uuid.uuid4().hex[:12]
        log.thinking(f"Git: new chat session — id: {chat_id}")

    # Phase 22: Git integration — subdir warning (informational only)
    git = GitIntegration(target, no_branch=no_branch, chat_id=chat_id, new_chat=new_chat)
    if git.is_subdir_of_repo:
        log.thinking(
            "Git: target is a subdirectory of a larger repository — "
            "git integration disabled to avoid bleeding parent-repo state "
            "(branches, dirty files, commits) into this run. "
            "Run from the repository root for full git integration."
        )

    # Phase 22: Acquire process lock — abort if another Turbine is running
    try:
        git.acquire_lock()
    except RuntimeError as exc:
        log.error(str(exc))
        return

    # --json-events: use structured JSON emitter; otherwise use Rich dashboard
    if json_events:
        ui: TurbineUI | JsonEventUI = JsonEventUI()
    else:
        ui_enabled = not no_ui and sys.stdout.isatty()
        ui = TurbineUI(title=target, enabled=ui_enabled)

    # Steps 2–5: Preprocess → Investigate → Delegate → Commit & Verify
    # Preflight, branch creation, review gate, repair loop, and cleanup are
    # all handled inside Manager.run() — no glue code needed here.
    manager = Manager(
        tree=tree,
        user_request=request,
        project_root=target,
        api_key=api_key,
        test_commands=test_commands or [],
        dry_run=dry_run,
        review=review,
        ui=ui,
        verbose=verbose,
        git=git,
        chat_id=chat_id,
    )

    try:
        with ui:
            await manager.run()
    finally:
        # Phase 22: Always release lock on exit (clean or crash)
        git.release_lock()

    # Final report
    if manager.commit_result:
        log.action(f"Commit: {manager.commit_result.summary()}")

    if manager.repair_tasks:
        log.error(
            f"{len(manager.repair_tasks)} worker(s) need repair after test failures:"
        )
        for task in manager.repair_tasks:
            log.error(f"  [{task.worker_id}] {task.ticket_description}")

    # Phase 22: Git hints — only merge and undo (Turbine never merges directly)
    hints = manager.git_hints()
    if hints["merge"]:
        log.action(f"Git: bring changes to original branch:  {hints['merge']}")
    if hints["undo"]:
        log.action(f"Git: undo Turbine's commit: {hints['undo']}")


def _eval_main() -> None:
    """Entry point for `turbine eval [dir]`."""
    import argparse
    import os
    from pathlib import Path

    from turbine.eval_runner import EvalRunner, print_eval_report

    parser = argparse.ArgumentParser(
        prog="turbine eval",
        description="Run the Turbine eval harness against a directory of task JSON files.",
    )
    parser.add_argument(
        "eval_dir",
        nargs="?",
        default="evals/tasks",
        help="Directory containing task *.json files (default: evals/tasks)",
    )
    parser.add_argument(
        "--model",
        default="mistral-large-latest",
        help="Mistral model to use for all pipeline calls",
    )
    parser.add_argument(
        "--filter",
        metavar="CATEGORY",
        help="Only run tasks whose category matches this value",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        metavar="N",
        help="Max tasks to run in parallel (default: 1)",
    )
    args = parser.parse_args(sys.argv[2:])

    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        print("Error: MISTRAL_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    runner = EvalRunner(
        eval_dir=Path(args.eval_dir),
        api_key=api_key,
        model=args.model,
        concurrency=args.concurrency,
    )
    tasks = runner.load_tasks()
    if args.filter:
        tasks = [t for t in tasks if t.category == args.filter]
    if not tasks:
        print(f"No tasks found in '{args.eval_dir}'.", file=sys.stderr)
        sys.exit(1)

    print(f"Running {len(tasks)} eval task(s) from '{args.eval_dir}' …")
    results = asyncio.run(runner.run(tasks))
    print_eval_report(results)

    if not all(r.passed for r in results):
        sys.exit(1)


def _purge_main() -> None:
    """Entry point for `turbine purge [dir]`."""
    import argparse
    import shutil

    parser = argparse.ArgumentParser(
        prog="turbine purge",
        description=(
            "Remove all turbine/ branches, .turbine.lock files, and local "
            "Turbine state from a project directory."
        ),
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Path to the project to purge (default: current directory)",
    )
    args = parser.parse_args(sys.argv[2:])

    log = TurbineLogger()
    target = Path(args.target).resolve()

    # Remove lock file
    lock = target / ".turbine.lock"
    if lock.exists():
        lock.unlink()
        log.action(f"Purge: removed {lock}")
    else:
        log.thinking("Purge: no .turbine.lock found.")

    # Remove .turbine/ directory (local log directories)
    turbine_dir = target / ".turbine"
    if turbine_dir.exists() and turbine_dir.is_dir():
        shutil.rmtree(turbine_dir)
        log.action(f"Purge: removed {turbine_dir}/")

    # Remove git branches
    git = GitIntegration(str(target))
    if git.is_subdir_of_repo:
        log.thinking(
            "Purge: target is a subdirectory of a larger repository — "
            "skipping branch cleanup to avoid affecting the wrong repo."
        )
    elif git.is_repo:
        try:
            deleted = git.purge_history()
            if deleted:
                log.action(f"Purge: removed {len(deleted)} turbine branch(es):")
                for branch in deleted:
                    log.action(f"  {branch}")
            else:
                log.action("Purge: no turbine branches found.")
        except RuntimeError as exc:
            log.error(f"Purge: {exc}")
    else:
        log.thinking("Purge: target is not a git repo — skipping branch cleanup.")

    log.action("Purge complete.")


def main() -> None:
    # Load API key from config file before anything else (env var takes priority).
    _load_config()

    # Route subcommands before the main argparser sees them.
    if len(sys.argv) > 1 and sys.argv[1] == "config":
        _config_main()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "eval":
        _eval_main()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "purge":
        _purge_main()
        return

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
    parser.add_argument(
        "--no-branch", action="store_true", dest="no_branch",
        help="Skip git branch creation; Turbine commits to the current branch instead",
    )
    chat_group = parser.add_mutually_exclusive_group()
    chat_group.add_argument(
        "--chat-id", metavar="ID", dest="chat_id", default=None,
        help=(
            "Named session ID — Turbine branches as turbine/chat-<ID> and resumes "
            "that branch on subsequent runs (clean tree only)."
        ),
    )
    chat_group.add_argument(
        "--new-chat", action="store_true", dest="new_chat",
        help=(
            "Start a fresh chat session: generate a new session ID and branch "
            "from the project's base branch, fully isolated from prior sessions."
        ),
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
        args.no_branch,
        args.chat_id,
        args.new_chat,
    ))


if __name__ == "__main__":
    main()
