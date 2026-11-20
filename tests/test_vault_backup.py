"""The personal vault backup, driven end to end against a real restic and a disposable repository.

Each test builds a throwaway vault, points the backup config at a repository under the test's temporary folder,
and injects damage on disk. The data is invented. A test that needs restic fails where `EXOMEM_REQUIRE_RESTIC`
is set (the CI job that installs it) and skips elsewhere with the install hint, so a green run in the job that
installs restic never means the restic tests were quietly absent.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from filelock import FileLock

from exomem import preserve, private_state, reserved_paths, state_paths, vault_backup
from exomem.governance.principal import library_scope

#: Keep rules combine as a union in restic, so "keep only the newest snapshot" must zero the other three.
ONLY_THE_LAST = {"keep_last": 1, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0}
RECORDING = "Field recording, side A: the river gauge read 2.4 m at dawn.\n"
NOTE = "---\ntitle: Tidal gauge\n---\nThe gauge at the east pier read 3.1 m.\n"
NOTE_PATH = "Knowledge Base/Notes/tidal-gauge.md"


@pytest.fixture
def restic_binary() -> str:
    found = shutil.which("restic")
    if found is None:
        if os.environ.get("EXOMEM_REQUIRE_RESTIC"):
            pytest.fail("restic is not on PATH: the core-tests job installs the pinned release with scripts/ci-install-restic.sh")
        pytest.skip("restic is not on PATH; run scripts/ci-install-restic.sh or install restic >= 0.17.1")
    return found


@pytest.fixture
def configured(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A function that writes the owner's config for the vault with the given overrides; returns the repository."""
    repository = tmp_path / "repository"

    def configure(**overrides: object) -> Path:
        secrets = tmp_path / "owner-secrets"
        secrets.mkdir(exist_ok=True)
        (secrets / "password").write_text("an invented repository password\n")
        entry = {"repository": str(repository), "password_file": str(secrets / "password"), "keep_last": 5,
                 "sample_size": 500, "check_subset_parts": 1, **overrides}
        (secrets / "backup.json").write_text(json.dumps({"vaults": {str(vault.resolve()): entry}}))
        monkeypatch.setenv(vault_backup.CONFIG_ENV, str(secrets / "backup.json"))
        return repository

    return configure


def _restic(repository: Path, tmp_path: Path, *args: str) -> str:
    environment = {**os.environ, "RESTIC_REPOSITORY": str(repository),
                   "RESTIC_PASSWORD_FILE": str(tmp_path / "owner-secrets" / "password")}
    return subprocess.run(["restic", *args], env=environment, capture_output=True, text=True, check=True).stdout


def _snapshot_ids(repository: Path, tmp_path: Path) -> list[str]:
    listed = json.loads(_restic(repository, tmp_path, "snapshots", "--json"))
    return [snapshot["id"] for snapshot in sorted(listed, key=lambda snapshot: snapshot["time"])]


def _stored_original(vault: Path) -> Path:
    with library_scope():
        captured = preserve.preserve(vault, scope="Field", category="Recordings", filename="side-a.txt",
                                     content=RECORDING)
    return vault / captured.path


def _write_note(vault: Path) -> Path:
    path = vault / NOTE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NOTE)
    return path


def _state_dir(vault: Path) -> Path:
    return state_paths.ensure_vault_state_dir(vault)


def test_the_whole_workflow_backs_up_verifies_and_recovers_a_deleted_note(
    restic_binary: str, vault: Path, tmp_path: Path, configured
) -> None:
    repository = configured()
    original = _stored_original(vault)
    note = _write_note(vault)
    assert vault_backup.status(vault)["last_backup"] == {"state": "unknown"}

    first = vault_backup.run_now(vault, init_repository=True)

    assert first["ran"] == ["backup", "forget", "check", "sample"]
    assert [first[name]["state"] for name in first["ran"]] == ["complete", "ok", "ok", "ok"]
    # The sampled restore proved the original against the digest recorded at its admission.
    assert first["sample"]["originals"]["verified"] >= 1 and first["sample"]["originals"]["failed"] == 0
    assert first["sample"]["notes"]["verified"] >= 1 and first["sample"]["notes"]["failed"] == 0
    shown = vault_backup.status(vault)
    assert (shown["last_backup"]["state"], shown["last_check"]["state"], shown["last_sampled_restore"]["state"]) == (
        "complete", "ok", "ok")
    assert shown["findings"] == [] and shown["last_backup"]["snapshot_id"] == _snapshot_ids(repository, tmp_path)[0]
    [first_snapshot] = _snapshot_ids(repository, tmp_path)

    kept = note.read_bytes()
    note.unlink()
    vault_backup.run_now(vault)
    assert len(_snapshot_ids(repository, tmp_path)) == 2
    target = tmp_path / "recovered"
    vault_before = sorted(path.relative_to(vault) for path in vault.rglob("*"))

    result = vault_backup.restore(vault, target, snapshot=first_snapshot, paths=[NOTE_PATH])

    assert result["files"] == 1 and (target / NOTE_PATH).read_bytes() == kept
    assert sorted(path.relative_to(vault) for path in vault.rglob("*")) == vault_before  # the vault was not written
    assert original.read_text() == RECORDING


