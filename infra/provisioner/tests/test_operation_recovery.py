from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _module():
    import exomem_provisioner.operation_recovery as recovery

    return recovery


def test_recovery_command_has_only_fixed_modes_and_environment_free_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recovery = _module()

    assert recovery.main(["--help"]) == 0
    assert capsys.readouterr().out == (
        "exomem-provisioner-recover-init-retry - recover one proven hosted init retry "
        "false-negative; configuration is supplied through environment variables\n"
    )
    parser = recovery._parser()
    assert parser.parse_args(["preflight", "--stdin"]).mode == "preflight"
    for mode in ("reopen", "inspect", "verify-receipt"):
        assert parser.parse_args([mode, "--stdin"]).mode == mode
    with pytest.raises(SystemExit):
        parser.parse_args(["anything-else", "--stdin"])
    with pytest.raises(SystemExit):
        parser.parse_args(["preflight", "--operation-id", str(uuid.uuid4())])


@pytest.mark.parametrize(
    "raw",
    ["", "\n", "not-a-uuid\n", f"{uuid.uuid4()}\n{uuid.uuid4()}\n"],
)
def test_operation_identity_stdin_is_exactly_one_uuid(raw: str) -> None:
    recovery = _module()

    with pytest.raises(recovery.RecoveryRefusal, match="operation identity is invalid"):
        recovery.read_operation_identity(stdin=raw)


def test_operation_identity_stdin_never_leaks_confidential_value() -> None:
    recovery = _module()
    identity = str(uuid.uuid4())

    assert recovery.read_operation_identity(stdin=identity + "\n") == identity


def test_operation_identity_file_requires_owned_regular_mode_0600(tmp_path: Path) -> None:
    recovery = _module()
    identity = str(uuid.uuid4())
    path = tmp_path / "identity"
    path.write_text(identity + "\n", encoding="utf-8")
    path.chmod(0o600)

    assert recovery.read_operation_identity(identity_file=path) == identity

    path.chmod(0o640)
    with pytest.raises(recovery.RecoveryRefusal, match="operation identity file is unsafe"):
        recovery.read_operation_identity(identity_file=path)

    path.chmod(0o600)
    linked = tmp_path / "linked"
    linked.symlink_to(path)
    with pytest.raises(recovery.RecoveryRefusal, match="operation identity file is unsafe"):
        recovery.read_operation_identity(identity_file=linked)


def test_operation_identity_rejects_both_or_neither_source() -> None:
    recovery = _module()
    identity = str(uuid.uuid4())

    with pytest.raises(recovery.RecoveryRefusal, match="operation identity source is invalid"):
        recovery.read_operation_identity()
    with pytest.raises(recovery.RecoveryRefusal, match="operation identity source is invalid"):
        recovery.read_operation_identity(stdin=identity + "\n", identity_file=Path("/tmp/identity"))


def test_canonical_hash_is_order_stable_and_never_serializes_secret_fields() -> None:
    recovery = _module()
    first = {
        "operation": uuid.UUID("123e4567-e89b-42d3-a456-426614174000"),
        "state": recovery.OperationState.ERROR,
        "observed_at": datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
        "empty": None,
    }
    second = dict(reversed(tuple(first.items())))

    assert recovery.canonical_sha256(first) == recovery.canonical_sha256(second)
    receipt = recovery.RecoveryReceiptPayload(
        helper_source_sha256="9" * 64,
        operation_sha256="a" * 64,
        request_sha256="b" * 64,
        resources_sha256="c" * 64,
        reservation_sha256="d" * 64,
        tenant_fence_sha256="e" * 64,
        first_observation_sha256="f" * 64,
        second_observation_sha256="0" * 64,
        committed_operation_sha256="1" * 64,
    )
    encoded = recovery.canonical_receipt_bytes(receipt.to_payload())
    assert b"operation_sha256" in encoded
    assert b"ciphertext" not in encoded
    assert b"reference" not in encoded


