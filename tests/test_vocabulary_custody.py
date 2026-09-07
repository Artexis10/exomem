from __future__ import annotations

from pathlib import Path

import pytest

from exomem.governance import (
    authorization_custody,
    authorization_hosted_mount,
    schema_downmigration,
    schema_migration,
)
from exomem.vocabulary_admission import VocabularyAdmissionError


def test_custody_transition_keeps_v1_and_refuses_v2_or_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "exomem.vocabulary_authority.transition_status", lambda _root, *, now: "v1"
    )
    authorization_custody._require_v1_vocabulary_transition(tmp_path, now=1)

    for mode in ("v2", "unavailable"):
        monkeypatch.setattr(
            "exomem.vocabulary_authority.transition_status",
            lambda _root, *, now, mode=mode: mode,
        )
        with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
            authorization_custody._require_v1_vocabulary_transition(tmp_path, now=1)


def test_new_custody_identity_refuses_existing_authority_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "retained.vocabulary-authority.activation.json"
    database = tmp_path / "database"
    marker.write_text("retained", encoding="utf-8")
    monkeypatch.setattr(
        "exomem.vocabulary_authority.authority_artifact_paths",
        lambda _control, _logical: (marker, database),
    )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody._require_vocabulary_identity_unused(
            tmp_path / "control.json", "fresh-logical-vault"
        )


def test_new_custody_identity_refuses_existing_authority_sqlite_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "retained.vocabulary-authority.activation.json"
    database = tmp_path / "retained.vocabulary-authority.sqlite"
    sidecar = database.with_name(f"{database.name}-wal")
    sidecar.write_text("retained", encoding="utf-8")
    monkeypatch.setattr(
        "exomem.vocabulary_authority.authority_artifact_paths",
        lambda _control, _logical: (marker, database),
    )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody._require_vocabulary_identity_unused(
            tmp_path / "control.json", "fresh-logical-vault"
        )


def test_hosted_republish_refuses_identity_change_with_authority_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "private"
    destination.mkdir()
    (destination / "keyring.json").write_bytes(b"old-keyring")
    (destination / "control.json").write_bytes(b"old-control")
    marker = tmp_path / "retained.vocabulary-authority.activation.json"
    database = tmp_path / "database"
    marker.write_text("retained", encoding="utf-8")

    class _Record:
        def __init__(self, *, cell_id: str, logical_vault_id: str, keyring_id: str) -> None:
            self.cell_id = cell_id
            self.logical_vault_id = logical_vault_id
            self.keyring_id = keyring_id

    monkeypatch.setattr(
        authorization_custody, "parse_keyring", lambda raw: raw
    )
    monkeypatch.setattr(
        authorization_custody,
        "parse_control_record",
        lambda raw, **_kwargs: _Record(
            cell_id="old-cell" if raw == b"old-control" else "new-cell",
            logical_vault_id="old-vault" if raw == b"old-control" else "new-vault",
            keyring_id="old-key" if raw == b"old-control" else "new-key",
        ),
    )
    monkeypatch.setattr(
        "exomem.vocabulary_authority.authority_artifact_paths",
        lambda _control, logical: (marker, database)
        if logical == "old-vault"
        else (tmp_path / "other-marker", tmp_path / "other-database"),
    )

    with pytest.raises(authorization_hosted_mount.HostedCustodyMountUnavailable):
        authorization_hosted_mount._refuse_authority_identity_change(
            destination,
            {
                "keyring.json": b"new-keyring",
                "control.json": b"new-control",
            },
        )


def test_hosted_republish_refuses_identity_change_with_retained_authority_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "private"
    destination.mkdir()
    (destination / "keyring.json").write_bytes(b"old-keyring")
    (destination / "control.json").write_bytes(b"old-control")
    database = tmp_path / "retained.vocabulary-authority.sqlite"
    database.with_name(f"{database.name}-wal").write_text("retained", encoding="utf-8")

    class _Record:
        def __init__(self, *, cell_id: str, logical_vault_id: str, keyring_id: str) -> None:
            self.cell_id = cell_id
            self.logical_vault_id = logical_vault_id
            self.keyring_id = keyring_id
            self.vocabulary_authority_floor = 1

    monkeypatch.setattr(authorization_custody, "parse_keyring", lambda raw: raw)
    monkeypatch.setattr(
        authorization_custody,
        "parse_control_record",
        lambda raw, **_kwargs: _Record(
            cell_id="old-cell" if raw == b"old-control" else "new-cell",
            logical_vault_id="old-vault" if raw == b"old-control" else "new-vault",
            keyring_id="old-key" if raw == b"old-control" else "new-key",
        ),
    )
    monkeypatch.setattr(
        "exomem.vocabulary_authority.authority_artifact_paths",
        lambda _control, logical: (tmp_path / f"{logical}.marker", database)
        if logical == "old-vault"
        else (tmp_path / "new.marker", tmp_path / "new.sqlite"),
    )

    with pytest.raises(authorization_hosted_mount.HostedCustodyMountUnavailable):
        authorization_hosted_mount._refuse_authority_identity_change(
            destination,
            {"keyring.json": b"new-keyring", "control.json": b"new-control"},
        )


def test_downmigration_refuses_before_rollback_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = False

    def refuse(_root: Path) -> None:
        raise VocabularyAdmissionError("VOCABULARY_RESTORE_REQUIRES_AUTHORITY")

    def rollback(_root: Path):  # noqa: ANN202
        nonlocal entered
        entered = True
        raise AssertionError("rollback must not start")

    monkeypatch.setattr("exomem.vocabulary_admission.require_restore_admission", refuse)
    monkeypatch.setattr(schema_downmigration.state_migration, "governance_rollback_session", rollback)

    with pytest.raises(VocabularyAdmissionError, match="VOCABULARY_RESTORE_REQUIRES_AUTHORITY"):
        schema_downmigration.downmigrate_enrolled_v4_store(tmp_path, now=1)

    assert entered is False


def test_forward_restore_refuses_before_rollback_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = False

    def refuse(_root: Path) -> None:
        raise VocabularyAdmissionError("VOCABULARY_AUTHORITY_UNAVAILABLE")

    def rollback(_root: Path):  # noqa: ANN202
        nonlocal entered
        entered = True
        raise AssertionError("rollback must not start")

    monkeypatch.setattr("exomem.vocabulary_admission.require_restore_admission", refuse)
    monkeypatch.setattr(schema_migration.state_migration, "governance_rollback_session", rollback)

    with pytest.raises(VocabularyAdmissionError, match="VOCABULARY_AUTHORITY_UNAVAILABLE"):
        schema_migration.restore_forward_migration_backup(
            tmp_path,
            expected_plan_digest="0" * 64,
            expected_backup_reference="exomem-backup://sha256/" + "0" * 64,
            now=1,
        )

    assert entered is False


def test_hosted_operator_keeps_vocabulary_restore_codes_stable() -> None:
    from exomem.hosted_operator import OperatorFailure

    for code in ("VOCABULARY_AUTHORITY_UNAVAILABLE", "VOCABULARY_RESTORE_REQUIRES_AUTHORITY"):
        assert OperatorFailure(code, command="restore-candidate").code == code
