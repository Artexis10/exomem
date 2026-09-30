from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from exomem import cell_init, state_migration
from exomem.__main__ import _cell_init_main
from exomem.governance import store as governance_store
from exomem.kbdir import kb_dirname
from exomem.vault import _is_vault


def test_cell_init_on_a_fresh_volume_creates_and_migrates(tmp_path: Path) -> None:
    vault = tmp_path / "empty-volume" / "vault"
    assert not vault.exists()

    result = cell_init.run_cell_init(vault)

    assert result.vault_created is True
    assert _is_vault(vault)
    # The desktop's own first-run path: no governance sidecar, no custody.
    assert not governance_store.sidecar_path(vault).exists()


@pytest.mark.skipif(os.name == "nt", reason="setgid is a POSIX-only directory semantic")
def test_cell_init_on_a_fresh_volume_under_a_setgid_root(tmp_path: Path) -> None:
    """A Kubernetes fsGroup volume leaves its root setgid (`2770`, D2)."""

    root = tmp_path / "setgid-root"
    root.mkdir()
    os.chmod(root, 0o2770)
    assert os.stat(root).st_mode & stat.S_ISGID

    vault = root / "vault"
    result = cell_init.run_cell_init(vault)

    assert result.vault_created is True
    assert _is_vault(vault)


def test_cell_init_second_run_is_a_no_op(tmp_path: Path) -> None:
    vault = tmp_path / "vault"

    first = cell_init.run_cell_init(vault)
    assert first.vault_created is True

    second = cell_init.run_cell_init(vault)
    assert second.vault_created is False
    assert _is_vault(vault)


