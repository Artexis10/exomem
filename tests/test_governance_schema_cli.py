from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from exomem.__main__ import main
from exomem.governance import (
    authorization_custody,
    schema_downmigration,
    store,
)

ACTIVE_DIGEST = "a" * 64


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
