from __future__ import annotations

import importlib.util
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from exomem import __version__ as runtime_version
from exomem.governance import (
    authorization_custody as runtime_custody,
)
from exomem.governance import (
    authorization_session_lifecycle,
    policy,
    schema_v4,
    store,
)

from exomem_provisioner import authorization_membership, governance_migration_membership
from exomem_provisioner.governance_migration_job import MigrationJobEvidence, MigrationJobRequest
from exomem_provisioner.governance_readiness import verify_governance_readiness
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata

NOW = 1_900_000_000
CELL_ID = "cell-alpha"
VAULT_ID = "tenant-alpha"
SOFTWARE_VERSION = runtime_version
ENVELOPE = "signed-authorization-session-secret"
METADATA = OpaqueProviderMetadata(VAULT_ID, CELL_ID, "operation-alpha", 7)
REPLICA_ID = METADATA.resource_name + "-0"
TARGET = {
    "activation_store_id": "activation-alpha",
    "activation_epoch": 9,
    "activation_state_digest": "a" * 64,
}

def test_governance_readiness_verifier_has_a_dedicated_provisioner_boundary() -> None:
    assert importlib.util.find_spec("exomem_provisioner.governance_readiness") is not None


def _identity(schema: int) -> dict[str, object]:
    return {
        "expected_cell_id": CELL_ID,
        "expected_logical_vault_id": VAULT_ID,
        "expected_replica_id": REPLICA_ID,
        "expected_software_version": SOFTWARE_VERSION,
        "expected_schema_version": schema,
        "expected_recovery_envelope": ENVELOPE,
    }


def _serving_bundle(target: dict[str, object] = TARGET):
    initial = authorization_membership.build_initial_hosted_authorization_bundle(
        cell_id=CELL_ID,
        logical_vault_id=VAULT_ID,
        replica_id=REPLICA_ID,
        software_version=SOFTWARE_VERSION,
        schema_version=3,
        recovery_envelope=ENVELOPE,
        now=NOW,
        entropy=lambda length: bytes(range(length)),
    )
    drained = authorization_membership.transition_hosted_authorization_bundle(
        initial.files,
        **_identity(3),
        target_state="DRAINING",
        target_no_in_flight=True,
        now=NOW + 1,
    )
    enrolled = authorization_membership.enroll_hosted_governance_bundle(
        drained.files,
        **_identity(3),
        **target,
        now=NOW + 2,
    )
    request = MigrationJobRequest(
        metadata=METADATA,
        vault_id=VAULT_ID,
        pvc_uid="pvc-alpha",
        runtime_image="ghcr.io/example/runtime@sha256:" + "b" * 64,
        custody_revision=enrolled.revision,
        phase="commit",
        source_store_digest="c" * 64,
        plan_digest="d" * 64,
    )
    terminal = {
        "artifact": "exomem-hosted-governance-migration",
        "schemaVersion": 1,
        "phase": "commit",
        "requestSha256": request.sha256,
        "custodyRevision": request.custody_revision,
        "actualSchema": 4,
        "membershipSchema": 3,
        "governanceEnrolled": True,
        "sourceStoreDigest": "c" * 64,
        "planDigest": "d" * 64,
        "backupReference": "exomem-governance-v3-backup://sha256/" + "e" * 64,
        "activationStoreId": target["activation_store_id"],
        "activationEpoch": target["activation_epoch"],
        "activationStateDigest": target["activation_state_digest"],
        "replayed": False,
    }
    migrated = governance_migration_membership.complete_governance_migration_membership(
        enrolled.files,
        request=request,
        evidence=MigrationJobEvidence(
            "job-alpha",
            "pod-alpha",
            json.dumps(terminal, sort_keys=True, separators=(",", ":")).encode(),
        ),
        recovery_envelope=ENVELOPE,
        now=NOW + 3,
    )
    return authorization_membership.transition_hosted_authorization_bundle(
        migrated.files,
        **_identity(4),
        target_state="SERVING",
        target_no_in_flight=False,
        now=NOW + 4,
    )


def _proof(bundle) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "actualSchema": 4,
        "cellId": CELL_ID,
        "vaultId": VAULT_ID,
        "replicaId": REPLICA_ID,
        "softwareVersion": SOFTWARE_VERSION,
        "governanceEnrolled": True,
        "activationStoreId": bundle.activation_store_id,
        "activationEpoch": bundle.activation_epoch,
        "activationStateDigest": bundle.activation_state_digest,
        "custodyRevision": bundle.revision,
        "membershipEpoch": bundle.epoch,
        "membershipDigest": bundle.membership_digest,
        "storeAgreement": True,
    }


