from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    import exomem_provisioner.operation_recovery as recovery

    return recovery


def _selected_deployment_lock_json() -> str:
    values = (
        Path(__file__).resolve().parents[3] / "infra/helm/platform/values.validation.yaml"
    ).read_text(encoding="utf-8")
    match = re.search(r"^  deploymentLockJson: \|\n    (?P<lock>\{.*\})$", values, re.MULTILINE)
    assert match is not None
    return match.group("lock")


def _recovery_environment(**overrides: str) -> dict[str, str]:
    values = {
        "EXOMEM_RECOVERY_DATABASE_URL": "postgresql+asyncpg://recovery_role:password@db.example/recovery_db",
        "EXOMEM_RECOVERY_DATABASE_SCHEMA": "recovery_schema",
        "EXOMEM_RECOVERY_DATABASE_ROLE": "recovery_role",
        "EXOMEM_RECOVERY_DATABASE_LOCK_TIMEOUT_SECONDS": "1",
        "EXOMEM_RECOVERY_ENVELOPE_KEY": "e" * 32,
        "EXOMEM_RECOVERY_PROVIDER_RECOVERY_PUBLIC_KEY": "p" * 43,
        "EXOMEM_RECOVERY_DEPLOYMENT_LOCK_JSON": _selected_deployment_lock_json(),
        "EXOMEM_RECOVERY_SOURCE_DEPLOYMENT_LOCK_JSON": _selected_deployment_lock_json(),
        "EXOMEM_RECOVERY_RUNTIME_SELECTION": "active",
        "EXOMEM_RECOVERY_HCLOUD_TOKEN": "h" * 32,
        "EXOMEM_RECOVERY_HCLOUD_LOCATION": "fsn1",
    }
    values.update(overrides)
    return values


def test_recovery_settings_accept_only_the_exact_minimal_environment() -> None:
    from exomem_provisioner.recovery_settings import load_recovery_settings

    settings = load_recovery_settings(_recovery_environment())

    assert settings.database_name == "recovery_db"
    assert settings.deployment_lock.components.provisioner.image.endswith("b" * 64)
    assert settings.source_deployment_lock == settings.deployment_lock
    assert settings.runtime_selection == "active"
    assert settings.hcloud_location == "fsn1"
    for name, value in (
        (
            "EXOMEM_RECOVERY_DATABASE_URL",
            "postgresql+asyncpg://recovery_role:password@db-pooler.example/recovery_db",
        ),
        ("EXOMEM_RECOVERY_DATABASE_SCHEMA", "public"),
        ("EXOMEM_RECOVERY_DATABASE_LOCK_TIMEOUT_SECONDS", "0"),
        ("EXOMEM_RECOVERY_HCLOUD_TOKEN", "short"),
        ("EXOMEM_RECOVERY_DEPLOYMENT_LOCK_JSON", "{}"),
        ("EXOMEM_RECOVERY_RUNTIME_SELECTION", "rollback"),
        ("EXOMEM_RECOVERY_UNRELATED_SECRET", "forbidden"),
        ("EXOMEM_PROVISIONER_BEARER", "b" * 32),
        ("EXOMEM_PROVIDER_RECOVERY_SIGNING_KEY", "s" * 43),
    ):
        environment = _recovery_environment(**{name: value})
        with pytest.raises(ValueError):
            load_recovery_settings(environment)


def test_recovery_settings_retain_the_sanitized_session_pool_url() -> None:
    from exomem_provisioner.recovery_settings import load_recovery_settings

    settings = load_recovery_settings(
        _recovery_environment(
            EXOMEM_RECOVERY_DATABASE_URL=(
                "postgresql+asyncpg://recovery_role:password@session-pooler.example/"
                "recovery_db?pool_mode=session"
            )
        )
    )

    assert "pool_mode" not in settings.database_url.get_secret_value()
    for absent in _recovery_environment():
        environment = _recovery_environment()
        environment.pop(absent)
        with pytest.raises(ValueError):
            load_recovery_settings(environment)