def test_cell_init_resumes_cleanly_after_an_interruption_right_after_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that crashes right after `init` (before state migration ever
    starts) leaves a real vault on disk; the next run must not re-run
    `init` (which would refuse: `Knowledge Base` already exists) and must
    still complete the state migration.
    """

    vault = tmp_path / "vault"
    real_migrate = state_migration.migrate_vault_state_offline

    def crash_before_migrate(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated interruption right after init")

    monkeypatch.setattr(
        state_migration, "migrate_vault_state_offline", crash_before_migrate
    )

    with pytest.raises(RuntimeError, match="simulated interruption right after init"):
        cell_init.run_cell_init(vault)

    assert _is_vault(vault)

    monkeypatch.setattr(state_migration, "migrate_vault_state_offline", real_migrate)

    result = cell_init.run_cell_init(vault)

    assert result.vault_created is False
    assert _is_vault(vault)


def test_cell_init_sets_no_custody_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import authorization_custody

    names = (
        authorization_custody.KEYRING_FILE_ENV,
        authorization_custody.CONTROL_FILE_ENV,
        authorization_custody.MEMBERSHIP_FILE_ENV,
        authorization_custody.REPLICA_ID_ENV,
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    cell_init.run_cell_init(tmp_path / "vault")

    for name in names:
        assert name not in os.environ


def test_cell_init_runs_no_governance_schema_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import schema_migration

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("cell-init must not touch governance schema migration")

    monkeypatch.setattr(schema_migration, "prepare_forward_migration", forbidden)
    monkeypatch.setattr(schema_migration, "stage_forward_migration", forbidden)
    monkeypatch.setattr(schema_migration, "commit_forward_migration", forbidden)

    result = cell_init.run_cell_init(tmp_path / "vault")

    assert result.vault_created is True


# --------------------------------------------------------- atomic vault init


@pytest.mark.parametrize("crash_after", [0, 1, 3, 10, 30])
def test_cell_init_atomic_vault_init_survives_a_crash_during_scaffold_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_after: int
) -> None:
    """A crash at any point in the scaffold copy must never wedge the cell
    (`FileExistsError` forever, the pre-fix wedged cases) or report success
    over a truncated vault (the pre-fix partial-vault cases) -- design D3.2.
    """
    vault = tmp_path / "vault"
    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def flaky_copy2(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] > crash_after:
            raise RuntimeError("simulated crash during scaffold copy")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", flaky_copy2)
    with pytest.raises(cell_init.CellInitError, match="CELL_INIT_VAULT_FAILED"):
        cell_init.run_cell_init(vault)
    monkeypatch.setattr(shutil, "copy2", real_copy2)

    # Neither wedged (FileExistsError) nor a partial vault: a clean re-run
    # must produce a complete one.
    result = cell_init.run_cell_init(vault)
    assert result.vault_created is True
    assert _is_vault(vault)
    kb = vault / kb_dirname()
    assert (kb / "index.md").is_file()
    assert (kb / "log.md").is_file()


def test_cell_init_cleans_up_its_own_staging_directory_on_a_caught_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash the process itself catches (an ordinary Python exception, as
    opposed to an OOM kill or a node failure) is cleaned up immediately by
    the same run, rather than left for the next run's stale-sibling sweep."""
    vault = tmp_path / "vault"
    real_copy2 = shutil.copy2

    def crash_first(src, dst, *args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(shutil, "copy2", crash_first)
    with pytest.raises(cell_init.CellInitError):
        cell_init.run_cell_init(vault)
    monkeypatch.setattr(shutil, "copy2", real_copy2)

    assert not [p for p in vault.parent.iterdir() if p.name.startswith(".vault-init-")]


def test_cell_init_removes_a_stale_staging_directory_left_by_an_earlier_run(
    tmp_path: Path,
) -> None:
    """The crash this guards against is an unclean one (an OOM kill, a node
    failure) that never runs any of *this* process's exception handlers, so
    the debris is planted directly here rather than through a catchable
    exception (design D3.2)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    stale = vault.parent / ".vault-init-deadbeefdeadbeef"
    stale.mkdir()
    (stale / "leftover.txt").write_text("debris from an earlier crash", encoding="utf-8")

    result = cell_init.run_cell_init(vault)

    assert result.vault_created is True
    assert not stale.exists()
    assert not [p for p in vault.parent.iterdir() if p.name.startswith(".vault-init-")]


def test_cell_init_refuses_a_non_empty_directory_that_is_not_a_vault(
    tmp_path: Path,
) -> None:
    """A non-empty, unrecognized vault path is never overlaid (design D3.2):
    an interrupted init can never produce this, so it means something else
    wrote there."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "unrelated-file.txt").write_text("not a vault", encoding="utf-8")

    with pytest.raises(cell_init.CellInitError, match="CELL_INIT_VAULT_UNRECOGNIZED"):
        cell_init.run_cell_init(vault)

    assert (vault / "unrelated-file.txt").is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_cell_init_enforces_0700_on_vault_and_host_root(tmp_path: Path) -> None:
    """design D3.1: `/data/vault` and `/data/host` at mode 0700, every run --
    idempotent enforcement, not just first creation (a Kubernetes `fsGroup`
    volume can leave a fresh mount setgid at a looser mode)."""
    vault = tmp_path / "vault"
    host = tmp_path / "host"
    host.mkdir()
    os.chmod(host, 0o2755)

    result = cell_init.run_cell_init(vault, host_root=host)

    assert result.vault_created is True
    assert stat.S_IMODE(os.stat(vault).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(host).st_mode) == 0o700


def test_cell_init_without_a_host_root_never_touches_one(tmp_path: Path) -> None:
    """`host_root` is opt-in: a caller with no stake in a host root (every
    other test in this file) must never have one created for it."""
    vault = tmp_path / "vault"

    result = cell_init.run_cell_init(vault)

    assert result.vault_created is True
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vault"]


# ------------------------------------------------------------- CLI: `exomem cell-init`


def test_cell_init_cli_reports_vault_created_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = tmp_path / "vault"

    code = _cell_init_main(["--vault", str(vault), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"vault_created": True}
    assert _is_vault(vault)


def test_cell_init_cli_rejects_a_relative_vault_path_with_one_json_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _cell_init_main(["--vault", "relative/path"])

    assert code == 1
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "ok": False,
        "step": "vault_path",
        "error_code": "CELL_INIT_VAULT_INVALID",
    }
    assert "relative/path" not in out


def test_cell_init_cli_reports_a_failure_as_one_json_line_no_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """design D3 "Output": a failure prints exactly one JSON line with a
    stable code, never a traceback (which would carry absolute paths)."""
    vault = tmp_path / "some-very-specific-absolute-path" / "vault"

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated state migration failure")

    monkeypatch.setattr(state_migration, "migrate_vault_state_offline", boom)

    code = _cell_init_main(["--vault", str(vault)])

    assert code == 1
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "ok": False,
        "step": "state_migration",
        "error_code": "CELL_INIT_STATE_MIGRATION_FAILED",
    }
    assert str(vault) not in out
    assert "Traceback" not in out