def test_verifier_accepts_only_the_matching_enrolled_schema_four_serving_bundle() -> None:
    bundle = _serving_bundle()

    assert (
        verify_governance_readiness(
            _proof(bundle),
            bundle=bundle,
            cell_id=CELL_ID,
            vault_id=VAULT_ID,
            replica_id=REPLICA_ID,
            software_version=SOFTWARE_VERSION,
        )
        is None
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda proof: proof | {"extra": True},
        lambda proof: {key: value for key, value in proof.items() if key != "storeAgreement"},
        lambda proof: {**proof, "schemaVersion": 2},
        lambda proof: {**proof, "actualSchema": 3},
        lambda proof: {**proof, "cellId": "other-cell"},
        lambda proof: {**proof, "vaultId": "other-vault"},
        lambda proof: {**proof, "replicaId": "other-replica"},
        lambda proof: {**proof, "softwareVersion": "other-version"},
        lambda proof: {**proof, "governanceEnrolled": False},
        lambda proof: {**proof, "activationStoreId": "other-activation"},
        lambda proof: {**proof, "activationEpoch": 10},
        lambda proof: {**proof, "activationStateDigest": "f" * 64},
        lambda proof: {**proof, "custodyRevision": "f" * 64},
        lambda proof: {**proof, "membershipEpoch": 10},
        lambda proof: {**proof, "membershipDigest": "f" * 64},
        lambda proof: {**proof, "storeAgreement": False},
        lambda proof: {**proof, "cellId": ""},
        lambda proof: {**proof, "vaultId": "bad\nvalue"},
        lambda proof: {**proof, "replicaId": "\U0010ffff" * 513},
        lambda proof: {**proof, "softwareVersion": ""},
        lambda proof: {**proof, "activationStoreId": ""},
        lambda proof: {**proof, "activationStateDigest": "not-a-digest"},
        lambda proof: {**proof, "custodyRevision": "not-a-digest"},
        lambda proof: {**proof, "membershipDigest": "not-a-digest"},
    ],
)
def test_verifier_refuses_every_non_exact_private_proof(mutate) -> None:
    bundle = _serving_bundle()

    with pytest.raises(MetadataConflict) as raised:
        verify_governance_readiness(
            mutate(_proof(bundle)),
            bundle=bundle,
            cell_id=CELL_ID,
            vault_id=VAULT_ID,
            replica_id=REPLICA_ID,
            software_version=SOFTWARE_VERSION,
        )

    assert str(raised.value) == "governance readiness is unavailable"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schemaVersion", True),
        ("actualSchema", True),
        ("activationEpoch", True),
        ("membershipEpoch", True),
        ("schemaVersion", 0),
        ("actualSchema", -1),
        ("activationEpoch", 0),
        ("membershipEpoch", -1),
        ("schemaVersion", 1 << 63),
        ("actualSchema", 1 << 63),
        ("activationEpoch", 1 << 63),
        ("membershipEpoch", 1 << 63),
    ],
)
def test_verifier_refuses_non_integer_or_unbounded_private_counters(
    field: str,
    value: object,
) -> None:
    bundle = _serving_bundle()

    with pytest.raises(MetadataConflict):
        verify_governance_readiness(
            _proof(bundle) | {field: value},
            bundle=bundle,
            cell_id=CELL_ID,
            vault_id=VAULT_ID,
            replica_id=REPLICA_ID,
            software_version=SOFTWARE_VERSION,
        )


@pytest.mark.parametrize("proof", [None, [], "proof", 1, True])
def test_verifier_refuses_non_object_private_proof(proof: object) -> None:
    bundle = _serving_bundle()

    with pytest.raises(MetadataConflict):
        verify_governance_readiness(
            proof,
            bundle=bundle,
            cell_id=CELL_ID,
            vault_id=VAULT_ID,
            replica_id=REPLICA_ID,
            software_version=SOFTWARE_VERSION,
        )


@pytest.mark.parametrize(
    "bundle_change,identity_change",
    [
        (lambda bundle: replace(bundle, revision="f" * 64), {}),
        (lambda bundle: replace(bundle, membership_schema_version=3), {}),
        (lambda bundle: replace(bundle, replica_state="DRAINING"), {}),
        (lambda bundle: replace(bundle, governance_enrolled=False), {}),
        (lambda bundle: replace(bundle, issuance_stopped=True), {}),
        (lambda bundle: replace(bundle, no_in_flight=True), {}),
        (lambda bundle: replace(bundle, software_version="other-version"), {}),
        (lambda bundle: replace(bundle, activation_store_id="other-activation"), {}),
        (lambda bundle: replace(bundle, activation_epoch=10), {}),
        (lambda bundle: replace(bundle, activation_state_digest="f" * 64), {}),
        (lambda bundle: replace(bundle, epoch=10), {}),
        (lambda bundle: replace(bundle, membership_digest="f" * 64), {}),
        (lambda bundle: replace(bundle, revision="not-a-digest"), {}),
        (lambda bundle: replace(bundle, control=b"{}"), {}),
        (lambda bundle: replace(bundle, membership=b"{}"), {}),
        (lambda bundle: bundle, {"cell_id": "other-cell"}),
        (lambda bundle: bundle, {"vault_id": "other-vault"}),
        (lambda bundle: bundle, {"replica_id": "other-replica"}),
        (lambda bundle: bundle, {"software_version": "other-version"}),
    ],
)
def test_verifier_refuses_any_bundle_or_identity_mismatch(bundle_change, identity_change) -> None:
    bundle = _serving_bundle()

    with pytest.raises(MetadataConflict):
        verify_governance_readiness(
            _proof(bundle),
            bundle=bundle_change(bundle),
            cell_id=identity_change.get("cell_id", CELL_ID),
            vault_id=identity_change.get("vault_id", VAULT_ID),
            replica_id=identity_change.get("replica_id", REPLICA_ID),
            software_version=identity_change.get("software_version", SOFTWARE_VERSION),
        )