def test_recovery_command_constructs_only_dedicated_recovery_settings() -> None:
    recovery = _module()
    source = Path(recovery.__file__).read_text(encoding="utf-8")

    assert "RecoverySettings" in source
    assert "ProvisionerSettings()" not in source
    assert "ProviderWorkerSettings()" not in source
    assert "VolumeWorkerSettings()" not in source


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
    for mode in (
        "reopen",
        "inspect",
        "verify-recovery",
        "retarget-preflight",
        "retarget",
        "verify-retarget",
        "resume-retarget-preflight",
        "resume-retarget",
        "verify-resume-retarget",
        "retry-retarget-preflight",
        "retry-retarget",
        "verify-retry-retarget",
        "successor-retarget-preflight",
        "successor-retarget",
        "verify-successor-retarget",
    ):
        assert parser.parse_args([mode, "--stdin"]).mode == mode
    with pytest.raises(recovery.RecoveryRefusal):
        parser.parse_args(["anything-else", "--stdin"])
    with pytest.raises(recovery.RecoveryRefusal):
        parser.parse_args(["preflight", "--operation-id", str(uuid.uuid4())])


def test_recovery_command_never_echoes_rejected_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recovery = _module()
    forbidden = str(uuid.uuid4())

    assert recovery.main(["preflight", "--operation-id", forbidden]) == 2

    captured = capsys.readouterr()
    assert forbidden not in captured.out
    assert forbidden not in captured.err
    assert json.loads(captured.out) == {
        "refusal": "command arguments are invalid",
        "status": "refused",
    }