def test_cell_init_cli_installs_redaction_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import privacy_log

    calls = {"n": 0}
    monkeypatch.setattr(
        privacy_log,
        "install_hosted_log_redaction",
        lambda: calls.__setitem__("n", calls["n"] + 1),
    )

    code = _cell_init_main(["--vault", str(tmp_path / "vault"), "--json"])

    assert code == 0
    assert calls["n"] == 1


def test_cell_init_cli_never_touches_a_host_root_outside_cloud_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Path.home()` is only enforced inside a real cloud cell (D3.1); a bare
    local invocation must never chmod a developer's actual home directory."""
    monkeypatch.delenv("EXOMEM_CLOUD_CELL", raising=False)
    real_run = cell_init.run_cell_init
    captured: dict[str, object] = {}

    def spy(vault_root, *, host_root=None):
        captured["host_root"] = host_root
        return real_run(vault_root, host_root=host_root)

    monkeypatch.setattr(cell_init, "run_cell_init", spy)

    code = _cell_init_main(["--vault", str(tmp_path / "vault"), "--json"])

    assert code == 0
    assert captured["host_root"] is None


def _fake_passwd_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    import pwd

    real = pwd.getpwuid

    def fake_getpwuid(uid: int):
        entry = real(uid)
        return pwd.struct_passwd((*entry[:5], str(home), *entry[6:]))

    monkeypatch.setattr(pwd, "getpwuid", fake_getpwuid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX passwd database only")
def test_cell_init_cli_enforces_the_passwd_home_root_in_cloud_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside a real cloud cell, the account's passwd home (the image's
    `usermod --home /data/host`) is passed through as `host_root` (design
    D3.1) -- the same entry standalone custody resolves, never `$HOME`, which
    a pod spec can override."""
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    passwd_home = tmp_path / "passwd-home"
    decoy_home = tmp_path / "decoy-home"
    _fake_passwd_home(monkeypatch, passwd_home)
    monkeypatch.setenv("HOME", str(decoy_home))
    real_run = cell_init.run_cell_init
    captured: dict[str, object] = {}

    def spy(vault_root, *, host_root=None):
        captured["host_root"] = host_root
        return real_run(vault_root, host_root=host_root)

    monkeypatch.setattr(cell_init, "run_cell_init", spy)

    code = _cell_init_main(["--vault", str(tmp_path / "vault"), "--json"])

    assert code == 0
    assert captured["host_root"] == passwd_home
    assert stat.S_IMODE(os.stat(passwd_home).st_mode) == 0o700
    assert not decoy_home.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks and modes only")
@pytest.mark.parametrize("which", ["vault", "host"])
def test_cell_init_refuses_a_symlinked_directory_without_touching_its_target(
    tmp_path: Path, which: str
) -> None:
    """A planted symlink at `/data/vault` or `/data/host` is refused, and its
    target's mode is never changed on the way to that refusal."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.chmod(target, 0o755)
    vault = tmp_path / "vault"
    host = tmp_path / "host"
    (vault if which == "vault" else host).symlink_to(target, target_is_directory=True)

    with pytest.raises(cell_init.CellInitError) as caught:
        cell_init.run_cell_init(vault, host_root=host)

    assert caught.value.code == "CELL_INIT_DIRECTORY_FAILED"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o755