def _migrate_runtime_store(vault: Path) -> schema_v4.MigrationResult:
    documents = {
        "scopes/migration.yaml": (
            b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths:\n  - Notes/**\n"
        )
    }
    compiled = policy.compile_documents(documents)
    database_path = store.sidecar_path(vault)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        store._migrate(connection)
        return schema_v4.migrate_v3_connection(
            connection,
            schema_v4.MigrationSeed(
                activation_store_id=TARGET["activation_store_id"],
                logical_vault_id=VAULT_ID,
                activation_epoch=TARGET["activation_epoch"],
                policy=schema_v4.PolicyGenerationSeed(
                    generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    source_documents=tuple(documents.items()),
                    source_fingerprint=compiled.fingerprint,
                    conflict_digest="2" * 64,
                    compiled_policy=policy.canonical_compiled_bytes(compiled),
                    policy_fingerprint=compiled.fingerprint,
                    compiler_schema_version=1,
                    projector_schema_version=1,
                    predecessor_generation_id=None,
                    authoring_event_id="event-migration-policy",
                    receipt_event_id="receipt-migration-policy",
                    created_at=NOW,
                ),
                catalog=schema_v4.CatalogGenerationSeed(
                    catalog_generation=7,
                    descriptor=b'{"artifacts":[]}',
                    artifact_count=0,
                    created_at=NOW,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="projection-namespace-7",
                    evidence=b'{"ready":true}',
                    ready_at=NOW,
                ),
                migrated_at=NOW,
            ),
        )
    finally:
        connection.close()


def test_runtime_proof_round_trips_to_the_provisioner_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    state_root = tmp_path / "state"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(state_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root / "xdg"))
    migration = _migrate_runtime_store(vault)
    bundle = _serving_bundle(
        {
            "activation_store_id": TARGET["activation_store_id"],
            "activation_epoch": TARGET["activation_epoch"],
            "activation_state_digest": migration.activation_state_digest,
        }
    )
    custody_root = tmp_path / "custody"
    custody_root.mkdir(mode=0o700)
    paths = {
        "HOSTED_KEYRING_FILE": custody_root / "keyring.json",
        "HOSTED_CONTROL_FILE": custody_root / "control.json",
        "HOSTED_MEMBERSHIP_FILE": custody_root / "serving-membership.json",
    }
    for name, path in paths.items():
        monkeypatch.setattr(runtime_custody, name, path)
    paths["HOSTED_KEYRING_FILE"].write_bytes(bundle.keyring)
    paths["HOSTED_CONTROL_FILE"].write_bytes(bundle.control)
    paths["HOSTED_MEMBERSHIP_FILE"].write_bytes(bundle.membership)
    for path in paths.values():
        path.chmod(0o600)
    monkeypatch.setenv(runtime_custody.KEYRING_FILE_ENV, str(paths["HOSTED_KEYRING_FILE"]))
    monkeypatch.setenv(runtime_custody.CONTROL_FILE_ENV, str(paths["HOSTED_CONTROL_FILE"]))
    monkeypatch.setenv(runtime_custody.MEMBERSHIP_FILE_ENV, str(paths["HOSTED_MEMBERSHIP_FILE"]))
    monkeypatch.setenv(runtime_custody.REPLICA_ID_ENV, REPLICA_ID)
    custody = runtime_custody.load_authorization_custody(vault, now=NOW + 4)
    connection = store.open_authorization_session_connection(vault)
    try:
        authorization_session_lifecycle._ready_custody(connection, custody, now=NOW + 4)
    finally:
        connection.close()

    readiness = authorization_session_lifecycle.hosted_serving_membership_readiness(
        vault,
        expected_cell_id=CELL_ID,
        expected_logical_vault_id=VAULT_ID,
        expected_replica_id=REPLICA_ID,
        now=NOW + 4,
    )

    assert readiness.governance is not None
    verify_governance_readiness(
        readiness.governance.as_private_dict(),
        bundle=bundle,
        cell_id=CELL_ID,
        vault_id=VAULT_ID,
        replica_id=REPLICA_ID,
        software_version=SOFTWARE_VERSION,
    )