def test_the_owner_cli_backs_up_reports_and_restores_into_a_separate_directory(
    restic_binary: str, vault: Path, tmp_path: Path, configured, capsys: pytest.CaptureFixture[str]
) -> None:
    from exomem.__main__ import main

    configured()
    note = _write_note(vault)
    vault_arg = ["--vault", str(vault)]

    assert main(["backup", "run", "--init-repository", *vault_arg]) == 0
    assert "backup: complete" in capsys.readouterr().out
    assert main(["backup", "status", *vault_arg]) == 0
    shown = capsys.readouterr().out
    assert "last backup: complete" in shown and "last sampled restore: ok" in shown and "finding:" not in shown
    assert main(["backup", "restore", *vault_arg, "--target", str(tmp_path / "out"), "--path", NOTE_PATH]) == 0
    assert (tmp_path / "out" / NOTE_PATH).read_bytes() == note.read_bytes()
    assert main(["backup", "restore", *vault_arg, "--target", str(vault / "inside"), "--path", NOTE_PATH]) == 1
    assert "target_overlaps_vault" in capsys.readouterr().err and not (vault / "inside").exists()


def test_retention_forgets_older_snapshots_after_a_complete_backup(
    restic_binary: str, vault: Path, tmp_path: Path, configured
) -> None:
    repository = configured(**ONLY_THE_LAST)

    vault_backup.run_now(vault, init_repository=True)
    [first] = _snapshot_ids(repository, tmp_path)
    vault_backup.run_now(vault)

    remaining = _snapshot_ids(repository, tmp_path)
    assert len(remaining) == 1 and remaining != [first]


