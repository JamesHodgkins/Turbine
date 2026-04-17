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
from turbine.cost_tracker import BudgetExceededError
from turbine.project_config import load_project_config
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
    static_check: str | None = None,
    budget: float | None = None,
    model: str | None = None,
    max_workers: int | None = None,
    interactive: bool = False,
    mode: str | None = None,
    deep_max_iterations: int | None = None,
) -> None:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise EnvironmentError("MISTRAL_API_KEY not set. Add it to your .env file.")

    # Phase 17: load project config file, then apply CLI overrides on top.
    try:
        project_cfg = load_project_config(target)
    except ValueError as exc:
        # Bad TOML — surface the error but don't abort; use empty defaults.
        import warnings
        warnings.warn(str(exc), stacklevel=2)
        from turbine.project_config import ProjectConfig
        project_cfg = ProjectConfig()

    merged = project_cfg.apply_cli_overrides(
        model=model,
        max_workers=max_workers,
        test_commands=test_commands,
        budget=budget,
        static_check=static_check,
        interactive=interactive if interactive else None,
    )
    resolved = merged.resolve()

    # Unpack resolved values for use throughout this function.
    effective_model = resolved.model
    effective_max_workers = resolved.max_workers
    effective_test_commands = resolved.test_commands
    effective_budget = resolved.budget
    effective_static_check = resolved.static_check
    effective_interactive = resolved.interactive

    # Phase 20: resolve pipeline mode override
    from turbine.manager import PipelineMode
    effective_mode_override: PipelineMode | None = None
    if mode is not None:
        try:
            effective_mode_override = PipelineMode(mode.lower())
        except ValueError:
            import warnings
            warnings.warn(f"Unknown --mode value {mode!r}; ignoring.", stacklevel=2)

    effective_deep_max_iterations = deep_max_iterations if deep_max_iterations is not None else 20

    # --json-events: create the UI first so the logger can forward to it immediately
    if json_events:
        ui: TurbineUI | JsonEventUI = JsonEventUI()
    else:
        ui_enabled = not no_ui and sys.stdout.isatty()
        ui = TurbineUI(title=target, enabled=ui_enabled)

    json_ui = ui if json_events else None
    log = TurbineLogger(ui=json_ui)

    # Step 1: Discovery (Phase 17: pass config ignore_patterns to mapper)
    log.thinking(f"Mapping project tree at: {target}")
    mapper = TreeMapper(target, extra_ignore_patterns=resolved.ignore_patterns)
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

    # Steps 2–5: Preprocess → Investigate → Delegate → Commit & Verify
    # Preflight, branch creation, review gate, repair loop, and cleanup are
    # all handled inside Manager.run() — no glue code needed here.
    manager = Manager(
        tree=tree,
        user_request=request,
        project_root=target,
        api_key=api_key,
        model=effective_model,
        max_workers=effective_max_workers,
        test_commands=effective_test_commands,
        dry_run=dry_run,
        review=review,
        ui=ui,
        verbose=verbose,
        git=git,
        chat_id=chat_id,
        json_ui=json_ui,
        static_check=effective_static_check,
        budget=effective_budget,
        interactive=effective_interactive,
        mode_override=effective_mode_override,
    )
    manager._deep_max_iterations = effective_deep_max_iterations

    try:
        with ui:
            await manager.run()
    except BudgetExceededError as exc:
        log.error(str(exc))
        log.action(manager._cost_tracker.report())
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

    from turbine.eval_runner import (
        EvalRunner,
        compare_to_baseline,
        load_baseline,
        print_baseline_report,
        print_eval_report,
        save_baseline,
    )

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
    parser.add_argument(
        "--baseline",
        metavar="FILE",
        help="Compare results against this baseline JSON file; exit 1 on regression",
    )
    parser.add_argument(
        "--save-baseline",
        metavar="FILE",
        dest="save_baseline",
        help="Save current results as a new baseline JSON file",
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

    # Phase 13 gate: compare against stored baseline and/or save a new one.
    exit_code = 0

    if args.baseline:
        baseline_path = Path(args.baseline)
        try:
            baseline = load_baseline(baseline_path)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        regressions = compare_to_baseline(results, baseline)
        print_baseline_report(regressions)
        if regressions:
            exit_code = 1

    if args.save_baseline:
        save_path = Path(args.save_baseline)
        save_baseline(results, save_path)
        print(f"Baseline saved to '{save_path}'.")

    if exit_code == 0 and not all(r.passed for r in results):
        exit_code = 1

    if exit_code:
        sys.exit(exit_code)


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


def _init_main() -> None:
    """Entry point for ``turbine init [dir]``."""
    import argparse

    from turbine.project_config import find_config_file, scaffold_config

    parser = argparse.ArgumentParser(
        prog="turbine init",
        description=(
            "Scaffold a starter turbine.toml in a project directory. "
            "Detects the project type (Python, Node, TypeScript, Rust, Go) "
            "and writes sensible commented-out defaults."
        ),
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Path to the project root (default: current directory)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing turbine.toml",
    )
    args = parser.parse_args(sys.argv[2:])

    from pathlib import Path
    target = Path(args.target).resolve()

    if not target.is_dir():
        print(f"Error: {target} is not a directory.", file=sys.stderr)
        sys.exit(1)

    existing = find_config_file(target)
    if existing and not args.force:
        print(
            f"turbine.toml already exists at {existing}\n"
            "Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        dest = scaffold_config(target, force=args.force)
        print(f"Created {dest}")
    except FileExistsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


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

    if len(sys.argv) > 1 and sys.argv[1] == "init":
        _init_main()
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
    static_group = parser.add_mutually_exclusive_group()
    static_group.add_argument(
        "--static-check", metavar="CMD", dest="static_check", default=None,
        help=(
            "Static-check command to run after commit and before tests "
            "(e.g. 'ruff check .' or 'mypy --no-error-summary .'). "
            "Defaults to auto-detecting from pyproject.toml / tsconfig.json."
        ),
    )
    static_group.add_argument(
        "--no-static-check", action="store_true", dest="no_static_check",
        help="Disable the static-check step entirely.",
    )
    parser.add_argument(
        "--budget", metavar="USD", type=float, default=None, dest="budget",
        help=(
            "Abort the run with a clear message if the estimated LLM spend "
            "reaches this value in USD (e.g. --budget 0.50)."
        ),
    )
    parser.add_argument(
        "--model", metavar="NAME", default=None, dest="model",
        help=(
            "Mistral model name to use (e.g. 'mistral-large-latest'). "
            "Overrides turbine.toml and the built-in default."
        ),
    )
    parser.add_argument(
        "--max-workers", metavar="N", type=int, default=None, dest="max_workers",
        help=(
            "Maximum number of parallel workers. "
            "Overrides turbine.toml and the built-in default of 4."
        ),
    )
    parser.add_argument(
        "--interactive", action="store_true", dest="interactive",
        help=(
            "Enable the clarification gate: if the investigator finds the request "
            "genuinely ambiguous, Turbine pauses and asks a question on stdin before "
            "proceeding.  Without this flag the best-guess plan is used instead."
        ),
    )
    parser.add_argument(
        "--mode", metavar="MODE", default=None, dest="mode",
        choices=["wide", "deep"],
        help=(
            "Override the automatic pipeline mode selection. "
            "'wide' uses parallel workers (default for 2+ tickets); "
            "'deep' uses a sequential tool-calling agent (default for 1 ticket)."
        ),
    )
    parser.add_argument(
        "--deep-max-iterations", metavar="N", type=int, default=None,
        dest="deep_max_iterations",
        help=(
            "Maximum tool-calling iterations for Deep Mode (default: 20). "
            "Exhaustion forces --review so changes can be inspected."
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
        static_check="" if args.no_static_check else args.static_check,
        budget=args.budget,
        model=args.model,
        max_workers=args.max_workers,
        interactive=args.interactive,
        mode=args.mode,
        deep_max_iterations=args.deep_max_iterations,
    ))


if __name__ == "__main__":
    main()
