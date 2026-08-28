from __future__ import annotations

import hashlib

import pytest
from exomem.governance import authorization_custody, authorization_serving_membership

from exomem_provisioner.authorization_membership import (
    build_initial_hosted_authorization_bundle,
    inspect_hosted_authorization_bundle,
    transition_hosted_authorization_bundle,
)
from exomem_provisioner.lifecycle import MetadataConflict


def _entropy(length: int) -> bytes:
    assert length == 32
    return bytes(range(32))


def _runtime_membership(bundle, *, now: int):
    keyring = authorization_custody.parse_keyring(bundle.keyring)
    control = authorization_custody.parse_control_record(
        bundle.control,
        keyring=keyring,
        now=now,
    )
    record = authorization_serving_membership.parse_serving_membership(
        bundle.membership,
        verifier_keys={item.key_id: item.key for item in keyring.accepted_keys},
        now=now,
        expected_cell_id="cell-alpha",
        expected_logical_vault_id="tenant-alpha",
        expected_epoch=control.serving_membership_epoch,
        expected_digest=control.serving_membership_digest,
    )
    return control, record


def test_initial_hosted_bundle_is_byte_compatible_with_the_runtime_verifier() -> None:
    now = 1_900_000_000
    bundle = build_initial_hosted_authorization_bundle(
        cell_id="cell-alpha",
        logical_vault_id="tenant-alpha",
        replica_id="exo-0123456789abcdef0123-0",
        software_version="0.48.0",
        schema_version=4,
        recovery_envelope="signed-authorization-session-secret",
        now=now,
        entropy=_entropy,
    )

    keyring = authorization_custody.parse_keyring(bundle.keyring)
    control = authorization_custody.parse_control_record(
        bundle.control,
        keyring=keyring,
        now=now,
    )
    membership = authorization_serving_membership.parse_serving_membership(
        bundle.membership,
        verifier_keys={item.key_id: item.key for item in keyring.accepted_keys},
        now=now,
        expected_cell_id="cell-alpha",
        expected_logical_vault_id="tenant-alpha",
        expected_epoch=control.serving_membership_epoch,
        expected_digest=control.serving_membership_digest,
    )

    assert control.governance_enrolled is False
    assert (
        control.activation_store_id,
        control.activation_epoch,
        control.activation_state_digest,
    ) == (None, None, None)
    assert control.registry_attachment_id.startswith("hosted-attachment-v1-")
    assert membership.replicas[0].replica_id == "exo-0123456789abcdef0123-0"
    assert membership.replicas[0].software_version == "0.48.0"
    assert membership.replicas[0].schema_version == 4
    assert (
        bundle.revision
        == hashlib.sha256(bundle.keyring + bundle.control + bundle.membership).hexdigest()
    )


def test_existing_hosted_bundle_is_exactly_bound_and_never_regenerated_on_retry() -> None:
    now = 1_900_000_000
    bundle = build_initial_hosted_authorization_bundle(
        cell_id="cell-alpha",
        logical_vault_id="tenant-alpha",
        replica_id="exo-0123456789abcdef0123-0",
        software_version="0.48.0",
        schema_version=4,
        recovery_envelope="signed-authorization-session-secret",
        now=now,
        entropy=_entropy,
    )

    inspected = inspect_hosted_authorization_bundle(
        {
            "keyring.json": bundle.keyring,
            "control.json": bundle.control,
            "serving-membership.json": bundle.membership,
        },
        expected_cell_id="cell-alpha",
        expected_logical_vault_id="tenant-alpha",
        expected_replica_id="exo-0123456789abcdef0123-0",
        expected_software_version="0.48.0",
        expected_schema_version=4,
        expected_recovery_envelope="signed-authorization-session-secret",
        now=now + 30,
    )

    assert inspected == bundle


def test_drain_and_current_epoch_rejoin_are_authenticated_runtime_successors() -> None:
    now = 1_900_000_000
    initial = build_initial_hosted_authorization_bundle(
        cell_id="cell-alpha",
        logical_vault_id="tenant-alpha",
        replica_id="exo-0123456789abcdef0123-0",
        software_version="0.48.0",
        schema_version=4,
        recovery_envelope="signed-authorization-session-secret",
        now=now,
        entropy=_entropy,
    )
    _initial_control, initial_record = _runtime_membership(initial, now=now)

    drained = transition_hosted_authorization_bundle(
        initial.files,
        expected_cell_id="cell-alpha",
        expected_logical_vault_id="tenant-alpha",
        expected_replica_id="exo-0123456789abcdef0123-0",
        expected_software_version="0.48.0",
        expected_schema_version=4,
        expected_recovery_envelope="signed-authorization-session-secret",
        target_state="DRAINING",
        target_no_in_flight=True,
        now=now + 30,
    )
    _drained_control, drained_record = _runtime_membership(drained, now=now + 30)
    authorization_serving_membership.validate_membership_successor(
        initial_record,
        drained_record,
        now=now + 30,
    )
    assert (drained.epoch, drained.replica_state, drained.issuance_stopped) == (
        2,
        "DRAINING",
        True,
    )
    assert drained.no_in_flight is True
    assert (
        transition_hosted_authorization_bundle(
            drained.files,
            expected_cell_id="cell-alpha",
            expected_logical_vault_id="tenant-alpha",
            expected_replica_id="exo-0123456789abcdef0123-0",
            expected_software_version="0.48.0",
            expected_schema_version=4,
            expected_recovery_envelope="signed-authorization-session-secret",
            target_state="DRAINING",
            target_no_in_flight=True,
            now=now + 31,
        )
        == drained
    )

    rejoined = transition_hosted_authorization_bundle(
        drained.files,
        expected_cell_id="cell-alpha",
        expected_logical_vault_id="tenant-alpha",
        expected_replica_id="exo-0123456789abcdef0123-0",
        expected_software_version="0.48.0",
        expected_schema_version=4,
        expected_recovery_envelope="signed-authorization-session-secret",
        target_state="SERVING",
        target_no_in_flight=False,
        target_software_version="0.49.0",
        now=now + 4_000,
    )
    _rejoined_control, rejoined_record = _runtime_membership(rejoined, now=now + 4_000)
    authorization_serving_membership.validate_membership_successor(
        drained_record,
        rejoined_record,
        now=now + 4_000,
    )
    assert (rejoined.epoch, rejoined.replica_state, rejoined.no_in_flight) == (
        3,
        "SERVING",
        False,
    )
    assert rejoined_record.replicas[0].software_version == "0.49.0"