def _wait_for(condition, seconds: float = 90.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the backup thread did not reach the expected state"
        time.sleep(0.2)


def test_the_service_thread_backs_up_on_its_own_and_never_creates_the_repository(
    restic_binary: str, vault: Path, tmp_path: Path, configured
) -> None:
    repository = configured()
    try:
        assert vault_backup.start(vault) is not None
        _wait_for(lambda: vault_backup.status(vault).get("last_error"))
    finally:
        vault_backup.stop(timeout=60)
    missing = vault_backup.status(vault)
    assert missing["last_error"]["code"] == "repository_missing"
    assert missing["last_backup"] == {"state": "never"}  # a readable record that holds no backup yet
    assert not repository.exists()  # a typo in the repository path must not start a backup somewhere new

    _restic(repository, tmp_path, "init")
    try:
        vault_backup.start(vault)
        _wait_for(lambda: vault_backup.status(vault)["last_backup"]["state"] == "complete")
    finally:
        vault_backup.stop(timeout=60)
    assert len(_snapshot_ids(repository, tmp_path)) == 1


def test_a_partial_backup_keeps_history_and_names_the_state_file_that_failed(
    restic_binary: str, vault: Path, tmp_path: Path, configured
) -> None:
    repository = configured(**ONLY_THE_LAST)
    vault_backup.run_now(vault, init_repository=True)
    # A file that carries SQLite's header but is not a database: staging it must fail without stopping the backup.
    (_state_dir(vault) / "collections.sqlite").write_bytes(b"SQLite format 3\x00" + b"not a database" * 100)

    outcome = vault_backup.run_now(vault)

    assert outcome["backup"]["state"] == "partial" and "forget" not in outcome["ran"]
    assert outcome["backup"]["snapshot_id"]
    assert len(_snapshot_ids(repository, tmp_path)) == 2  # keep_last=1 would have removed the first
    [finding] = [item for item in vault_backup.status(vault)["findings"] if item["activity"] == "backup"]
    assert finding["code"] == "state_copy_failed"


def test_state_that_cannot_be_rebuilt_is_staged_consistently_and_rebuildable_state_is_left_out(
    restic_binary: str, vault: Path, tmp_path: Path, configured
) -> None:
    configured()
    state = _state_dir(vault)
    (state / ".review-state.json").write_text('{"decided": ["a"]}')
    rebuildable = next(
        descriptor for descriptor in reserved_paths.internal_state_registry()
        if descriptor.placement is reserved_paths.StatePlacement.EXTERNAL_STATE and descriptor.rebuildable
        and descriptor.exact
    )
    (state / rebuildable.exact[0]).write_text("an index the product rebuilds")
    database = sqlite3.connect(state / "collections.sqlite")
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("CREATE TABLE decisions (name TEXT)")
    database.execute("INSERT INTO decisions VALUES ('kept in the write-ahead log')")
    database.commit()  # the row lives in the -wal file while this connection stays open
    try:
        vault_backup.run_now(vault, init_repository=True)
    finally:
        database.close()

    target = tmp_path / "recovered-state"
    vault_backup.restore(vault, target, state=True)

    assert (target / ".review-state.json").read_text() == '{"decided": ["a"]}'
    assert not (target / rebuildable.exact[0]).exists()
    assert not list(target.glob("collections.sqlite-*"))  # no raw write-ahead or shared-memory copy
    restored = sqlite3.connect(target / "collections.sqlite")
    assert restored.execute("SELECT name FROM decisions").fetchall() == [("kept in the write-ahead log",)]
    restored.close()


def test_originals_left_out_by_the_owner_are_counted_and_the_sidecar_stays(
    restic_binary: str, vault: Path, tmp_path: Path, configured
) -> None:
    repository = configured(include_originals=False)
    original = _stored_original(vault)
    sidecar = next(path for path in original.parent.iterdir() if path != original and path.suffix == ".md")

    outcome = vault_backup.run_now(vault, init_repository=True)

    listing = (json.loads(line) for line in _restic(repository, tmp_path, "ls", "latest", "--json").splitlines())
    held = {entry["path"] for entry in listing if entry.get("type") == "file"}
    assert str(sidecar) in held and str(original) not in held
    assert outcome["sample"]["originals"] == {"state": "excluded_by_owner"}
    excluded = vault_backup.status(vault)["originals_excluded"]
    assert excluded["count"] >= 1
    # The count of originals without a recoverable replica needs the replica-custody change: unknown, never 0.
    assert excluded["without_recoverable_replica"] == "unknown"


def test_a_damaged_repository_pack_is_a_finding_in_the_check_and_the_sampled_restore(
    restic_binary: str, vault: Path, tmp_path: Path, configured
) -> None:
    repository = configured(check_interval_days=1e-9, sample_interval_days=1e-9, sample_size=5)
    _stored_original(vault)
    vault_backup.run_now(vault, init_repository=True)
    for pack in (repository / "data").rglob("*"):
        if pack.is_file():
            pack.chmod(0o600)  # restic writes its packs read-only
            raw = bytearray(pack.read_bytes())
            for offset in range(32, len(raw), 64):
                raw[offset] ^= 0xFF
            pack.write_bytes(bytes(raw))

    outcome = vault_backup.run_cycle(vault, force=True)

    assert outcome["check"]["state"] == "failed" and outcome["sample"]["state"] == "failed"
    shown = vault_backup.status(vault)
    assert shown["last_check"]["state"] == "failed" and shown["last_sampled_restore"]["state"] == "failed"
    by_activity = {finding["activity"] for finding in shown["findings"]}
    assert by_activity == {"check", "sample"}
    assert all(finding["code"] for finding in shown["findings"])
    assert any(finding.get("path") for finding in shown["findings"] if finding["activity"] == "sample")


def test_status_reads_restic_not_found_when_the_executable_is_missing(
    vault: Path, tmp_path: Path, configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured()
    monkeypatch.setenv("PATH", str(tmp_path / "no-executables-here"))

    shown = vault_backup.status(vault)

    assert shown["state"] == "restic_not_found" and shown["message"] == "restic not found"
    with pytest.raises(vault_backup.BackupError) as refused:
        vault_backup.run_now(vault)
    assert refused.value.code == "restic_not_found"


def test_nothing_runs_until_the_owner_names_a_config(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(vault_backup.CONFIG_ENV, raising=False)

    assert vault_backup.status(vault)["state"] == "not_configured"
    assert vault_backup.start(vault) is None


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"password_file": "inside-vault"}, "password_file"),
        ({"repository": "inside-vault"}, "repository"),
        ({"keep_last": 0, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0}, "keep"),
        ({"include_originals": "no"}, "include_originals"),
    ],
)
def test_an_unusable_entry_is_reported_with_the_field_and_never_run(
    vault: Path, configured, overrides: dict[str, object], field: str
) -> None:
    configured(**{key: str(vault / "x") if value == "inside-vault" else value for key, value in overrides.items()})

    shown = vault_backup.status(vault)

    assert shown["state"] == "invalid_config" and shown["field"] == field
    with pytest.raises(vault_backup.ConfigError):
        vault_backup.run_now(vault)


