from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime
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
    for mode in ("reopen", "inspect", "verify-recovery"):
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
