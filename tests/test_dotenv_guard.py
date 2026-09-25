"""The shared vault guard every working-directory `.env` reader/writer uses.

`exomem.dotenv_guard` is the single place that decides whether a `.env`
candidate may be read or written: vault content is writable by remote
principals through the file tools and arrives through sync, so a `.env`
found there must never become configuration. These tests exercise the
mechanism directly; the per-loader tests (`tests/test_cli_dotenv_vault_guard.py`,
`tests/test_remote_setup_wizard.py`, `tests/test_setup_wizard.py`,
`tests/test_remote_owner_invariants.py`) confirm each consumer actually
routes through it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import dotenv_guard


def _plant(directory: Path, content: str = "EXOMEM_TEST_KEY=from-dotenv\n") -> Path:
    path = directory / ".env"
    path.write_text(content, encoding="utf-8")
    return path


def test_normal_directory_outside_a_vault_returns_the_candidate(tmp_path: Path) -> None:
    """The everyday case: an operator's `.env` next to a non-vault working
    directory is returned untouched."""
    service_root = tmp_path / "service-root"
    service_root.mkdir()
    candidate = _plant(service_root)

    assert dotenv_guard.dotenv_load_guard(candidate) == candidate


def test_refuses_a_dotenv_planted_at_the_vault_root(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A `.env` a remote writer planted at the vault root must not be read or
    written just because a command ran from there."""
    candidate = _plant(vault)

    with caplog.at_level("WARNING", logger=dotenv_guard.log.name):
        result = dotenv_guard.dotenv_load_guard(candidate)

    assert result is None
    refusals = [r.getMessage() for r in caplog.records if "dotenv_refused" in r.getMessage()]
    assert len(refusals) == 1
    assert str(candidate.resolve()) in refusals[0]


def test_refuses_a_dotenv_planted_in_a_vault_subdirectory(vault: Path) -> None:
    """A structural ancestor being a vault is enough; the candidate need not
    sit at the vault root itself."""
    subdirectory = vault / "Knowledge Base"
    candidate = _plant(subdirectory)

    assert dotenv_guard.dotenv_load_guard(candidate) is None


def test_refuses_when_the_holding_directory_is_a_symlink_into_the_vault(
    vault: Path, tmp_path: Path
) -> None:
    """A working directory that is itself a symlink resolving into the vault
    must be refused, not just a literal vault path."""
    linked_root = tmp_path / "symlinked-service-root"
    linked_root.symlink_to(vault)
    candidate = linked_root / ".env"
    _plant(vault)

    assert dotenv_guard.dotenv_load_guard(candidate) is None


def test_refuses_when_the_dotenv_file_itself_is_a_symlink_into_the_vault(
    vault: Path, tmp_path: Path
) -> None:
    """The working directory is outside the vault, but the `.env` file is a
    symlink whose target a remote writer can plant inside the vault."""
    planted = _plant(vault / "Knowledge Base")
    service_root = tmp_path / "service-root"
    service_root.mkdir()
    candidate = service_root / ".env"
    candidate.symlink_to(planted)

    assert dotenv_guard.dotenv_load_guard(candidate) is None


def test_refuses_when_the_dotenv_declares_the_enclosing_vault(vault: Path) -> None:
    """With no vault configured in the process environment, a `.env` sitting
    inside the vault it names is still refused."""
    candidate = _plant(vault, f"EXOMEM_VAULT_PATH={vault}\nEXOMEM_TEST_KEY=from-dotenv\n")

    assert dotenv_guard.dotenv_load_guard(candidate) is None


def test_refuses_a_directory_that_is_a_vault_even_when_a_different_vault_is_configured(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured vault may be elsewhere; a directory that is itself
    structurally a vault is still reachable by remote writers."""
    other = tmp_path / "configured-elsewhere"
    other.mkdir()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(other))
    candidate = _plant(vault)

    assert dotenv_guard.dotenv_load_guard(candidate) is None


def test_refusal_message_names_the_remedy(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    candidate = _plant(vault)
    with caplog.at_level("WARNING", logger=dotenv_guard.log.name):
        dotenv_guard.dotenv_load_guard(candidate)
    refusals = [r.getMessage() for r in caplog.records if "dotenv_refused" in r.getMessage()]
    assert len(refusals) == 1
    assert "move the .env out of the vault, or put the settings in service.env" in refusals[0]


def test_working_directory_dotenv_uses_the_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_root = tmp_path / "service-root"
    service_root.mkdir()
    _plant(service_root)
    monkeypatch.chdir(service_root)

    assert dotenv_guard.working_directory_dotenv() == service_root / ".env"


def test_working_directory_dotenv_refuses_from_inside_a_vault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant(vault)
    monkeypatch.chdir(vault)

    assert dotenv_guard.working_directory_dotenv() is None