def test_a_second_flight_is_refused_while_one_runs(vault: Path, configured) -> None:
    configured()
    store = private_state.store(vault, vault_backup.STORE_DIRNAME)

    with FileLock(store / "flight.lock"), pytest.raises(vault_backup.BackupError) as refused:
        vault_backup.run_now(vault)

    assert refused.value.code == "already_running"


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("vault/Knowledge Base/restored", "target_overlaps_vault"),
        ("vault", "target_overlaps_vault"),
        ("", "target_overlaps_vault"),  # the folder that holds the vault
        ("not-empty", "target_not_empty"),
    ],
)
def test_a_restore_target_that_could_overwrite_the_vault_or_files_is_refused(
    vault: Path, tmp_path: Path, configured, target: str, code: str
) -> None:
    configured()
    (tmp_path / "not-empty").mkdir()
    (tmp_path / "not-empty" / "mine.txt").write_text("keep")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    with pytest.raises(vault_backup.BackupError) as refused:
        vault_backup.restore(vault, tmp_path / target if target else tmp_path, paths=[NOTE_PATH])

    assert refused.value.code == code
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("path", ["../outside.md", "/etc/passwd", "Knowledge Base/../../x", ""])
def test_a_restore_path_that_leaves_the_vault_is_refused(vault: Path, tmp_path: Path, configured, path: str) -> None:
    configured()

    with pytest.raises(vault_backup.BackupError) as refused:
        vault_backup.restore(vault, tmp_path / "recovered", paths=[path])

    assert refused.value.code == "invalid_path"


def test_a_failing_restic_leaks_no_secret_into_the_log_or_the_record(
    vault: Path, tmp_path: Path, configured, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stand-in restic that echoes its environment to stderr, as a backend error text may."""
    shim = tmp_path / "bin" / "restic"
    shim.parent.mkdir()
    shim.write_text('#!/bin/sh\n[ "$1" = version ] && { echo "restic 0.19.1 compiled with go"; exit 0; }\n'
                    'echo "fatal: cannot open $RESTIC_REPOSITORY with $INVENTED_SECRET" >&2\nexit 1\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim.parent}:{os.environ['PATH']}")
    credentials = tmp_path / "owner-secrets" / "credentials"
    configured(repository="rest:https://owner:invented-pass-1234@backup.invalid/vault",
               environment_file=str(credentials))
    credentials.write_text("INVENTED_SECRET=invented-token-9876\n")
    caplog.set_level(logging.DEBUG)

    with pytest.raises(vault_backup.BackupError):
        vault_backup.run_now(vault)

    everything = caplog.text + json.dumps(vault_backup.status(vault)) + (
        (private_state.store(vault, vault_backup.STORE_DIRNAME) / "status.json").read_text())
    assert "invented-pass-1234" not in everything and "invented-token-9876" not in everything
    assert "restic cat exited 1" in caplog.text  # the failure itself stays visible


def test_stopping_the_service_ends_a_running_backup_and_leaves_no_false_failure(
    vault: Path, tmp_path: Path, configured, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stand-in restic that runs for an hour, as a first backup of a large vault may."""
    shim = tmp_path / "bin" / "restic"
    shim.parent.mkdir()
    shim.write_text('#!/bin/sh\n[ "$1" = version ] && { echo "restic 0.19.1 compiled with go"; exit 0; }\n'
                    '[ "$1" = cat ] && exit 0\nexec sleep 3600\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim.parent}:{os.environ['PATH']}")
    configured()
    started = time.monotonic()
    try:
        worker = vault_backup.start(vault)
        _wait_for(lambda: subprocess.run(["pgrep", "-f", "sleep 3600"], capture_output=True).returncode == 0)
    finally:
        vault_backup.stop(timeout=30, worker=worker)
    assert time.monotonic() - started < 30  # the service stops in seconds, not when restic's own timeout expires
    assert not worker.is_alive()
    status = vault_backup.status(vault)
    assert status["last_backup"]["state"] == "unknown" and status["findings"] == []  # a stop is not a failed backup
