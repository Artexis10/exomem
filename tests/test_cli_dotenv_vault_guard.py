"""Every CLI `.env` loader refuses a working-directory `.env` inside a vault,
the same way `server_runtime.initialize_runtime` already does.

`exomem.__main__._load_cwd_dotenv` backs both the `auth` and `doctor`
commands; `exomem.runtime_resources.preload_local_dotenv_policy` is the
earlier, native-resource-only read that runs before either. All three now
route through `exomem.dotenv_guard`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_SENTINEL = "EXOMEM_TEST_DOTENV_SENTINEL"


def _plant(directory: Path) -> None:
    (directory / ".env").write_text(
        f"{_SENTINEL}=from-dotenv\nEXOMEM_BASE_URL=https://from-dotenv.invalid\n"
        "EXOMEM_CPU_THREADS=7\n",
        encoding="utf-8",
    )


def _forget_leaked_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (_SENTINEL, "EXOMEM_BASE_URL", "EXOMEM_CPU_THREADS"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


# --------------------------------------------------------------------------- #
# auth (exomem.__main__._build_auth_session_authority)
# --------------------------------------------------------------------------- #


def test_auth_still_loads_a_normal_working_directory_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import __main__ as cli

    service_root = tmp_path / "service-root"
    service_root.mkdir()
    _plant(service_root)
    monkeypatch.chdir(service_root)
    _forget_leaked_env(monkeypatch)

    with pytest.raises(RuntimeError, match="EXOMEM_JWT_SIGNING_KEY"):
        # base_url now resolves from the planted .env, so auth proceeds past
        # its own check and fails on the next missing setting instead.
        cli._build_auth_session_authority()
    assert os.environ.get(_SENTINEL) == "from-dotenv"


def test_auth_refuses_a_dotenv_planted_at_the_vault_root(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import __main__ as cli

    _plant(vault)
    monkeypatch.chdir(vault)
    _forget_leaked_env(monkeypatch)

    with pytest.raises(ValueError, match="EXOMEM_BASE_URL is required"):
        cli._build_auth_session_authority()
    assert _SENTINEL not in os.environ


def test_auth_refuses_a_working_directory_reached_via_a_symlinked_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import __main__ as cli

    _plant(vault)
    linked_root = tmp_path / "symlinked-service-root"
    linked_root.symlink_to(vault)
    monkeypatch.chdir(linked_root)
    _forget_leaked_env(monkeypatch)

    with pytest.raises(ValueError, match="EXOMEM_BASE_URL is required"):
        cli._build_auth_session_authority()
    assert _SENTINEL not in os.environ


def test_auth_refuses_a_symlinked_env_file_into_the_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import __main__ as cli

    planted = vault / "Knowledge Base" / ".env"
    planted.write_text(f"{_SENTINEL}=from-dotenv\n", encoding="utf-8")
    service_root = tmp_path / "service-root"
    service_root.mkdir()
    (service_root / ".env").symlink_to(planted)
    monkeypatch.chdir(service_root)
    _forget_leaked_env(monkeypatch)

    with pytest.raises(ValueError, match="EXOMEM_BASE_URL is required"):
        cli._build_auth_session_authority()
    assert _SENTINEL not in os.environ


# --------------------------------------------------------------------------- #
# doctor (exomem.__main__._doctor_main)
# --------------------------------------------------------------------------- #


class _Stop(Exception):
    pass


def _run_doctor_until_dotenv(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    from exomem import __main__ as cli
    from exomem import doctor as doctor_module

    seen: dict[str, str | None] = {}

    def fake_doctor(**_kwargs):
        seen["sentinel"] = os.environ.get(_SENTINEL)
        raise _Stop

    monkeypatch.setattr(doctor_module, "doctor", fake_doctor)
    with pytest.raises(_Stop):
        cli._doctor_main([])
    return seen


def test_doctor_still_loads_a_normal_working_directory_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_root = tmp_path / "service-root"
    service_root.mkdir()
    _plant(service_root)
    monkeypatch.chdir(service_root)
    _forget_leaked_env(monkeypatch)

    assert _run_doctor_until_dotenv(monkeypatch) == {"sentinel": "from-dotenv"}


def test_doctor_refuses_a_dotenv_planted_at_the_vault_root(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant(vault)
    monkeypatch.chdir(vault)
    _forget_leaked_env(monkeypatch)

    assert _run_doctor_until_dotenv(monkeypatch) == {"sentinel": None}


def test_doctor_refuses_a_working_directory_reached_via_a_symlinked_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant(vault)
    linked_root = tmp_path / "symlinked-service-root"
    linked_root.symlink_to(vault)
    monkeypatch.chdir(linked_root)
    _forget_leaked_env(monkeypatch)

    assert _run_doctor_until_dotenv(monkeypatch) == {"sentinel": None}


def test_doctor_refuses_a_symlinked_env_file_into_the_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = vault / "Knowledge Base" / ".env"
    planted.write_text(f"{_SENTINEL}=from-dotenv\n", encoding="utf-8")
    service_root = tmp_path / "service-root"
    service_root.mkdir()
    (service_root / ".env").symlink_to(planted)
    monkeypatch.chdir(service_root)
    _forget_leaked_env(monkeypatch)

    assert _run_doctor_until_dotenv(monkeypatch) == {"sentinel": None}


# --------------------------------------------------------------------------- #
# runtime_resources.preload_local_dotenv_policy (native-resource preload,
# runs before either subcommand above)
# --------------------------------------------------------------------------- #


def test_preload_resource_policy_still_reads_a_normal_working_directory_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import runtime_resources

    service_root = tmp_path / "service-root"
    service_root.mkdir()
    _plant(service_root)
    monkeypatch.chdir(service_root)
    monkeypatch.delenv("EXOMEM_CPU_THREADS", raising=False)

    runtime_resources.preload_local_dotenv_policy()

    assert os.environ.get("EXOMEM_CPU_THREADS") == "7"


def test_preload_resource_policy_refuses_a_dotenv_planted_at_the_vault_root(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import runtime_resources

    _plant(vault)
    monkeypatch.chdir(vault)
    monkeypatch.delenv("EXOMEM_CPU_THREADS", raising=False)

    runtime_resources.preload_local_dotenv_policy()

    assert "EXOMEM_CPU_THREADS" not in os.environ


def test_preload_resource_policy_refuses_a_working_directory_reached_via_a_symlinked_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import runtime_resources

    _plant(vault)
    linked_root = tmp_path / "symlinked-service-root"
    linked_root.symlink_to(vault)
    monkeypatch.chdir(linked_root)
    monkeypatch.delenv("EXOMEM_CPU_THREADS", raising=False)

    runtime_resources.preload_local_dotenv_policy()

    assert "EXOMEM_CPU_THREADS" not in os.environ


def test_preload_resource_policy_refuses_a_symlinked_env_file_into_the_vault(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import runtime_resources

    planted = vault / "Knowledge Base" / ".env"
    planted.write_text("EXOMEM_CPU_THREADS=7\n", encoding="utf-8")
    service_root = tmp_path / "service-root"
    service_root.mkdir()
    (service_root / ".env").symlink_to(planted)
    monkeypatch.chdir(service_root)
    monkeypatch.delenv("EXOMEM_CPU_THREADS", raising=False)

    runtime_resources.preload_local_dotenv_policy()

    assert "EXOMEM_CPU_THREADS" not in os.environ
