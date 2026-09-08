from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace

import pytest
from exomem.governance import authorization_custody, authorization_serving_membership

from exomem_provisioner import authorization_membership as membership
from exomem_provisioner.governance_migration_job import MigrationJobEvidence, MigrationJobRequest
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata

NOW = 1_900_000_000
ENVELOPE = "signed-authorization-session-secret"
METADATA = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7)
TARGET = {
    "activation_store_id": "activation-alpha",
    "activation_epoch": 9,
    "activation_state_digest": "a" * 64,
}


def _module():
    from exomem_provisioner import governance_migration_membership

    return governance_migration_membership


def _identity(schema):
    return {
        "expected_cell_id": METADATA.subject_id,
        "expected_logical_vault_id": METADATA.tenant_id,
        "expected_replica_id": METADATA.resource_name + "-0",
        "expected_software_version": None,
        "expected_schema_version": schema,
        "expected_recovery_envelope": ENVELOPE,
    }


def _source(schema, *, drained=True, enrolled=False):
    bundle = membership.build_initial_hosted_authorization_bundle(
        cell_id=METADATA.subject_id,
        logical_vault_id=METADATA.tenant_id,
        replica_id=METADATA.resource_name + "-0",
        software_version="0.48.0",
        schema_version=schema,
        recovery_envelope=ENVELOPE,
        now=NOW,
        entropy=lambda length: bytes(range(length)),
    )
    if drained:
        bundle = membership.transition_hosted_authorization_bundle(
            bundle.files,
            **_identity(schema),
            target_state="DRAINING",
            target_no_in_flight=True,
            now=NOW + 1,
        )
    if enrolled:
        bundle = membership.enroll_hosted_governance_bundle(
            bundle.files, **_identity(schema), **TARGET, now=NOW + 2
        )
    return bundle


def _proof(source, phase):
    request = MigrationJobRequest(
        metadata=METADATA,
        vault_id=METADATA.tenant_id,
        pvc_uid="pvc-alpha",
        runtime_image="ghcr.io/example/runtime@sha256:" + "b" * 64,
        custody_revision=source.revision,
        phase=phase,
        source_store_digest="c" * 64 if phase == "commit" else None,
        plan_digest="d" * 64 if phase == "commit" else None,
    )
    terminal = {
        "artifact": "exomem-hosted-governance-migration",
        "schemaVersion": 1,
        "phase": phase,
        "requestSha256": request.sha256,
        "custodyRevision": request.custody_revision,
        "actualSchema": 4 if phase == "commit" else 3,
        "membershipSchema": source.membership_schema_version,
        "governanceEnrolled": source.governance_enrolled,
        "sourceStoreDigest": "c" * 64,
    }
    if phase == "commit":
        terminal.update(
            planDigest=request.plan_digest,
            backupReference="exomem-governance-v3-backup://sha256/" + "e" * 64,
            activationStoreId=TARGET["activation_store_id"],
            activationEpoch=TARGET["activation_epoch"],
            activationStateDigest=TARGET["activation_state_digest"],
            replayed=False,
        )
    return request, MigrationJobEvidence("job-alpha", "pod-alpha", _raw(terminal))


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _run(source, phase, *, now=NOW + 3, request=None, evidence=None, files=None):
    defaults = _proof(source, phase)
    function = (
        _module().repair_governance_schema_claim
        if phase == "inspect"
        else _module().complete_governance_migration_membership
    )
    return function(
        source.files if files is None else files,
        request=defaults[0] if request is None else request,
        evidence=defaults[1] if evidence is None else evidence,
        recovery_envelope=ENVELOPE,
        now=now,
    )


def _runtime_verify(bundle, *, now):
    keyring = authorization_custody.parse_keyring(bundle.keyring)
    control = authorization_custody.parse_control_record(bundle.control, keyring=keyring, now=now)
    record = authorization_serving_membership.parse_serving_membership(
        bundle.membership,
        verifier_keys={key.key_id: key.key for key in keyring.accepted_keys},
        now=now,
        expected_cell_id=METADATA.subject_id,
        expected_logical_vault_id=METADATA.tenant_id,
        expected_epoch=control.serving_membership_epoch,
        expected_digest=control.serving_membership_digest,
    )
    return control, record


@pytest.mark.parametrize("phase", ["inspect", "commit"])
def test_migration_schema_successor_preserves_every_non_lineage_field(phase):
    source = _source(4 if phase == "inspect" else 3, enrolled=phase == "commit")
    successor = _run(source, phase)
    target_schema = 3 if phase == "inspect" else 4
    control, record = _runtime_verify(successor, now=NOW + 3)
    assert successor.membership_schema_version == record.replicas[0].schema_version == target_schema
    assert successor.epoch == record.epoch == source.epoch + 1
    assert record.previous_epoch_digest == source.membership_digest
    assert successor.keyring == source.keyring
    assert successor.expires_at == source.expires_at
    assert successor.governance_enrolled is source.governance_enrolled
    assert successor.revision != source.revision
    assert successor.replica_state == "DRAINING"
    assert successor.issuance_stopped is successor.no_in_flight is True
    before_control, after_control = json.loads(source.control), json.loads(successor.control)
    assert {key for key in before_control if before_control[key] != after_control[key]} == {
        "serving_membership_epoch",
        "serving_membership_digest",
        "mac",
    }
    before_member, after_member = json.loads(source.membership), json.loads(successor.membership)
    assert {key for key in before_member if before_member[key] != after_member[key]} == {
        "epoch",
        "previous_epoch_digest",
        "replicas",
        "mac",
    }
    before_replica, after_replica = before_member["replicas"][0], after_member["replicas"][0]
    assert {key for key in before_replica if before_replica[key] != after_replica[key]} == {
        "epoch",
        "schema_version",
        "mac",
    }
    assert control.activation_store_id == source.activation_store_id
    assert (
        membership.inspect_hosted_authorization_bundle(
            successor.files, **_identity(target_schema), now=NOW + 3
        )
        == successor
    )