def test_recovery_command_accepts_operation_identity_only_from_stdin(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recovery = _module()
    forbidden = "/secure/operator/operation-id"

    assert recovery.main(["preflight", "--identity-file", forbidden]) == 2

    captured = capsys.readouterr()
    assert forbidden not in captured.out
    assert forbidden not in captured.err
    assert json.loads(captured.out) == {
        "refusal": "command arguments are invalid",
        "status": "refused",
    }


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


def test_operation_identity_requires_stdin() -> None:
    recovery = _module()

    with pytest.raises(recovery.RecoveryRefusal, match="operation identity source is invalid"):
        recovery.read_operation_identity()


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
    marker = recovery.recovery_marker(
        preflight_sha256="a" * 64,
        helper_source_sha256="b" * 64,
        claim_generation=7,
        committed_at=datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    encoded = recovery.canonical_receipt_bytes(marker)
    assert b"preflight_sha256" in encoded
    assert b"helper_source_sha256" in encoded
    assert b"reference" not in encoded


def test_recovery_marker_is_content_free_and_exact() -> None:
    recovery = _module()
    marker = recovery.recovery_marker(
        preflight_sha256="a" * 64,
        helper_source_sha256="b" * 64,
        claim_generation=7,
        committed_at=datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    assert marker == {
        "schema": 1,
        "preflight_sha256": "a" * 64,
        "helper_source_sha256": "b" * 64,
        "claim_generation": 7,
        "committed_at": "2030-01-02T03:04:05Z",
    }
    assert recovery.parse_recovery_marker(marker) == marker
    for changed in (
        {"schema": 2},
        {"preflight_sha256": "short"},
        {"reference": "secret"},
    ):
        with pytest.raises(recovery.RecoveryRefusal):
            recovery.parse_recovery_marker({**marker, **changed})


def test_retarget_request_changes_only_the_v1_legacy_runtime_pair() -> None:
    recovery = _module()
    source = {
        "operationId": "provider-alpha",
        "checkpoint": "requested",
        "fenceGeneration": 7,
        "tenantId": "tenant-alpha",
        "cellId": "cell-alpha",
        "protocolVersion": "1",
        "releaseVersion": "0.66.0",
        "serviceCredential": "secret-alpha",
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
        "provisionMode": "serve",
    }

    target = recovery.retarget_provision_request(
        source,
        wire_protocol="exomem-cell-provisioner.v1",
        runtime_target={"releaseVersion": "0.68.1", "protocolVersion": "1"},
    )

    assert target == {**source, "releaseVersion": "0.68.1", "protocolVersion": "1"}
    assert recovery.request_targets_selected_runtime(
        target,
        wire_protocol="exomem-cell-provisioner.v1",
        runtime_target={"releaseVersion": "0.68.1", "protocolVersion": "1"},
    )
    assert not recovery.request_targets_selected_runtime(
        source,
        wire_protocol="exomem-cell-provisioner.v1",
        runtime_target={"releaseVersion": "0.68.1", "protocolVersion": "1"},
    )
    assert source["releaseVersion"] == "0.66.0"
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.retarget_provision_request(
            {**source, "provisionMode": "restore-candidate"},
            wire_protocol="exomem-cell-provisioner.v1",
            runtime_target={"releaseVersion": "0.68.1", "protocolVersion": "1"},
        )
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.retarget_provision_request(
            {**source, "runtimeTarget": {}},
            wire_protocol="exomem-cell-provisioner.v1",
            runtime_target={"releaseVersion": "0.68.1", "protocolVersion": "1"},
        )
    with pytest.raises(recovery.RecoveryRefusal, match="already targets"):
        recovery.retarget_provision_request(
            target,
            wire_protocol="exomem-cell-provisioner.v1",
            runtime_target={"releaseVersion": "0.68.1", "protocolVersion": "1"},
        )


def test_retarget_request_changes_only_the_complete_v2_runtime_target() -> None:
    recovery = _module()
    source_target = {
        "releaseVersion": "0.66.0",
        "protocolVersion": "1",
        "agentProfile": "hosted-alpha-agent-v4",
        "gatewayContractDigest": "a" * 64,
        "commandFingerprint": "b" * 64,
        "schemaDigest": "c" * 64,
        "compatibilityDigest": "d" * 64,
    }
    selected = {
        **source_target,
        "releaseVersion": "0.68.1",
        "gatewayContractDigest": "e" * 64,
        "schemaDigest": "f" * 64,
        "compatibilityDigest": "1" * 64,
    }
    source = {
        "operationId": "provider-alpha",
        "checkpoint": "requested",
        "fenceGeneration": 7,
        "tenantId": "tenant-alpha",
        "cellId": "cell-alpha",
        "serviceCredential": "secret-alpha",
        "workerPolicy": {"workerCount": 0, "semantic": False, "media": False},
        "provisionMode": "serve",
        "runtimeTarget": source_target,
        "_providerRecoveryEnvelopes": {"namespace": "opaque"},
    }

    target = recovery.retarget_provision_request(
        source,
        wire_protocol="exomem-cell-provisioner.v2",
        runtime_target=selected,
    )

    assert target == {**source, "runtimeTarget": selected}
    assert recovery.request_targets_selected_runtime(
        target,
        wire_protocol="exomem-cell-provisioner.v2",
        runtime_target=selected,
    )
    assert not recovery.request_targets_selected_runtime(
        source,
        wire_protocol="exomem-cell-provisioner.v2",
        runtime_target=selected,
    )
    assert source["runtimeTarget"] is source_target
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.retarget_provision_request(
            {**source, "releaseVersion": "0.66.0"},
            wire_protocol="exomem-cell-provisioner.v2",
            runtime_target=selected,
        )


def test_retarget_transition_accepts_only_unfinished_recovered_work() -> None:
    recovery = _module()
    now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    pending = recovery.RetargetPreState(
        action="provision",
        state="pending",
        checkpoint="volume-owned",
        error_code=None,
        claim_owner=None,
        claim_token=None,
        claim_expires_at=None,
        has_result=False,
        finalized=False,
        has_recovery_marker=True,
        has_retarget_marker=False,
    )

    assert (
        recovery.retarget_transition_values(pending, now=now)["state"]
        is recovery.OperationState.PENDING
    )
    expired = replace(
        pending,
        state="claimed",
        claim_owner="worker-alpha",
        claim_token="a" * 64,
        claim_expires_at=datetime(2030, 1, 2, 3, 4, 4, tzinfo=UTC),
    )
    assert recovery.retarget_transition_values(expired, now=now)["claim_owner"] is None
    with pytest.raises(recovery.RecoveryRefusal, match="active claim"):
        recovery.retarget_transition_values(
            replace(expired, claim_expires_at=datetime(2030, 1, 2, 3, 4, 6, tzinfo=UTC)),
            now=now,
        )
    for changed in (
        {"checkpoint": "queued"},
        {"has_recovery_marker": False},
        {"has_result": True},
        {"finalized": True},
    ):
        with pytest.raises(recovery.RecoveryRefusal):
            recovery.retarget_transition_values(replace(pending, **changed), now=now)


def test_retarget_marker_is_exact_content_free_and_one_way() -> None:
    recovery = _module()
    marker = recovery.retarget_marker(
        preflight_sha256="a" * 64,
        source_request_sha256="b" * 64,
        target_request_sha256="c" * 64,
        target_runtime_sha256="d" * 64,
        helper_source_sha256="e" * 64,
        claim_generation=4,
        committed_at=datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    assert set(marker) == {
        "schema",
        "preflight_sha256",
        "source_request_sha256",
        "target_request_sha256",
        "target_runtime_sha256",
        "helper_source_sha256",
        "claim_generation",
        "committed_at",
    }
    assert recovery.parse_retarget_marker(marker) == marker
    assert b"secret" not in recovery.canonical_receipt_bytes(marker)
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.parse_retarget_marker({**marker, "tenantId": "tenant-alpha"})


def test_retarget_resume_accepts_only_the_exact_failed_retarget() -> None:
    recovery = _module()
    before = recovery.RetargetResumePreState(
        action="provision",
        state="error",
        checkpoint="failed",
        error_code="PROVISIONER_PROVIDER_METADATA_CONFLICT",
        has_claim=False,
        has_result=False,
        finalized=True,
        has_recovery_marker=True,
        has_retarget_marker=True,
        has_resume_marker=False,
    )

    transition = recovery.retarget_resume_transition_values(before)

    assert transition["state"].value == "pending"
    assert transition["checkpoint"] == "volume-owned"
    assert transition["error_code"] is None
    assert transition["finalized_at"] is None
    for changed in (
        {"state": "pending"},
        {"checkpoint": "retarget-runtime-stopped"},
        {"error_code": None},
        {"has_claim": True},
        {"has_result": True},
        {"finalized": False},
        {"has_recovery_marker": False},
        {"has_retarget_marker": False},
        {"has_resume_marker": True},
    ):
        with pytest.raises(recovery.RecoveryRefusal):
            recovery.retarget_resume_transition_values(replace(before, **changed))


def test_retarget_resume_marker_is_exact_and_bound_to_the_retarget_receipt() -> None:
    recovery = _module()
    marker = recovery.retarget_resume_marker(
        retarget_marker_sha256="a" * 64,
        preflight_sha256="b" * 64,
        helper_source_sha256="c" * 64,
        claim_generation=4,
        committed_at=datetime(2030, 1, 2, tzinfo=UTC),
    )

    assert recovery.parse_retarget_resume_marker(marker) == marker
    assert set(marker) == {
        "schema",
        "retarget_marker_sha256",
        "preflight_sha256",
        "helper_source_sha256",
        "claim_generation",
        "committed_at",
    }
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.parse_retarget_resume_marker({**marker, "cellId": "cell-alpha"})


def test_retarget_retry_accepts_only_the_exact_failed_resumed_retarget() -> None:
    recovery = _module()
    before = recovery.RetargetRetryPreState(
        action="provision",
        state="error",
        checkpoint="failed",
        error_code="PROVISIONER_PROVIDER_METADATA_CONFLICT",
        has_claim=False,
        has_result=False,
        finalized=True,
        has_recovery_marker=True,
        has_retarget_marker=True,
        has_resume_marker=True,
        has_retry_marker=False,
    )

    transition = recovery.retarget_retry_transition_values(before)

    assert transition["state"].value == "pending"
    assert transition["checkpoint"] == "volume-owned"
    assert transition["error_code"] is None
    assert transition["finalized_at"] is None
    for changed in (
        {"state": "pending"},
        {"checkpoint": "retarget-runtime-stopped"},
        {"error_code": None},
        {"has_claim": True},
        {"has_result": True},
        {"finalized": False},
        {"has_recovery_marker": False},
        {"has_retarget_marker": False},
        {"has_resume_marker": False},
        {"has_retry_marker": True},
    ):
        with pytest.raises(recovery.RecoveryRefusal):
            recovery.retarget_retry_transition_values(replace(before, **changed))


def test_retarget_retry_marker_binds_both_prior_recovery_receipts() -> None:
    recovery = _module()
    marker = recovery.retarget_retry_marker(
        retarget_marker_sha256="a" * 64,
        resume_marker_sha256="b" * 64,
        preflight_sha256="c" * 64,
        helper_source_sha256="d" * 64,
        claim_generation=5,
        committed_at=datetime(2030, 1, 3, tzinfo=UTC),
    )

    assert recovery.parse_retarget_retry_marker(marker) == marker
    assert set(marker) == {
        "schema",
        "retarget_marker_sha256",
        "resume_marker_sha256",
        "preflight_sha256",
        "helper_source_sha256",
        "claim_generation",
        "committed_at",
    }
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.parse_retarget_retry_marker({**marker, "cellId": "cell-alpha"})


def test_successor_retarget_accepts_pending_or_exhausted_prior_retarget_only() -> None:
    recovery = _module()
    pending = recovery.RetargetSuccessorPreState(
        action="provision",
        state="pending",
        checkpoint="volume-owned",
        error_code=None,
        has_claim=False,
        has_result=False,
        finalized=False,
        has_recovery_marker=True,
        has_retarget_marker=True,
        has_resume_marker=False,
        has_retry_marker=False,
        has_successor_marker=False,
    )

    assert recovery.retarget_successor_transition_values(pending) == {
        "state": recovery.OperationState.PENDING,
        "checkpoint": "volume-owned",
        "error_code": None,
        "claim_owner": None,
        "claim_token": None,
        "claim_expires_at": None,
        "finalized_at": None,
    }
    capacity_blocked = replace(
        pending,
        checkpoint="capacity-live-observation-mismatch",
    )
    assert recovery.retarget_successor_transition_values(capacity_blocked)["checkpoint"] == (
        "volume-owned"
    )
    exhausted = replace(
        pending,
        state="error",
        checkpoint="failed",
        error_code="PROVISIONER_PROVIDER_METADATA_CONFLICT",
        finalized=True,
        has_resume_marker=True,
        has_retry_marker=True,
    )
    assert recovery.retarget_successor_transition_values(exhausted)["state"] is (
        recovery.OperationState.PENDING
    )
    for changed in (
        {"checkpoint": "requested"},
        {"has_claim": True},
        {"has_result": True},
        {"has_recovery_marker": False},
        {"has_retarget_marker": False},
        {"has_successor_marker": True},
        {"state": "error", "checkpoint": "failed", "error_code": "OTHER", "finalized": True},
        {
            "state": "error",
            "checkpoint": "failed",
            "error_code": "PROVISIONER_PROVIDER_METADATA_CONFLICT",
            "finalized": True,
            "has_resume_marker": True,
            "has_retry_marker": False,
        },
    ):
        with pytest.raises(recovery.RecoveryRefusal):
            recovery.retarget_successor_transition_values(replace(pending, **changed))


def test_successor_retarget_marker_binds_prior_chain_and_both_requests() -> None:
    recovery = _module()
    marker = recovery.retarget_successor_marker(
        prior_receipts_sha256="a" * 64,
        preflight_sha256="b" * 64,
        source_request_sha256="c" * 64,
        target_request_sha256="d" * 64,
        target_runtime_sha256="e" * 64,
        helper_source_sha256="f" * 64,
        claim_generation=6,
        committed_at=datetime(2030, 1, 4, tzinfo=UTC),
    )

    assert recovery.parse_retarget_successor_marker(marker) == marker
    assert set(marker) == {
        "schema",
        "prior_receipts_sha256",
        "preflight_sha256",
        "source_request_sha256",
        "target_request_sha256",
        "target_runtime_sha256",
        "helper_source_sha256",
        "claim_generation",
        "committed_at",
    }
    assert b"secret" not in recovery.canonical_receipt_bytes(marker)
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.parse_retarget_successor_marker({**marker, "tenantId": "tenant-alpha"})


def test_successor_retarget_prior_chain_refuses_reordered_or_unbound_receipts() -> None:
    recovery = _module()
    now = datetime(2030, 1, 4, tzinfo=UTC)
    recovered_at = now - timedelta(minutes=3)
    retargeted_at = now - timedelta(minutes=2)
    resumed_at = now - timedelta(minutes=1)
    recovered = recovery.recovery_marker(
        preflight_sha256="1" * 64,
        helper_source_sha256="2" * 64,
        claim_generation=4,
        committed_at=recovered_at,
    )
    retargeted = recovery.retarget_marker(
        preflight_sha256="3" * 64,
        source_request_sha256="4" * 64,
        target_request_sha256="5" * 64,
        target_runtime_sha256="6" * 64,
        helper_source_sha256="7" * 64,
        claim_generation=4,
        committed_at=retargeted_at,
    )
    resumed = recovery.retarget_resume_marker(
        retarget_marker_sha256=recovery.canonical_sha256(retargeted),
        preflight_sha256="8" * 64,
        helper_source_sha256="9" * 64,
        claim_generation=5,
        committed_at=resumed_at,
    )
    retried = recovery.retarget_retry_marker(
        retarget_marker_sha256=recovery.canonical_sha256(retargeted),
        resume_marker_sha256=recovery.canonical_sha256(resumed),
        preflight_sha256="a" * 64,
        helper_source_sha256="b" * 64,
        claim_generation=6,
        committed_at=now,
    )
    operation = type(
        "PriorOperation",
        (),
        {
            "canonical_request_sha256": "5" * 64,
            "state": recovery.OperationState.PENDING,
            "checkpoint": "capacity-live-observation-mismatch",
            "finalized_at": None,
            "claim_generation": 7,
            "updated_at": now,
            "progress": {
                "_init_retry_recovery_v1": recovered,
                "_runtime_retarget_recovery_v1": retargeted,
                "_runtime_retarget_resume_v1": resumed,
                "_runtime_retarget_retry_v1": retried,
            },
        },
    )()

    digest = recovery.RecoveryService._prior_retarget_receipts_sha256(operation)
    assert len(digest) == 64
    operation.progress["_runtime_retarget_retry_v1"] = {
        **retried,
        "resume_marker_sha256": "c" * 64,
    }
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.RecoveryService._prior_retarget_receipts_sha256(operation)
    operation.progress["_runtime_retarget_retry_v1"] = retried
    operation.claim_generation = 5
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.RecoveryService._prior_retarget_receipts_sha256(operation)
    operation.claim_generation = 6
    operation.progress["_runtime_retarget_retry_v1"] = {
        **retried,
        "claim_generation": 4,
    }
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.RecoveryService._prior_retarget_receipts_sha256(operation)
    operation.progress["_runtime_retarget_retry_v1"] = {
        **retried,
        "committed_at": (resumed_at - timedelta(seconds=1)).isoformat(),
    }
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.RecoveryService._prior_retarget_receipts_sha256(operation)
    operation.progress["_runtime_retarget_retry_v1"] = retried
    operation.updated_at = now - timedelta(seconds=1)
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.RecoveryService._prior_retarget_receipts_sha256(operation)
    operation.updated_at = now
    operation.claim_generation = 6
    with pytest.raises(recovery.RecoveryRefusal):
        recovery.RecoveryService._prior_retarget_receipts_sha256(operation)


def test_database_stays_at_0006_without_recovery_receipt_table() -> None:
    from exomem_provisioner.database import DATABASE_REVISION
    from exomem_provisioner.models import Base

    assert DATABASE_REVISION == "0006_operation_wire_protocol"
    assert "operation_recovery_receipts" not in Base.metadata.tables


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


def test_live_init_observation_allows_absent_or_complete_job_but_refuses_failed_or_terminating() -> (
    None
):
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
    assert (
        recovery.validate_live_observation(
            replace(absent, init_job_present=True, init_complete=True)
        )
        is None
    )
    for changed in (
        {"init_job_present": True},
        {"init_job_present": True, "init_failed_only": True},
        {"pvc_bound": False},
        {"terminating": True},
        {"runtime_admitted": True},
        {"routes_present": 1},
    ):
        with pytest.raises(recovery.RecoveryRefusal, match="live recovery preflight failed"):
            recovery.validate_live_observation(replace(absent, **changed))


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


def test_final_proof_requires_the_exact_normal_provision_completion_shape() -> None:
    recovery = _module()
    operation = type(
        "OperationProof",
        (),
        {
            "state": recovery.OperationState.FINAL,
            "checkpoint": "complete",
            "result_ciphertext": "encrypted-result",
            "result_redacted": {
                "completed": True,
                "fields": ["privateEndpoint", "providerRef"],
            },
            "finalized_at": datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC),
        },
    )()

    assert recovery.RecoveryService._final_proof(operation) is True
    operation.result_redacted = {"completed": True, "fields": []}
    assert recovery.RecoveryService._final_proof(operation) is False


@pytest.mark.asyncio
async def test_production_observer_compares_live_volume_fields_not_absent_hcloud_copy() -> None:
    recovery = _module()
    from exomem_provisioner.lifecycle import OpaqueProviderMetadata, RecordedVolume
    from exomem_provisioner.models import ResourceKind

    metadata = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "provider-alpha", 7)
    expected = RecordedVolume(
        "42", "pv-alpha", "fsn1", metadata, "hcloud-envelope", "pv-envelope", "pvc-envelope"
    )
    live = RecordedVolume("42", "pv-alpha", "fsn1", metadata, "", "pv-envelope", "pvc-envelope")
    references = {
        ResourceKind.KUBERNETES_NAMESPACE: metadata.resource_name,
        ResourceKind.HELM_RELEASE: metadata.resource_name,
        ResourceKind.PVC: metadata.resource_name + "-data",
        ResourceKind.VOLUME: expected.recoverable_reference(),
    }

    class Codec:
        def decrypt_json(self, ciphertext, *, purpose):
            return {"reference": references[ResourceKind(ciphertext)]}

    class Registry:
        async def inspect(self, current, owned):
            return type(
                "Snapshot",
                (),
                {
                    "namespace": True,
                    "release": True,
                    "init_job_present": False,
                    "init_complete": False,
                    "init_failed": False,
                    "serving": False,
                    "runtime_admitted": False,
                    "routes": (False, False),
                },
            )()

        async def authenticate_recovery_record(self, current):
            return "a" * 64

    class Volumes:
        async def observe_recovery_bound_volume(self, current):
            return type("Bound", (), {"recorded": live, "stability_digest": "b" * 64})()

    class HCloud:
        async def verify_recovery_volume(self, handle, current, location, envelope):
            return (handle, current, location, envelope) == (
                "42",
                metadata,
                "fsn1",
                "hcloud-envelope",
            )

    class Cell:
        async def volume_claim_bound(self, current):
            return current == metadata

    observer = recovery._ProductionRecoveryObserver(
        Registry(), Cell(), Volumes(), HCloud(), "fsn1", Codec()
    )
    operation = type(
        "Operation",
        (),
        {
            "cell_id": "cell-alpha",
            "tenant_id": "tenant-alpha",
            "external_operation_id": "provider-alpha",
            "fence_generation": 7,
        },
    )()
    resources = tuple(
        type(
            "Resource",
            (),
            {"kind": kind, "reference_ciphertext": kind.value, "operation_id": "internal"},
        )()
        for kind in references
    )

    observed = await observer.observe(operation, resources)
    assert observed.volume_present is True


def test_main_converts_refusals_to_content_free_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recovery = _module()

    async def refuse(_: object) -> dict[str, object]:
        raise recovery.RecoveryRefusal("preflight-failed")

    monkeypatch.setattr(recovery, "_run", refuse)

    assert recovery.main(["preflight", "--stdin"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "refusal": "preflight-failed",
        "status": "refused",
    }
