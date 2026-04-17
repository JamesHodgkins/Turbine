"""Project-level configuration file support for Turbine.

Turbine looks for a ``turbine.toml`` file (or ``.turbine/config.toml``) in
the project root.  Values there act as project-specific defaults that sit
*below* CLI flags but *above* the built-in code defaults.

Supported keys
--------------
model           str      Mistral model name  (default: "mistral-large-latest")
max_workers     int      Max parallel workers (default: 4)
test_commands   [str]    Shell commands to run after commit
ignore_patterns [str]    Extra patterns appended to .turbineignore logic
budget          float    USD spend cap for the run
static_check    str      Static-check command; empty string "" disables it
interactive     bool     Enable the clarification gate (default: false)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Dataclass for a loaded config
# ---------------------------------------------------------------------------

@dataclass
class ProjectConfig:
    """Holds all values parsed from a ``turbine.toml`` (or defaults)."""

    model: str | None = None
    max_workers: int | None = None
    test_commands: list[str] = field(default_factory=list)
    ignore_patterns: list[str] = field(default_factory=list)
    budget: float | None = None
    # None  → auto-detect; ""  → disabled; "<cmd>" → use this command
    static_check: str | None = None
    # Phase 19: None means "not set in config"; True/False are explicit values
    interactive: bool | None = None

    # Path to the loaded config file (None if no file was found)
    _source: Path | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------
    # Merge helpers
    # ------------------------------------------------------------------

    def apply_cli_overrides(
        self,
        *,
        model: str | None = None,
        max_workers: int | None = None,
        test_commands: list[str] | None = None,
        budget: float | None = None,
        static_check: str | None = None,
        interactive: bool | None = None,
    ) -> "ProjectConfig":
        """Return a *new* ProjectConfig where explicit CLI values win.

        A ``None`` argument means "the CLI did not provide this flag", so the
        config-file value (or built-in default) remains in effect.

        Lists are *replaced*, not merged: if ``--test`` is supplied on the
        CLI we use only the CLI's list.
        """
        return ProjectConfig(
            model=model if model is not None else self.model,
            max_workers=max_workers if max_workers is not None else self.max_workers,
            test_commands=test_commands if test_commands is not None else self.test_commands,
            ignore_patterns=self.ignore_patterns,   # CLI has no override for this
            budget=budget if budget is not None else self.budget,
            static_check=static_check if static_check is not None else self.static_check,
            interactive=interactive if interactive is not None else self.interactive,
            _source=self._source,
        )

    def resolve(
        self,
        *,
        default_model: str = "mistral-large-latest",
        default_max_workers: int = 4,
    ) -> "_ResolvedConfig":
        """Return a ``_ResolvedConfig`` with all None fields replaced by built-in defaults."""
        return _ResolvedConfig(
            model=self.model if self.model is not None else default_model,
            max_workers=self.max_workers if self.max_workers is not None else default_max_workers,
            test_commands=list(self.test_commands),
            ignore_patterns=list(self.ignore_patterns),
            budget=self.budget,
            static_check=self.static_check,
            interactive=self.interactive if self.interactive is not None else False,
        )


@dataclass
class _ResolvedConfig:
    """All fields are concrete — no ``None`` for model/max_workers."""
    model: str
    max_workers: int
    test_commands: list[str]
    ignore_patterns: list[str]
    budget: float | None
    static_check: str | None
    interactive: bool = False


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

_CONFIG_NAMES = ("turbine.toml", ".turbine/config.toml")


def find_config_file(project_root: str | Path) -> Path | None:
    """Return the first config file found under *project_root*, or ``None``."""
    root = Path(project_root)
    for name in _CONFIG_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_toml(text: str) -> dict[str, Any]:
    """Parse TOML text using the stdlib (3.11+) or the ``tomli`` fallback."""
    if sys.version_info >= (3, 11):
        import tomllib  # stdlib
        return tomllib.loads(text)
    try:
        import tomllib  # type: ignore[no-redef]
        return tomllib.loads(text)
    except ImportError:
        pass
    try:
        import tomli  # type: ignore[import]
        return tomli.loads(text)
    except ImportError:
        raise ImportError(
            "Python < 3.11 requires the 'tomli' package to parse TOML files. "
            "Install it with: pip install tomli"
        )


def load_project_config(project_root: str | Path) -> ProjectConfig:
    """Load and validate the first ``turbine.toml`` found under *project_root*.

    Returns a ``ProjectConfig`` with only the keys explicitly set in the file
    populated (the rest are ``None`` / empty lists so that callers can detect
    "not configured" vs. a deliberate value).

    Returns an empty ``ProjectConfig`` (all defaults) if no file is found.
    """
    path = find_config_file(project_root)
    if path is None:
        return ProjectConfig()

    try:
        raw = _parse_toml(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Could not parse {path}: {exc}") from exc

    cfg = ProjectConfig(_source=path)

    # model
    if "model" in raw:
        v = raw["model"]
        if not isinstance(v, str):
            raise ValueError(f"{path}: 'model' must be a string")
        cfg.model = v

    # max_workers
    if "max_workers" in raw:
        v = raw["max_workers"]
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(f"{path}: 'max_workers' must be an integer")
        if v < 1:
            raise ValueError(f"{path}: 'max_workers' must be >= 1")
        cfg.max_workers = v

    # test_commands
    if "test_commands" in raw:
        v = raw["test_commands"]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"{path}: 'test_commands' must be a list of strings")
        cfg.test_commands = list(v)

    # ignore_patterns
    if "ignore_patterns" in raw:
        v = raw["ignore_patterns"]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"{path}: 'ignore_patterns' must be a list of strings")
        cfg.ignore_patterns = list(v)

    # budget
    if "budget" in raw:
        v = raw["budget"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{path}: 'budget' must be a number")
        if float(v) <= 0:
            raise ValueError(f"{path}: 'budget' must be > 0")
        cfg.budget = float(v)

    # static_check
    if "static_check" in raw:
        v = raw["static_check"]
        if not isinstance(v, str):
            raise ValueError(f"{path}: 'static_check' must be a string")
        cfg.static_check = v

    # interactive (Phase 19)
    if "interactive" in raw:
        v = raw["interactive"]
        if not isinstance(v, bool):
            raise ValueError(f"{path}: 'interactive' must be a boolean (true or false)")
        cfg.interactive = v

    return cfg


# ---------------------------------------------------------------------------
# turbine init helpers
# ---------------------------------------------------------------------------

def _detect_project_type(project_root: Path) -> str:
    """Return a broad project-type label by inspecting *project_root*."""
    if (project_root / "pyproject.toml").exists() or (project_root / "setup.py").exists():
        return "python"
    if (project_root / "package.json").exists():
        if (project_root / "tsconfig.json").exists():
            return "typescript"
        return "node"
    if (project_root / "Cargo.toml").exists():
        return "rust"
    if (project_root / "go.mod").exists():
        return "go"
    return "generic"


def _starter_toml(project_type: str, project_root: Path) -> str:
    """Return a starter ``turbine.toml`` body for *project_type*."""
    type_snippets: dict[str, str] = {
        "python": (
            '# test_commands = ["pytest tests/ -x"]\n'
            '# static_check  = "ruff check ."\n'
        ),
        "typescript": (
            '# test_commands = ["npm test"]\n'
            '# static_check  = "npx tsc --noEmit"\n'
        ),
        "node": (
            '# test_commands = ["npm test"]\n'
        ),
        "rust": (
            '# test_commands = ["cargo test"]\n'
            '# static_check  = "cargo clippy -- -D warnings"\n'
        ),
        "go": (
            '# test_commands = ["go test ./..."]\n'
            '# static_check  = "go vet ./..."\n'
        ),
        "generic": (
            '# test_commands = ["make test"]\n'
        ),
    }
    snippet = type_snippets.get(project_type, type_snippets["generic"])

    return (
        "# turbine.toml — project-level defaults for Turbine\n"
        "# CLI flags override these values; these override built-in defaults.\n"
        "# Uncomment and edit any line you want to customise.\n"
        "\n"
        '# model       = "mistral-large-latest"   # Mistral model name\n'
        "# max_workers = 4                         # max parallel workers\n"
        "# budget      = 1.00                      # USD spend cap per run\n"
        "# static_check = \"\"                      # \"\" to disable; or e.g. \"ruff check .\"\n"
        "# interactive  = true                     # pause for clarification on ambiguous requests\n"
        "\n"
        + snippet
        + "\n"
        "# ignore_patterns = [\n"
        "#   \"*.log\",\n"
        "#   \"dist/\",\n"
        "# ]\n"
    )


def scaffold_config(project_root: str | Path, *, force: bool = False) -> Path:
    """Write a starter ``turbine.toml`` in *project_root*.

    Parameters
    ----------
    project_root:
        Directory in which to create the file.
    force:
        If ``True``, overwrite an existing file.  Otherwise raise
        ``FileExistsError`` if one is already present.

    Returns the path of the written file.
    """
    root = Path(project_root)
    dest = root / "turbine.toml"
    if dest.exists() and not force:
        raise FileExistsError(
            f"{dest} already exists. Use --force to overwrite."
        )
    project_type = _detect_project_type(root)
    dest.write_text(_starter_toml(project_type, root), encoding="utf-8")
    return dest