@pytest.mark.parametrize("phase", ["inspect", "commit"])
def test_current_request_replay_is_identical_but_old_proof_cannot_advance_a_successor(phase):
    source = _source(4 if phase == "inspect" else 3, enrolled=phase == "commit")
    request, evidence = _proof(source, phase)
    successor = _run(source, phase)
    assert _run(successor, phase) == successor
    with pytest.raises(MetadataConflict):
        _run(successor, phase, request=request, evidence=evidence)


def test_completion_preserves_expired_window_without_granting_serving_authority():
    source = _source(3, enrolled=True)
    successor = _run(source, "commit", now=source.expires_at + 30)
    assert successor.expires_at == source.expires_at
    assert successor.replica_state == "DRAINING"
    with pytest.raises(MetadataConflict):
        membership.inspect_hosted_authorization_bundle(
            successor.files, **_identity(4), now=source.expires_at + 30
        )
    # A migration-only same-window successor is not permission for ordinary
    # readers to accept expired custody. At its original time the signatures agree.
    _runtime_verify(successor, now=NOW + 3)


@pytest.mark.parametrize("phase", ["inspect", "commit"])
@pytest.mark.parametrize("case", ["serving", "future", "key-expired", "missing-file", "tamper"])
def test_schema_helpers_refuse_unproven_custody(phase, case):
    source = _source(4 if phase == "inspect" else 3, enrolled=phase == "commit")
    files = source.files
    now = NOW + 3
    if case == "serving":
        source = membership.transition_hosted_authorization_bundle(
            files,
            **_identity(source.membership_schema_version),
            target_state="SERVING",
            target_no_in_flight=False,
            now=now,
        )
        files = source.files
    elif case == "future":
        now = NOW
    elif case == "key-expired":
        now = NOW + 367 * 24 * 3600
    elif case == "missing-file":
        files.pop("keyring.json")
    elif case == "tamper":
        files["control.json"] = files["control.json"].replace(
            b'"attachment_epoch":1', b'"attachment_epoch":2'
        )
    with pytest.raises(MetadataConflict):
        _run(source, phase, files=files, now=now)


def test_repair_refuses_expired_or_enrolled_custody_and_completion_requires_enrollment():
    source = _source(4)
    with pytest.raises(MetadataConflict):
        _run(source, "inspect", now=source.expires_at)
    with pytest.raises(MetadataConflict):
        _run(_source(3, enrolled=True), "inspect")
    with pytest.raises(MetadataConflict):
        _run(_source(3), "commit")


@pytest.mark.parametrize("phase", ["inspect", "commit"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("requestSha256", "f" * 64),
        ("custodyRevision", "f" * 64),
        ("phase", "prepare"),
        ("actualSchema", 2),
        ("actualSchema", True),
        ("membershipSchema", 2),
        ("governanceEnrolled", "true"),
        ("sourceStoreDigest", "broken"),
        ("extra", "not-allowed"),
    ],
)
def test_schema_helpers_revalidate_closed_request_bound_terminal(phase, field, value):
    source = _source(4 if phase == "inspect" else 3, enrolled=phase == "commit")
    request, evidence = _proof(source, phase)
    forged = replace(evidence, _terminal=_raw({**evidence.terminal, field: value}))
    with pytest.raises(MetadataConflict):
        _run(source, phase, request=request, evidence=forged)


@pytest.mark.parametrize(
    "field,value",
    [
        ("activationStoreId", "foreign-store"),
        ("activationEpoch", 10),
        ("activationStateDigest", "f" * 64),
        ("membershipSchema", 4),
        ("planDigest", "f" * 64),
    ],
)
def test_completion_refuses_a_validly_shaped_but_different_target_or_membership(field, value):
    source = _source(3, enrolled=True)
    request, evidence = _proof(source, "commit")
    forged = replace(evidence, _terminal=_raw({**evidence.terminal, field: value}))
    with pytest.raises(MetadataConflict):
        _run(source, "commit", request=request, evidence=forged)


@pytest.mark.parametrize("phase", ["inspect", "commit"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("pvc_uid", "foreign-pvc"),
        ("runtime_image", "ghcr.io/example/runtime@sha256:" + "f" * 64),
        ("metadata", replace(METADATA, operation_id="another-operation")),
        ("metadata", replace(METADATA, fence_generation=8)),
    ],
)
def test_changed_request_identity_cannot_reuse_old_job_evidence(phase, field, value):
    source = _source(4 if phase == "inspect" else 3, enrolled=phase == "commit")
    request, evidence = _proof(source, phase)
    with pytest.raises(MetadataConflict):
        _run(source, phase, request=replace(request, **{field: value}), evidence=evidence)


@pytest.mark.parametrize("phase", ["inspect", "commit"])
def test_schema_transform_uses_only_once_read_authenticated_mapping_bytes(phase):
    source = _source(4 if phase == "inspect" else 3, enrolled=phase == "commit")
    values = source.files

    class Once(Mapping):
        def __init__(self):
            self.reads = []

        def __iter__(self):
            return iter(values)

        def __len__(self):
            return len(values)

        def __getitem__(self, name):
            assert name not in self.reads
            self.reads.append(name)
            return values[name]

    files = Once()
    successor = _run(source, phase, files=files)
    assert sorted(files.reads) == sorted(values)
    assert (
        successor.revision
        == hashlib.sha256(successor.keyring + successor.control + successor.membership).hexdigest()
    )