def test_transactional_receipt_payload_is_content_free_and_hashable() -> None:
    recovery = _module()
    receipt = recovery.RecoveryReceiptPayload(
        helper_source_sha256="9" * 64,
        operation_sha256="a" * 64,
        request_sha256="b" * 64,
        resources_sha256="c" * 64,
        reservation_sha256="d" * 64,
        tenant_fence_sha256="e" * 64,
        first_observation_sha256="f" * 64,
        second_observation_sha256="0" * 64,
        committed_operation_sha256="1" * 64,
    )

    encoded = recovery.canonical_receipt_bytes(receipt.to_payload())
    assert set(json.loads(encoded)) == recovery._RECEIPT_PAYLOAD_KEYS
    assert b"ciphertext" not in encoded and b"reference" not in encoded


def test_recovery_receipt_model_is_one_to_one_content_free_and_immutable() -> None:
    from exomem_provisioner.database import DATABASE_REVISION
    from exomem_provisioner.models import OperationRecoveryReceipt

    assert DATABASE_REVISION == "0007_operation_recovery_receipt"
    table = OperationRecoveryReceipt.__table__
    assert set(table.c.keys()) == {
        "operation_id",
        "schema_version",
        "helper_source_sha256",
        "old_state",
        "old_checkpoint",
        "new_state",
        "new_checkpoint",
        "resource_count",
        "route_count",
        "init_job_present",
        "init_job_complete",
        "operation_sha256",
        "preserved_sha256",
        "request_sha256",
        "request_ciphertext_sha256",
        "resources_sha256",
        "reservation_sha256",
        "tenant_fence_sha256",
        "first_observation_sha256",
        "second_observation_sha256",
        "committed_operation_sha256",
        "committed_at",
    }
    assert table.c.operation_id.primary_key is True
    assert table.c.operation_id.foreign_keys
    assert {"tenant_id", "cell_id", "provider_operation_id"}.isdisjoint(table.c.keys())


def test_recovery_refusal_output_is_fixed_and_content_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recovery = _module()
    secret = "secret-operation-and-dsn-value"

    assert recovery.emit_result({"status": "refused", "refusal": "preflight-failed"}) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"refusal": "preflight-failed", "status": "refused"}
    assert secret not in output
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.emit_result({"status": "refused", "operation_id": secret})


def test_sqlite_is_refused_before_any_recovery_work() -> None:
    recovery = _module()

    with pytest.raises(recovery.RecoveryRefusal, match="PostgreSQL is required"):
        recovery.require_postgresql("sqlite")


def test_live_init_observation_allows_absent_or_complete_job_but_refuses_failed_or_terminating() -> None:
    recovery = _module()

    absent = recovery.LiveObservation(
        namespace_present=True,
        release_present=True,
        pvc_bound=True,
        volume_present=True,
        init_job_present=False,
        init_complete=False,
        init_failed_only=False,
        terminating=False,
        runtime_admitted=False,
        routes_present=0,
    )
    assert recovery.validate_live_observation(absent) is None
    assert recovery.validate_live_observation(
        replace(absent, init_job_present=True, init_complete=True)
    ) is None
    for changed in (
        {"init_job_present": True, "init_failed_only": True},
        {"pvc_bound": False},
        {"terminating": True},
        {"runtime_admitted": True},
        {"routes_present": 1},
    ):
        with pytest.raises(recovery.RecoveryRefusal, match="live recovery preflight failed"):
            recovery.validate_live_observation(
                replace(absent, **changed)
            )


def test_reopen_is_single_exact_cas_and_a_second_invocation_is_noop() -> None:
    recovery = _module()
    before = recovery.OperationPreState(
        action="provision",
        state="error",
        checkpoint="failed",
        error_code="PROVISIONER_PROVIDER_METADATA_CONFLICT",
        has_claim=False,
        has_result=False,
        finalized=True,
    )

    assert recovery.recovery_transition_values(before) == {
        "state": recovery.OperationState.PENDING,
        "checkpoint": "volume-owned",
        "error_code": None,
        "claim_owner": None,
        "claim_token": None,
        "claim_expires_at": None,
        "finalized_at": None,
    }
    with pytest.raises(recovery.RecoveryRefusal, match="already progressed"):
        recovery.recovery_transition_values(
            replace(before, state="pending", checkpoint="volume-owned")
        )


def test_main_converts_refusals_to_content_free_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    recovery = _module()

    async def refuse(_: object) -> dict[str, object]:
        raise recovery.RecoveryRefusal("preflight-failed")

    monkeypatch.setattr(recovery, "_run", refuse)

    assert recovery.main(["preflight", "--stdin"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "refusal": "preflight-failed",
        "status": "refused",
    }
