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


def test_preload_resource_policy_reads_the_evermatter_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import runtime_resources

    (tmp_path / ".env").write_text("EVERMATTER_CPU_THREADS=3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EXOMEM_CPU_THREADS", raising=False)

    runtime_resources.preload_local_dotenv_policy()

    assert os.environ.get("EXOMEM_CPU_THREADS") == "3"


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


# --------------------------------------------------------------------------- #
# exported values the working-directory .env would replace
# --------------------------------------------------------------------------- #


def _plant_store(directory: Path) -> None:
    (directory / ".env").write_text(
        "EXOMEM_OAUTH_STORAGE_URL=sqlite:////dotenv-store/sessions.db\n", encoding="utf-8"
    )


def test_issue_local_refuses_to_mint_into_a_store_the_dotenv_swaps_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A shell that exports one store, run from a folder whose .env names another,
    must not get a token minted into the other store."""
    from exomem import __main__ as cli
    from exomem import server_auth

    _plant_store(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXOMEM_OAUTH_STORAGE_URL", "sqlite:////exported-store/sessions.db")
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "12345")
    monkeypatch.setenv("EXOMEM_GITHUB_USERNAME", "owner")
    monkeypatch.setattr(
        server_auth,
        "build_local_session_authority",
        lambda: pytest.fail("minted against an ambiguous store"),
    )
    output = tmp_path / "token"

    assert cli.main(["auth", "issue-local", "--client", "home", "--output", str(output)]) == 2

    error = capsys.readouterr().err
    assert "EXOMEM_OAUTH_STORAGE_URL" in error
    assert "dotenv-store" not in error and "exported-store" not in error
    assert not output.exists()
    assert os.environ["EXOMEM_OAUTH_STORAGE_URL"] == "sqlite:////exported-store/sessions.db"


def test_session_controls_refuse_a_store_the_dotenv_swaps_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`auth revoke --all` against the .env's store would revoke the wrong sessions."""
    from exomem import __main__ as cli
    from exomem import server_auth

    _plant_store(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXOMEM_OAUTH_STORAGE_URL", "sqlite:////exported-store/sessions.db")
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://kb.example.com/")
    monkeypatch.setattr(
        server_auth,
        "build_session_authority",
        lambda **_kwargs: pytest.fail("revoked against an ambiguous store"),
    )

    assert cli.main(["auth", "revoke", "--all"]) == 2

    assert "EXOMEM_OAUTH_STORAGE_URL" in capsys.readouterr().err


def test_a_dotenv_evermatter_value_conflicts_with_a_different_exported_exomem_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conflict check must see `EVERMATTER_BASE_URL` in the .env and
    `EXOMEM_BASE_URL` in the shell as one setting, or a credential command acts on
    the wrong deployment."""
    from exomem import __main__ as cli

    (tmp_path / ".env").write_text(
        "EVERMATTER_BASE_URL=https://dotenv.example.com\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://exported.example.com")

    with pytest.raises(cli._DotenvConflict) as conflict:
        cli._load_cwd_dotenv(refuse_conflicts=True)

    assert conflict.value.keys == ["EXOMEM_BASE_URL"]
    assert os.environ["EXOMEM_BASE_URL"] == "https://exported.example.com"


def test_a_dotenv_evermatter_value_replaces_the_exported_value_and_the_warning_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from exomem import __main__ as cli

    (tmp_path / ".env").write_text(
        "EVERMATTER_BASE_URL=https://dotenv.example.com\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://exported.example.com")

    cli._load_cwd_dotenv()

    assert "replaces exported EXOMEM_BASE_URL" in capsys.readouterr().err
    assert os.environ["EXOMEM_BASE_URL"] == "https://dotenv.example.com"


def test_a_dotenv_that_agrees_with_the_environment_is_no_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing every key the shell and the .env share would block a correct setup."""
    from exomem import __main__ as cli
    from exomem import server_auth

    class _Reached(Exception):
        pass

    _plant_store(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXOMEM_OAUTH_STORAGE_URL", "sqlite:////dotenv-store/sessions.db")
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://kb.example.com/")

    def reached(**_kwargs):
        raise _Reached

    monkeypatch.setattr(server_auth, "build_session_authority", reached)

    with pytest.raises(_Reached):
        cli.main(["auth", "revoke", "--all"])