def test_membership_transition_never_infers_drain_or_renews_stale_serving_state() -> None:
    now = 1_900_000_000
    initial = build_initial_hosted_authorization_bundle(
        cell_id="cell-alpha",
        logical_vault_id="tenant-alpha",
        replica_id="exo-0123456789abcdef0123-0",
        software_version="0.48.0",
        schema_version=4,
        recovery_envelope="signed-authorization-session-secret",
        now=now,
        entropy=_entropy,
    )
    common = {
        "expected_cell_id": "cell-alpha",
        "expected_logical_vault_id": "tenant-alpha",
        "expected_replica_id": "exo-0123456789abcdef0123-0",
        "expected_software_version": "0.48.0",
        "expected_schema_version": 4,
        "expected_recovery_envelope": "signed-authorization-session-secret",
    }
    with pytest.raises(MetadataConflict, match="drain acknowledgement"):
        transition_hosted_authorization_bundle(
            initial.files,
            **common,
            target_state="DRAINING",
            target_no_in_flight=False,
            now=now + 30,
        )
    with pytest.raises(MetadataConflict, match="stale"):
        transition_hosted_authorization_bundle(
            initial.files,
            **common,
            target_state="SERVING",
            target_no_in_flight=False,
            now=now + 4_000,
            renew=True,
        )


def test_membership_transition_commits_only_the_exact_runtime_attestation() -> None:
    now = 1_900_000_000
    initial = build_initial_hosted_authorization_bundle(
        cell_id="cell-alpha",
        logical_vault_id="tenant-alpha",
        replica_id="exo-0123456789abcdef0123-0",
        software_version="0.48.0",
        schema_version=4,
        recovery_envelope="signed-authorization-session-secret",
        now=now,
        entropy=_entropy,
    )
    keyring = authorization_custody.parse_keyring(initial.keyring)
    control = authorization_custody.parse_control_record(
        initial.control,
        keyring=keyring,
        now=now + 30,
    )
    attestation = authorization_serving_membership.ReplicaReadinessAttestation(
        version=1,
        epoch=2,
        replica_id="exo-0123456789abcdef0123-0",
        state="DRAINING",
        software_version="0.48.0",
        schema_version=4,
        cell_id="cell-alpha",
        active_key_id=keyring.active_key_id,
        accepted_key_ids=tuple(item.key_id for item in keyring.accepted_keys),
        control_digest=authorization_custody.control_attestation_digest(control),
        keyring_digest=authorization_custody.keyring_attestation_digest(keyring),
        attested_at=now + 29,
        expires_at=now + 329,
        issuance_stopped=True,
        no_in_flight=True,
        signing_key_id=keyring.active_key_id,
    )
    raw = authorization_serving_membership.encode_replica_readiness_attestation(
        attestation,
        verifier_keys={item.key_id: item.key for item in keyring.accepted_keys},
    )

    successor = transition_hosted_authorization_bundle(
        initial.files,
        expected_cell_id="cell-alpha",
        expected_logical_vault_id="tenant-alpha",
        expected_replica_id="exo-0123456789abcdef0123-0",
        expected_software_version="0.48.0",
        expected_schema_version=4,
        expected_recovery_envelope="signed-authorization-session-secret",
        target_state="DRAINING",
        target_no_in_flight=True,
        now=now + 30,
        ttl_seconds=300,
        runtime_attestation=raw,
    )

    assert (
        inspect_hosted_authorization_bundle(
            successor.files,
            expected_cell_id="cell-alpha",
            expected_logical_vault_id="tenant-alpha",
            expected_replica_id="exo-0123456789abcdef0123-0",
            expected_software_version="0.48.0",
            expected_schema_version=4,
            expected_recovery_envelope="signed-authorization-session-secret",
            now=now + 30,
        )
        == successor
    )

    _successor_control, successor_record = _runtime_membership(
        successor,
        now=now + 30,
    )
    assert successor_record.replicas == (attestation,)
    assert (successor_record.issued_at, successor_record.expires_at) == (
        attestation.attested_at,
        attestation.expires_at,
    )

    tampered = raw.replace(b'"no_in_flight":true', b'"no_in_flight":false')
    with pytest.raises(MetadataConflict, match="runtime attestation"):
        transition_hosted_authorization_bundle(
            initial.files,
            expected_cell_id="cell-alpha",
            expected_logical_vault_id="tenant-alpha",
            expected_replica_id="exo-0123456789abcdef0123-0",
            expected_software_version="0.48.0",
            expected_schema_version=4,
            expected_recovery_envelope="signed-authorization-session-secret",
            target_state="DRAINING",
            target_no_in_flight=True,
            now=now + 30,
            ttl_seconds=300,
            runtime_attestation=tampered,
        )
