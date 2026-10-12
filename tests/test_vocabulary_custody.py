from __future__ import annotations

from pathlib import Path

import pytest

from exomem.governance import (
    authorization_custody,
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


def test_downmigration_refuses_before_rollback_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = False

    def refuse(_root: Path, *, now: int) -> None:
        assert now == 1
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

    def refuse(_root: Path, *, now: int) -> None:
        assert now == 1
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
