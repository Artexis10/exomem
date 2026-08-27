from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem.__main__ import main
from exomem.governance import (
    authorization_custody,
    schema_downmigration,
    schema_migration,
    schema_v4,
    store,
)

ACTIVE_DIGEST = "a" * 64


def test_governance_schema_plan_migration_emits_reviewable_target(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    summary = {
        "schema_version": 3,
        "logical_vault_id": "logical-vault-cli",
        "activation_store_id": "activation-store-cli",
        "activation_epoch": 1,
        "activation_state_digest": ACTIVE_DIGEST,
        "policy_generation_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "policy_fingerprint": "b" * 64,
        "projector_schema_version": 1,
        "catalog_generation": 1,
        "projection_namespace_id": "c" * 64,
        "source_store_digest": "e" * 64,
        "projection_rows_digest": "d" * 64,
        "item_count": 7,
        "plan_digest": "f" * 64,
    }
    calls: list[tuple[Path, int]] = []

    def prepare(root: Path, *, now: int):
        calls.append((root, now))
        return object()

    monkeypatch.setattr(schema_migration, "prepare_forward_migration", prepare)
    monkeypatch.setattr(schema_migration, "plan_summary", lambda _plan: summary)

    assert (
        main(
            [
                "governance-schema",
                "plan-migration",
                "--vault",
                str(vault),
                "--json",
            ]
        )
        == 0
    )

    assert len(calls) == 1 and calls[0][0] == vault
    assert isinstance(calls[0][1], int) and calls[0][1] > 0
    assert json.loads(capsys.readouterr().out) == summary


def test_governance_schema_stage_migration_requires_digest_bound_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[object] = []
    monkeypatch.setattr(
        schema_migration,
        "stage_forward_migration",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    code = main(
        [
            "governance-schema",
            "stage-migration",
            "--vault",
            str(vault),
            "--expected-plan-digest",
            "f" * 64,
            "--json",
        ]
    )

    assert code == 2
    assert calls == []
    assert json.loads(capsys.readouterr().out) == {
        "staged": False,
        "reason": "confirmation_required",
        "plan_digest": "f" * 64,
    }


def test_governance_schema_stage_migration_returns_content_free_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    terminal = schema_migration.ForwardMigrationStageResult(
        plan_digest="f" * 64,
        projection_namespace_id="c" * 64,
        projection_rows_digest="d" * 64,
        item_count=7,
    )
    calls: list[tuple[Path, str, int]] = []

    def stage(root: Path, *, expected_plan_digest: str, now: int):
        calls.append((root, expected_plan_digest, now))
        return terminal

    monkeypatch.setattr(schema_migration, "stage_forward_migration", stage)

    assert (
        main(
            [
                "governance-schema",
                "stage-migration",
                "--vault",
                str(vault),
                "--expected-plan-digest",
                "f" * 64,
                "--yes",
                "--json",
            ]
        )
        == 0
    )

    assert len(calls) == 1
    assert calls[0][0] == vault and calls[0][1] == "f" * 64
    assert isinstance(calls[0][2], int) and calls[0][2] > 0
    assert json.loads(capsys.readouterr().out) == {
        "staged": True,
        "schema_version": 3,
        "plan_digest": "f" * 64,
        "projection_namespace_id": "c" * 64,
        "projection_rows_digest": "d" * 64,
        "item_count": 7,
    }


def test_governance_schema_stage_migration_refuses_a_stale_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(
        schema_migration,
        "stage_forward_migration",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            schema_migration.ForwardMigrationPlanMismatch
        ),
    )

    code = main(
        [
            "governance-schema",
            "stage-migration",
            "--vault",
            str(vault),
            "--expected-plan-digest",
            "f" * 64,
            "--yes",
            "--json",
        ]
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "GOVERNANCE_SCHEMA_PLAN_MISMATCH",
        "message": "the reviewed migration plan changed; no activation was attempted",
    }


def test_governance_schema_commit_migration_requires_digest_bound_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[object] = []
    monkeypatch.setattr(
        schema_migration,
        "commit_forward_migration",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    code = main(
        [
            "governance-schema",
            "commit-migration",
            "--vault",
            str(vault),
            "--expected-plan-digest",
            "f" * 64,
            "--json",
        ]
    )

    assert code == 2
    assert calls == []
    assert json.loads(capsys.readouterr().out) == {
        "migrated": False,
        "reason": "confirmation_required",
        "plan_digest": "f" * 64,
    }


@pytest.mark.parametrize("replayed", [False, True])
def test_governance_schema_commit_migration_returns_content_free_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    replayed: bool,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id="logical-vault-cli",
        activation_store_id="activation-store-cli",
        activation_epoch=1,
        activation_state_digest="a" * 64,
        policy_generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        policy_fingerprint="b" * 64,
        projector_schema_version=1,
        catalog_generation=1,
        projection_namespace_id="c" * 64,
    )
    result = schema_migration.ForwardMigrationResult(
        schema_version=4,
        target=target,
        plan_digest="f" * 64,
        source_store_digest="e" * 64,
        backup_reference="exomem-governance-v3-backup://sha256/" + "d" * 64,
        replayed=replayed,
    )
    calls: list[tuple[Path, str, int]] = []

    def commit(root: Path, *, expected_plan_digest: str, now: int):  # noqa: ANN202
        calls.append((root, expected_plan_digest, now))
        return result

    monkeypatch.setattr(schema_migration, "commit_forward_migration", commit)

    assert (
        main(
            [
                "governance-schema",
                "commit-migration",
                "--vault",
                str(vault),
                "--expected-plan-digest",
                "f" * 64,
                "--yes",
                "--json",
            ]
        )
        == 0
    )

    assert len(calls) == 1
    assert calls[0][0] == vault and calls[0][1] == "f" * 64
    assert isinstance(calls[0][2], int) and calls[0][2] > 0
    assert json.loads(capsys.readouterr().out) == {
        "migrated": True,
        "schema_version": 4,
        "logical_vault_id": "logical-vault-cli",
        "activation_store_id": "activation-store-cli",
        "activation_epoch": 1,
        "activation_state_digest": "a" * 64,
        "policy_generation_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "policy_fingerprint": "b" * 64,
        "projector_schema_version": 1,
        "catalog_generation": 1,
        "projection_namespace_id": "c" * 64,
        "plan_digest": "f" * 64,
        "source_store_digest": "e" * 64,
        "backup_reference": "exomem-governance-v3-backup://sha256/" + "d" * 64,
        "replayed": replayed,
    }


@pytest.fixture
def schema_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    control = SimpleNamespace(
        governance_enrolled=True,
        logical_vault_id="logical-vault-cli",
        activation_store_id="activation-store-cli",
        activation_epoch=7,
        activation_state_digest=ACTIVE_DIGEST,
        serving_membership_epoch=12,
    )
    membership = SimpleNamespace(
        epoch=12,
        replicas=(
            SimpleNamespace(
                replica_id="standalone",
                state="DRAINING",
                schema_version=4,
                issuance_stopped=True,
                no_in_flight=True,
            ),
        ),
    )
    custody = SimpleNamespace(control=control, serving_membership=membership)
    monkeypatch.setattr(
        authorization_custody,
        "load_authorization_custody",
        lambda root, *, now: custody,
    )
    monkeypatch.setattr(store, "authorization_session_schema_version", lambda root: 4)
    return vault, control


def test_governance_schema_status_is_content_free_and_exact(
    schema_state,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault, _control = schema_state

    assert main(["governance-schema", "status", "--vault", str(vault), "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 4,
        "governance_enrolled": True,
        "logical_vault_id": "logical-vault-cli",
        "activation_store_id": "activation-store-cli",
        "activation_epoch": 7,
        "activation_state_digest": ACTIVE_DIGEST,
        "serving_membership_epoch": 12,
        "replicas": [
            {
                "replica_id": "standalone",
                "state": "DRAINING",
                "schema_version": 4,
                "issuance_stopped": True,
                "no_in_flight": True,
            }
        ],
    }


def test_governance_schema_downmigration_requires_digest_bound_confirmation(
    schema_state,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault, _control = schema_state
    calls: list[object] = []
    monkeypatch.setattr(
        schema_downmigration,
        "downmigrate_enrolled_v4_store",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    code = main(
        [
            "governance-schema",
            "downmigrate",
            "--vault",
            str(vault),
            "--expected-activation-state-digest",
            ACTIVE_DIGEST,
            "--json",
        ]
    )

    assert code == 2
    assert calls == []
    assert json.loads(capsys.readouterr().out) == {
        "downmigrated": False,
        "reason": "confirmation_required",
        "schema_version": 4,
        "logical_vault_id": "logical-vault-cli",
        "activation_state_digest": ACTIVE_DIGEST,
    }


def test_governance_schema_downmigration_refuses_a_stale_owner_digest(
    schema_state,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault, _control = schema_state
    calls: list[object] = []
    monkeypatch.setattr(
        schema_downmigration,
        "downmigrate_enrolled_v4_store",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    code = main(
        [
            "governance-schema",
            "downmigrate",
            "--vault",
            str(vault),
            "--expected-activation-state-digest",
            "b" * 64,
            "--yes",
            "--json",
        ]
    )

    assert code == 1
    assert calls == []
    assert json.loads(capsys.readouterr().out) == {
        "error": "GOVERNANCE_SCHEMA_TARGET_MISMATCH",
        "message": "the reviewed activation state changed; no downmigration was attempted",
    }


@pytest.mark.parametrize("replayed", [False, True])
def test_governance_schema_downmigration_returns_the_durable_terminal(
    schema_state,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    replayed: bool,
) -> None:
    vault, control = schema_state
    result = schema_downmigration.OfflineDownmigrationResult(
        schema_version=3,
        active=SimpleNamespace(
            logical_vault_id=control.logical_vault_id,
            activation_store_id=control.activation_store_id,
            activation_epoch=control.activation_epoch,
            activation_state_digest=control.activation_state_digest,
        ),
        recovery_event_id="b" * 64,
        recovery_plan_digest="c" * 64,
        recovery_target_digest="d" * 64,
        recovery_terminal_digest="e" * 64,
        replayed=replayed,
    )
    calls: list[tuple[object, int]] = []

    def downmigrate(root, *, now: int):  # noqa: ANN001, ANN202
        calls.append((root, now))
        return result

    monkeypatch.setattr(
        schema_downmigration,
        "downmigrate_enrolled_v4_store",
        downmigrate,
    )

    assert (
        main(
            [
                "governance-schema",
                "downmigrate",
                "--vault",
                str(vault),
                "--expected-activation-state-digest",
                ACTIVE_DIGEST,
                "--yes",
                "--json",
            ]
        )
        == 0
    )

    assert len(calls) == 1 and calls[0][0] == vault
    assert isinstance(calls[0][1], int) and calls[0][1] > 0
    assert json.loads(capsys.readouterr().out) == {
        "downmigrated": True,
        "schema_version": 3,
        "logical_vault_id": "logical-vault-cli",
        "activation_store_id": "activation-store-cli",
        "activation_epoch": 7,
        "activation_state_digest": ACTIVE_DIGEST,
        "recovery_event_id": "b" * 64,
        "recovery_plan_digest": "c" * 64,
        "recovery_target_digest": "d" * 64,
        "recovery_terminal_digest": "e" * 64,
        "replayed": replayed,
    }
