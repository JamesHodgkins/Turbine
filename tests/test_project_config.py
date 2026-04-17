"""Tests for turbine.project_config (Phase 17)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from turbine.project_config import (
    ProjectConfig,
    _detect_project_type,
    _starter_toml,
    find_config_file,
    load_project_config,
    scaffold_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_toml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "turbine.toml"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# find_config_file
# ---------------------------------------------------------------------------

class TestFindConfigFile:
    def test_finds_turbine_toml(self, tmp_path):
        (tmp_path / "turbine.toml").write_text("", encoding="utf-8")
        assert find_config_file(tmp_path) == tmp_path / "turbine.toml"

    def test_finds_dot_turbine_config_toml(self, tmp_path):
        d = tmp_path / ".turbine"
        d.mkdir()
        (d / "config.toml").write_text("", encoding="utf-8")
        assert find_config_file(tmp_path) == d / "config.toml"

    def test_prefers_turbine_toml_over_dot_turbine(self, tmp_path):
        (tmp_path / "turbine.toml").write_text("", encoding="utf-8")
        d = tmp_path / ".turbine"
        d.mkdir()
        (d / "config.toml").write_text("", encoding="utf-8")
        assert find_config_file(tmp_path) == tmp_path / "turbine.toml"

    def test_returns_none_when_no_file(self, tmp_path):
        assert find_config_file(tmp_path) is None


# ---------------------------------------------------------------------------
# load_project_config — happy paths
# ---------------------------------------------------------------------------

class TestLoadProjectConfig:
    def test_empty_file_returns_defaults(self, tmp_path):
        write_toml(tmp_path, "")
        cfg = load_project_config(tmp_path)
        assert cfg.model is None
        assert cfg.max_workers is None
        assert cfg.test_commands == []
        assert cfg.ignore_patterns == []
        assert cfg.budget is None
        assert cfg.static_check is None

    def test_no_file_returns_defaults(self, tmp_path):
        cfg = load_project_config(tmp_path)
        assert cfg.model is None
        assert cfg.max_workers is None

    def test_model(self, tmp_path):
        write_toml(tmp_path, 'model = "mistral-small-latest"\n')
        cfg = load_project_config(tmp_path)
        assert cfg.model == "mistral-small-latest"

    def test_max_workers(self, tmp_path):
        write_toml(tmp_path, "max_workers = 8\n")
        cfg = load_project_config(tmp_path)
        assert cfg.max_workers == 8

    def test_test_commands(self, tmp_path):
        write_toml(tmp_path, 'test_commands = ["pytest tests/", "mypy ."]\n')
        cfg = load_project_config(tmp_path)
        assert cfg.test_commands == ["pytest tests/", "mypy ."]

    def test_ignore_patterns(self, tmp_path):
        write_toml(tmp_path, 'ignore_patterns = ["*.log", "dist/"]\n')
        cfg = load_project_config(tmp_path)
        assert cfg.ignore_patterns == ["*.log", "dist/"]

    def test_budget(self, tmp_path):
        write_toml(tmp_path, "budget = 0.75\n")
        cfg = load_project_config(tmp_path)
        assert cfg.budget == pytest.approx(0.75)

    def test_budget_integer(self, tmp_path):
        write_toml(tmp_path, "budget = 2\n")
        cfg = load_project_config(tmp_path)
        assert cfg.budget == pytest.approx(2.0)

    def test_static_check_command(self, tmp_path):
        write_toml(tmp_path, 'static_check = "ruff check ."\n')
        cfg = load_project_config(tmp_path)
        assert cfg.static_check == "ruff check ."

    def test_static_check_empty_disables(self, tmp_path):
        write_toml(tmp_path, 'static_check = ""\n')
        cfg = load_project_config(tmp_path)
        assert cfg.static_check == ""

    def test_source_path_set(self, tmp_path):
        write_toml(tmp_path, "")
        cfg = load_project_config(tmp_path)
        assert cfg._source == tmp_path / "turbine.toml"

    def test_source_path_none_when_no_file(self, tmp_path):
        cfg = load_project_config(tmp_path)
        assert cfg._source is None

    def test_dot_turbine_subdir(self, tmp_path):
        d = tmp_path / ".turbine"
        d.mkdir()
        (d / "config.toml").write_text("max_workers = 3\n", encoding="utf-8")
        cfg = load_project_config(tmp_path)
        assert cfg.max_workers == 3


# ---------------------------------------------------------------------------
# load_project_config — validation errors
# ---------------------------------------------------------------------------

class TestLoadProjectConfigValidation:
    def test_model_must_be_string(self, tmp_path):
        write_toml(tmp_path, "model = 42\n")
        with pytest.raises(ValueError, match="model"):
            load_project_config(tmp_path)

    def test_max_workers_must_be_int(self, tmp_path):
        write_toml(tmp_path, 'max_workers = "four"\n')
        with pytest.raises(ValueError, match="max_workers"):
            load_project_config(tmp_path)

    def test_max_workers_must_be_positive(self, tmp_path):
        write_toml(tmp_path, "max_workers = 0\n")
        with pytest.raises(ValueError, match="max_workers"):
            load_project_config(tmp_path)

    def test_test_commands_must_be_list_of_strings(self, tmp_path):
        write_toml(tmp_path, "test_commands = 123\n")
        with pytest.raises(ValueError, match="test_commands"):
            load_project_config(tmp_path)

    def test_ignore_patterns_must_be_list_of_strings(self, tmp_path):
        write_toml(tmp_path, "ignore_patterns = [1, 2]\n")
        with pytest.raises(ValueError, match="ignore_patterns"):
            load_project_config(tmp_path)

    def test_budget_must_be_positive(self, tmp_path):
        write_toml(tmp_path, "budget = -1.0\n")
        with pytest.raises(ValueError, match="budget"):
            load_project_config(tmp_path)

    def test_budget_zero_rejected(self, tmp_path):
        write_toml(tmp_path, "budget = 0.0\n")
        with pytest.raises(ValueError, match="budget"):
            load_project_config(tmp_path)

    def test_static_check_must_be_string(self, tmp_path):
        write_toml(tmp_path, "static_check = true\n")
        with pytest.raises(ValueError, match="static_check"):
            load_project_config(tmp_path)

    def test_invalid_toml_raises_value_error(self, tmp_path):
        (tmp_path / "turbine.toml").write_text("not valid [[toml\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_project_config(tmp_path)


# ---------------------------------------------------------------------------
# ProjectConfig.apply_cli_overrides
# ---------------------------------------------------------------------------

class TestApplyCliOverrides:
    def _base(self) -> ProjectConfig:
        return ProjectConfig(
            model="mistral-large-latest",
            max_workers=4,
            test_commands=["pytest tests/"],
            budget=1.0,
            static_check="ruff check .",
        )

    def test_cli_model_wins(self):
        cfg = self._base().apply_cli_overrides(model="mistral-small-latest")
        assert cfg.model == "mistral-small-latest"

    def test_none_cli_model_keeps_config(self):
        cfg = self._base().apply_cli_overrides(model=None)
        assert cfg.model == "mistral-large-latest"

    def test_cli_max_workers_wins(self):
        cfg = self._base().apply_cli_overrides(max_workers=2)
        assert cfg.max_workers == 2

    def test_none_cli_max_workers_keeps_config(self):
        cfg = self._base().apply_cli_overrides(max_workers=None)
        assert cfg.max_workers == 4

    def test_cli_test_commands_replace(self):
        cfg = self._base().apply_cli_overrides(test_commands=["cargo test"])
        assert cfg.test_commands == ["cargo test"]

    def test_none_cli_test_commands_keeps_config(self):
        cfg = self._base().apply_cli_overrides(test_commands=None)
        assert cfg.test_commands == ["pytest tests/"]

    def test_cli_budget_wins(self):
        cfg = self._base().apply_cli_overrides(budget=0.5)
        assert cfg.budget == pytest.approx(0.5)

    def test_cli_static_check_wins(self):
        cfg = self._base().apply_cli_overrides(static_check="mypy .")
        assert cfg.static_check == "mypy ."

    def test_cli_static_check_empty_string_wins(self):
        # "" means "disable" — must propagate even though it is falsy
        cfg = self._base().apply_cli_overrides(static_check="")
        assert cfg.static_check == ""

    def test_ignore_patterns_preserved(self):
        base = ProjectConfig(ignore_patterns=["*.log"])
        cfg = base.apply_cli_overrides(model="x")
        assert cfg.ignore_patterns == ["*.log"]


# ---------------------------------------------------------------------------
# ProjectConfig.resolve (built-in defaults)
# ---------------------------------------------------------------------------

class TestResolve:
    def test_all_nones_use_builtin_defaults(self):
        r = ProjectConfig().resolve()
        assert r.model == "mistral-large-latest"
        assert r.max_workers == 4
        assert r.test_commands == []
        assert r.ignore_patterns == []
        assert r.budget is None
        assert r.static_check is None

    def test_set_values_are_preserved(self):
        cfg = ProjectConfig(model="mistral-small-latest", max_workers=2, budget=0.25)
        r = cfg.resolve()
        assert r.model == "mistral-small-latest"
        assert r.max_workers == 2
        assert r.budget == pytest.approx(0.25)

    def test_custom_builtin_defaults(self):
        r = ProjectConfig().resolve(default_model="custom-model", default_max_workers=8)
        assert r.model == "custom-model"
        assert r.max_workers == 8


# ---------------------------------------------------------------------------
# _detect_project_type
# ---------------------------------------------------------------------------

class TestDetectProjectType:
    def test_python_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        assert _detect_project_type(tmp_path) == "python"

    def test_python_setup_py(self, tmp_path):
        (tmp_path / "setup.py").write_text("", encoding="utf-8")
        assert _detect_project_type(tmp_path) == "python"

    def test_typescript(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
        assert _detect_project_type(tmp_path) == "typescript"

    def test_node_without_tsconfig(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert _detect_project_type(tmp_path) == "node"

    def test_rust(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("", encoding="utf-8")
        assert _detect_project_type(tmp_path) == "rust"

    def test_go(self, tmp_path):
        (tmp_path / "go.mod").write_text("", encoding="utf-8")
        assert _detect_project_type(tmp_path) == "go"

    def test_generic_fallback(self, tmp_path):
        assert _detect_project_type(tmp_path) == "generic"


# ---------------------------------------------------------------------------
# _starter_toml
# ---------------------------------------------------------------------------

class TestStarterToml:
    def test_contains_model_comment(self, tmp_path):
        text = _starter_toml("python", tmp_path)
        assert "model" in text

    def test_python_includes_pytest(self, tmp_path):
        text = _starter_toml("python", tmp_path)
        assert "pytest" in text

    def test_typescript_includes_npm_test(self, tmp_path):
        text = _starter_toml("typescript", tmp_path)
        assert "npm test" in text

    def test_rust_includes_cargo_test(self, tmp_path):
        text = _starter_toml("rust", tmp_path)
        assert "cargo test" in text

    def test_go_includes_go_test(self, tmp_path):
        text = _starter_toml("go", tmp_path)
        assert "go test" in text

    def test_all_lines_commented_or_blank(self, tmp_path):
        """The starter file must not set any real keys — all content is comments."""
        text = _starter_toml("python", tmp_path)
        for line in text.splitlines():
            stripped = line.strip()
            assert stripped == "" or stripped.startswith("#"), (
                f"Unexpected non-comment line: {line!r}"
            )


# ---------------------------------------------------------------------------
# scaffold_config
# ---------------------------------------------------------------------------

class TestScaffoldConfig:
    def test_creates_turbine_toml(self, tmp_path):
        path = scaffold_config(tmp_path)
        assert path == tmp_path / "turbine.toml"
        assert path.is_file()

    def test_content_is_valid_comments(self, tmp_path):
        path = scaffold_config(tmp_path)
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            assert stripped == "" or stripped.startswith("#"), (
                f"Unexpected non-comment line: {line!r}"
            )

    def test_raises_if_exists_without_force(self, tmp_path):
        (tmp_path / "turbine.toml").write_text("# existing\n", encoding="utf-8")
        with pytest.raises(FileExistsError):
            scaffold_config(tmp_path, force=False)

    def test_force_overwrites(self, tmp_path):
        (tmp_path / "turbine.toml").write_text("# old\n", encoding="utf-8")
        scaffold_config(tmp_path, force=True)
        text = (tmp_path / "turbine.toml").read_text(encoding="utf-8")
        assert "# old" not in text

    def test_python_project_mentioned(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        path = scaffold_config(tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "pytest" in text


# ---------------------------------------------------------------------------
# TreeMapper integration: ignore_patterns from config are applied
# ---------------------------------------------------------------------------

class TestTreeMapperIgnorePatterns:
    def test_config_ignore_patterns_exclude_files(self, tmp_path):
        from turbine.tree_mapper import TreeMapper

        # Create two files — one that should be ignored
        (tmp_path / "main.py").write_text("# main\n", encoding="utf-8")
        (tmp_path / "debug.log").write_text("log output\n", encoding="utf-8")

        mapper = TreeMapper(tmp_path, extra_ignore_patterns=["*.log"])
        tree = mapper.map()
        names = [f.relative for f in tree.files]
        assert "main.py" in names
        assert "debug.log" not in names

    def test_no_extra_patterns_includes_all_files(self, tmp_path):
        from turbine.tree_mapper import TreeMapper

        (tmp_path / "main.py").write_text("# main\n", encoding="utf-8")
        (tmp_path / "output.log").write_text("log\n", encoding="utf-8")

        mapper = TreeMapper(tmp_path)
        tree = mapper.map()
        names = [f.relative for f in tree.files]
        assert "output.log" in names
