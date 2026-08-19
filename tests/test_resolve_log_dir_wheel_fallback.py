"""TDD tests for issue #552: `resolve_log_dir()`'s final fallback must not
resolve into a wheel install's site-packages/venv tree.

Root cause: `Path(__file__).resolve().parents[2] / "logs"` assumes a
src-layout source checkout (`<repo>/src/exomem/logging_config.py` ->
`parents[2]` == `<repo>`). In a wheel install `__file__` is
`<venv>/Lib/site-packages/exomem/logging_config.py` (Windows) or
`<venv>/lib/pythonX.Y/site-packages/exomem/logging_config.py` (POSIX), so
`parents[2]` lands inside the venv (`<venv>/Lib` / `<venv>/lib/pythonX.Y`) --
exactly the production incident: the live service's logs land at
`exomem-service-ha/.venv/Lib/logs/` instead of a real log location.
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


def test_wheel_install_fallback_lands_in_a_per_platform_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)
    fake_file = _fake_wheel_module_file(tmp_path)
    monkeypatch.setattr(logging_config, "__file__", str(fake_file))

    result = logging_config.resolve_log_dir()

    # Deliberately no blanket `result.name == "logs"`: macOS follows Apple's
    # convention and lands on `~/Library/Logs/Exomem`, which the darwin branch
    # below already asserts in full. The two platforms that do end in `logs`
    # assert their whole path, which is the stronger claim anyway.
    if sys.platform == "win32":
        # Machine-wide, NOT the user profile: a LocalSystem-run service and
        # an operator's own-user `exomem` CLI must land on the same
        # directory, matching `mode.config_path()`'s own rationale for
        # exactly this split. See the win32-branch tests below for the
        # ALLUSERSPROFILE/hardcoded-fallback tiers, exercised via a
        # monkeypatched `sys.platform` so they run on any host OS.
        import os as _os

        program_data = (
            _os.environ.get("PROGRAMDATA")
            or _os.environ.get("ALLUSERSPROFILE")
            or "C:" + r"\ProgramData"
        )
        assert result == Path(program_data) / "exomem" / "logs"
    elif sys.platform == "darwin":
        assert result == Path.home() / "Library" / "Logs" / "Exomem"
    else:
        assert result.name == "logs"
        assert result.parent.name == "exomem"


# --- win32 branch, exercised via a monkeypatched sys.platform so it runs on
# --- every CI host OS, not just when the test happens to execute on Windows.
# --- Windows is the platform that matters for production (the live incident
# --- was a Windows service), so it must not go untested on Linux CI.


def test_win32_fallback_uses_programdata_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_config.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", "C:" + r"\CustomProgramData")
    monkeypatch.delenv("ALLUSERSPROFILE", raising=False)

    result = logging_config._user_log_dir()

    assert result == Path("C:" + r"\CustomProgramData") / "exomem" / "logs"


def test_win32_fallback_uses_alluserprofile_when_programdata_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging_config.sys, "platform", "win32")
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.setenv("ALLUSERSPROFILE", "C:" + r"\Users\All Users")

    result = logging_config._user_log_dir()

    assert result == Path("C:" + r"\Users\All Users") / "exomem" / "logs"


def test_win32_fallback_uses_hardcoded_programdata_when_both_env_vars_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging_config.sys, "platform", "win32")
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.delenv("ALLUSERSPROFILE", raising=False)

    result = logging_config._user_log_dir()

    assert result == Path("C:" + "\\ProgramData") / "exomem" / "logs"


def test_win32_fallback_never_touches_the_user_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect this guards: a LocalSystem-run service's `%LOCALAPPDATA%`/
    `Path.home()` resolves to `%SystemRoot%/System32/config/systemprofile/...`
    -- unreadable without elevation, and no operator-run `exomem doctor` can
    ever find it. The win32 branch must not read either signal at all."""
    monkeypatch.setattr(logging_config.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", "C:" + r"\ProgramData")
    monkeypatch.setenv(
        "LOCALAPPDATA", "C:" + r"\Windows\System32\config\systemprofile\AppData\Local"
    )

    def _forbidden_home() -> Path:
        raise AssertionError("win32 branch must not call Path.home()")

    monkeypatch.setattr(logging_config.Path, "home", staticmethod(_forbidden_home))

    result = logging_config._user_log_dir()

    assert "systemprofile" not in str(result)
    assert result == Path("C:" + r"\ProgramData") / "exomem" / "logs"


def test_win32_fallback_directory_name_matches_mode_config_path_lowercase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matches `mode.config_path()`'s `%PROGRAMDATA%/exomem/config.json`
    convention exactly -- lowercase `exomem`, not `Exomem`."""
    monkeypatch.setattr(logging_config.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", "C:" + r"\ProgramData")

    result = logging_config._user_log_dir()

    assert result.parent.name == "exomem"
    assert result.name == "logs"


# --- darwin/linux branches, also exercised via monkeypatched sys.platform --


def test_darwin_fallback_uses_home_library_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_config.sys, "platform", "darwin")
    fake_home = Path("/Users/fakeuser")
    monkeypatch.setattr(logging_config.Path, "home", staticmethod(lambda: fake_home))

    result = logging_config._user_log_dir()

    assert result == fake_home / "Library" / "Logs" / "Exomem"


def test_linux_fallback_uses_xdg_state_home_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_config.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", "/custom/state")

    result = logging_config._user_log_dir()

    assert result == Path("/custom/state") / "exomem" / "logs"


def test_linux_fallback_uses_home_local_state_when_xdg_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging_config.sys, "platform", "linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    fake_home = Path("/home/fakeuser")
    monkeypatch.setattr(logging_config.Path, "home", staticmethod(lambda: fake_home))

    result = logging_config._user_log_dir()

    assert result == fake_home / ".local" / "state" / "exomem" / "logs"


# --- resolve_log_dir() must never raise, even when Path.home() itself can't
# --- resolve (e.g. a container with no $HOME and no /etc/passwd entry) -----


def _raising_home() -> Path:
    raise RuntimeError("could not determine home directory")


def test_darwin_fallback_survives_an_unresolvable_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_config.sys, "platform", "darwin")
    monkeypatch.setattr(logging_config.Path, "home", staticmethod(_raising_home))

    result = logging_config._user_log_dir()  # must not raise

    assert result.name == "logs"
    assert result.parent.name == "exomem"


def test_linux_fallback_survives_an_unresolvable_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_config.sys, "platform", "linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(logging_config.Path, "home", staticmethod(_raising_home))

    result = logging_config._user_log_dir()  # must not raise

    assert result.name == "logs"
    assert result.parent.name == "exomem"


def test_resolve_log_dir_end_to_end_survives_an_unresolvable_home_on_a_wheel_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full call chain a wheel install with EXOMEM_LOG_DIR unset would
    actually take, on a homeless POSIX host, must not raise out of an
    early-startup logging bootstrap that none of server.py/__main__.py/
    media_worker_child.py guard against."""
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)
    fake_file = _fake_wheel_module_file(tmp_path)
    monkeypatch.setattr(logging_config, "__file__", str(fake_file))
    monkeypatch.setattr(logging_config.sys, "platform", "linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(logging_config.Path, "home", staticmethod(_raising_home))

    result = logging_config.resolve_log_dir()  # must not raise

    assert result.name == "logs"
