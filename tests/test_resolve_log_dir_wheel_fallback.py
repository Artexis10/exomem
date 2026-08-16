"""TDD tests for issue #552: `resolve_log_dir()`'s final fallback must not
resolve into a wheel install's site-packages/venv tree.

Root cause: `Path(__file__).resolve().parents[2] / "logs"` assumes a
src-layout source checkout (`<repo>/src/exomem/logging_config.py` ->
`parents[2]` == `<repo>`). In a wheel install `__file__` is
`<venv>/Lib/site-packages/exomem/logging_config.py` (Windows) or
`<venv>/lib/pythonX.Y/site-packages/exomem/logging_config.py` (POSIX), so
`parents[2]` lands inside the venv (`<venv>/Lib` / `<venv>/lib/pythonX.Y`) --
exactly the production incident: the live service's logs land at
`exomem-service-ha\\.venv\\Lib\\logs\\` instead of a real log location.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from exomem import logging_config


def _fake_wheel_module_file(tmp_path: Path) -> Path:
    """A `.venv/Lib/site-packages/exomem/logging_config.py`-shaped path --
    a real wheel install layout, with no `pyproject.toml` anywhere near it."""
    site_packages = tmp_path / "exomem-service-ha" / ".venv" / "Lib" / "site-packages"
    package_dir = site_packages / "exomem"
    package_dir.mkdir(parents=True)
    return package_dir / "logging_config.py"


def _fake_checkout_module_file(tmp_path: Path, *, name: str = "exomem") -> Path:
    """A `<repo>/src/exomem/logging_config.py`-shaped path with a real
    `pyproject.toml` at the repo root -- a genuine source checkout."""
    repo_root = tmp_path / name
    package_dir = repo_root / "src" / "exomem"
    package_dir.mkdir(parents=True)
    (repo_root / "pyproject.toml").write_text(
        '[project]\nname = "exomem"\n', encoding="utf-8"
    )
    return package_dir / "logging_config.py"


def _fake_src_named_dir_without_pyproject(tmp_path: Path) -> Path:
    """A directory that happens to be named `src` two hops up from the
    package, but is NOT a real checkout (no `pyproject.toml` at the root).
    Must not false-positive as a source checkout."""
    package_dir = tmp_path / "somewhere" / "src" / "exomem"
    package_dir.mkdir(parents=True)
    return package_dir / "logging_config.py"


# --- the bug: a wheel install must not fall back into its own venv tree ----


def test_wheel_install_fallback_does_not_land_in_site_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)
    fake_file = _fake_wheel_module_file(tmp_path)
    monkeypatch.setattr(logging_config, "__file__", str(fake_file))

    result = logging_config.resolve_log_dir()

    # Today's code returns fake_file.parents[2] / "logs", i.e. `.venv/Lib/logs`
    # -- inside the venv tree. The fix must stay outside it entirely.
    venv_lib = fake_file.resolve().parents[2]
    assert venv_lib not in result.parents and result != venv_lib
    assert "site-packages" not in result.parts


def test_wheel_install_fallback_avoids_a_src_named_dir_with_no_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory literally named `src` two hops up is not, by itself,
    evidence of a real checkout -- the `pyproject.toml` marker must also be
    required, so this can't false-positive inside an unrelated tree."""
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)
    fake_file = _fake_src_named_dir_without_pyproject(tmp_path)
    monkeypatch.setattr(logging_config, "__file__", str(fake_file))

    result = logging_config.resolve_log_dir()

    bogus_root = fake_file.resolve().parents[2]
    assert result != bogus_root / "logs"


# --- contract: a genuine source checkout keeps its current default ---------


def test_source_checkout_fallback_still_resolves_repo_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)
    fake_file = _fake_checkout_module_file(tmp_path)
    monkeypatch.setattr(logging_config, "__file__", str(fake_file))

    result = logging_config.resolve_log_dir()

    assert result == fake_file.resolve().parents[2] / "logs"


# --- contract: EXOMEM_LOG_DIR and an explicit default still win first ------


def test_env_override_still_wins_over_a_simulated_wheel_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom-logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(override))
    fake_file = _fake_wheel_module_file(tmp_path)
    monkeypatch.setattr(logging_config, "__file__", str(fake_file))

    assert logging_config.resolve_log_dir() == override


def test_explicit_default_still_wins_over_a_simulated_wheel_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)
    fake_file = _fake_wheel_module_file(tmp_path)
    monkeypatch.setattr(logging_config, "__file__", str(fake_file))
    explicit_default = tmp_path / "explicit-default-logs"

    assert logging_config.resolve_log_dir(default=explicit_default) == explicit_default


# --- the fallback lands somewhere sane and platform-appropriate ------------


def test_wheel_install_fallback_lands_in_a_per_user_platform_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)
    fake_file = _fake_wheel_module_file(tmp_path)
    monkeypatch.setattr(logging_config, "__file__", str(fake_file))

    result = logging_config.resolve_log_dir()

    assert result.name == "logs"
    if sys.platform == "win32":
        local_appdata = Path(
            __import__("os").environ.get("LOCALAPPDATA")
            or (Path.home() / "AppData" / "Local")
        )
        assert result == local_appdata / "Exomem" / "logs"
    elif sys.platform == "darwin":
        assert result == Path.home() / "Library" / "Logs" / "Exomem"
    else:
        assert result.parent.name == "exomem"
