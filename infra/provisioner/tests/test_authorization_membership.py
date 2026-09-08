from __future__ import annotations

import hashlib
import json

import pytest
from exomem.governance import authorization_custody, authorization_serving_membership

from exomem_provisioner import authorization_membership
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


@pytest.mark.parametrize("schema_version", [3, 4])
def test_schema_discovery_authenticates_supported_membership(schema_version: int) -> None:
    now = 1_900_000_000
    bundle = build_initial_hosted_authorization_bundle(
        cell_id="cell-alpha",
        logical_vault_id="tenant-alpha",
        replica_id="replica-alpha",
        software_version="0.48.0",
        schema_version=schema_version,
        recovery_envelope="signed-authorization-session-secret",
        now=now,
    )
    observed = inspect_hosted_authorization_bundle(
        bundle.files,
        expected_cell_id="cell-alpha",
        expected_logical_vault_id="tenant-alpha",
        expected_replica_id="replica-alpha",
        expected_software_version=None,
        expected_schema_version=None,
        expected_recovery_envelope="signed-authorization-session-secret",
        now=now + 1,
    )
    assert observed == bundle
    assert observed.membership_schema_version == schema_version


@pytest.mark.parametrize("schema_version", [2, 5])
def test_schema_discovery_rejects_authenticated_unsupported_membership(schema_version: int) -> None:
    now = 1_900_000_000
    bundle = build_initial_hosted_authorization_bundle(
        cell_id="cell-alpha",
        logical_vault_id="tenant-alpha",
        replica_id="replica-alpha",
        software_version="0.48.0",
        schema_version=schema_version,
        recovery_envelope="signed-authorization-session-secret",
        now=now,
    )
    with pytest.raises(MetadataConflict):
        inspect_hosted_authorization_bundle(
            bundle.files,
            expected_cell_id="cell-alpha",
            expected_logical_vault_id="tenant-alpha",
            expected_replica_id="replica-alpha",
            expected_software_version=None,
            expected_schema_version=None,
            expected_recovery_envelope="signed-authorization-session-secret",
            now=now + 1,
        )


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


def _drained_bundle(now: int):
    """A cell that has been quiesced: DRAINING with its in-flight work acknowledged."""
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
    drained = transition_hosted_authorization_bundle(
        initial.files,
        **common,
        target_state="DRAINING",
        target_no_in_flight=True,
        now=now + 30,
    )
    return drained, common


def test_renewal_cannot_un_quiesce_a_drained_cell() -> None:
    """Renewal moves the window; it must not double as an unaudited resume.

    `renew_authorization_session` names `target_state="SERVING"` because that is
    the only state it ever renews. Against a drained cell that target would take
    the resume branch, so a background sweep running every minute would silently
    put a deliberately quiesced cell back into service.
    """
    now = 1_900_000_000
    drained, common = _drained_bundle(now)
    _, record = _runtime_membership(drained, now=now + 30)
    assert [replica.state for replica in record.replicas] == ["DRAINING"]

    with pytest.raises(MetadataConflict, match="renewal cannot change"):
        transition_hosted_authorization_bundle(
            drained.files,
            **common,
            target_state="SERVING",
            target_no_in_flight=False,
            now=now + 60,
            renew=True,
        )


def test_renewal_cannot_resurrect_a_drained_cell_whose_window_lapsed() -> None:
    """The drain exemption is the one path allowed to act on a stale bundle.

    It exists so a fully drained cell can still be resumed after its window
    closed. Renewal riding it would resurrect a cell expired by any amount at
    all, which is precisely the fence the whole change exists to respect.
    """
    now = 1_900_000_000
    drained, common = _drained_bundle(now)

    with pytest.raises(MetadataConflict, match="renewal cannot change"):
        transition_hosted_authorization_bundle(
            drained.files,
            **common,
            target_state="SERVING",
            target_no_in_flight=False,
            now=now + 999_999,
            renew=True,
        )

    # The exemption itself is untouched: a real resume of the same drained cell,
    # past expiry, still works. The guard closes renewal, not recovery.
    resumed = transition_hosted_authorization_bundle(
        drained.files,
        **common,
        target_state="SERVING",
        target_no_in_flight=False,
        now=now + 999_999,
    )
    _, resumed_record = _runtime_membership(resumed, now=now + 999_999)
    assert [replica.state for replica in resumed_record.replicas] == ["SERVING"]


def test_renewal_of_an_in_date_serving_cell_still_moves_the_window() -> None:
    """The control: the guard must not have closed the path renewal exists for."""
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
    _, before_record = _runtime_membership(initial, now=now)

    renewed = transition_hosted_authorization_bundle(
        initial.files,
        expected_cell_id="cell-alpha",
        expected_logical_vault_id="tenant-alpha",
        expected_replica_id="exo-0123456789abcdef0123-0",
        expected_software_version="0.48.0",
        expected_schema_version=4,
        expected_recovery_envelope="signed-authorization-session-secret",
        target_state="SERVING",
        target_no_in_flight=False,
        now=now + 1_800,
        renew=True,
    )
    _, renewed_record = _runtime_membership(renewed, now=now + 1_800)

    assert [replica.state for replica in renewed_record.replicas] == ["SERVING"]
    assert renewed_record.expires_at > before_record.expires_at


def test_enrollment_rewrites_only_the_authenticated_control_tuple() -> None:
    now = 1_900_000_000
    initial = build_initial_hosted_authorization_bundle(
        cell_id="cell-alpha",
        logical_vault_id="tenant-alpha",
        replica_id="exo-0123456789abcdef0123-0",
        software_version="0.48.0",
        schema_version=3,
        recovery_envelope="signed-authorization-session-secret",
        now=now,
        entropy=_entropy,
    )
    common = {
        "expected_cell_id": "cell-alpha",
        "expected_logical_vault_id": "tenant-alpha",
        "expected_replica_id": "exo-0123456789abcdef0123-0",
        "expected_software_version": "0.48.0",
        "expected_schema_version": 3,
        "expected_recovery_envelope": "signed-authorization-session-secret",
    }
    drained = transition_hosted_authorization_bundle(
        initial.files,
        **common,
        target_state="DRAINING",
        target_no_in_flight=True,
        now=now + 30,
    )

    enrolled = authorization_membership.enroll_hosted_governance_bundle(
        drained.files,
        **common,
        activation_store_id="activation-store-alpha",
        activation_epoch=9,
        activation_state_digest="a" * 64,
        now=now + 31,
    )

    control, membership = _runtime_membership(enrolled, now=now + 31)
    assert (
        enrolled.membership_schema_version,
        enrolled.governance_enrolled,
        enrolled.activation_store_id,
        enrolled.activation_epoch,
        enrolled.activation_state_digest,
    ) == (3, True, "activation-store-alpha", 9, "a" * 64)
    assert (
        control.governance_enrolled,
        control.activation_store_id,
        control.activation_epoch,
        control.activation_state_digest,
    ) == (True, "activation-store-alpha", 9, "a" * 64)
    assert membership.replicas[0].schema_version == 3
    assert enrolled.keyring == drained.keyring
    assert enrolled.membership == drained.membership
    assert (enrolled.epoch, enrolled.membership_digest) == (
        drained.epoch,
        drained.membership_digest,
    )

    before = json.loads(drained.control)
    after = json.loads(enrolled.control)
    assert {name for name in after if after[name] != before[name]} == {
        "governance_enrolled",
        "activation_store_id",
        "activation_epoch",
        "activation_state_digest",
        "mac",
    }


@pytest.fixture
def enrollment_source():
    now = 1_900_000_000
    initial = build_initial_hosted_authorization_bundle(
        cell_id="cell-alpha",
        logical_vault_id="tenant-alpha",
        replica_id="exo-0123456789abcdef0123-0",
        software_version="0.48.0",
        schema_version=3,
        recovery_envelope="signed-authorization-session-secret",
        now=now,
        entropy=_entropy,
    )
    identity = {
        "expected_cell_id": "cell-alpha",
        "expected_logical_vault_id": "tenant-alpha",
        "expected_replica_id": "exo-0123456789abcdef0123-0",
        "expected_software_version": "0.48.0",
        "expected_schema_version": 3,
        "expected_recovery_envelope": "signed-authorization-session-secret",
    }
    drained = transition_hosted_authorization_bundle(
        initial.files,
        **identity,
        target_state="DRAINING",
        target_no_in_flight=True,
        now=now + 30,
    )
    target = {
        "activation_store_id": "activation-store-alpha",
        "activation_epoch": 9,
        "activation_state_digest": "a" * 64,
        "now": now + 31,
    }
    return initial, drained, identity, target


def test_enrollment_exact_replay_preserves_every_byte(enrollment_source) -> None:
    _, drained, identity, target = enrollment_source
    assert drained.epoch > 1
    enrolled = authorization_membership.enroll_hosted_governance_bundle(
        drained.files,
        **identity,
        **target,
    )
    replay = authorization_membership.enroll_hosted_governance_bundle(
        enrolled.files,
        **identity,
        **{**target, "now": target["now"] + 1},
    )
    assert replay == enrolled
    assert replay.files == enrolled.files


@pytest.mark.parametrize(
    "field,value",
    [
        ("activation_store_id", "another-store"),
        ("activation_epoch", 10),
        ("activation_state_digest", "b" * 64),
    ],
)
def test_enrollment_never_replaces_an_enrolled_target(enrollment_source, field, value) -> None:
    _, drained, identity, target = enrollment_source
    enrolled = authorization_membership.enroll_hosted_governance_bundle(
        drained.files,
        **identity,
        **target,
    )
    with pytest.raises(MetadataConflict):
        authorization_membership.enroll_hosted_governance_bundle(
            enrolled.files,
            **identity,
            **{**target, field: value},
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("activation_store_id", ""),
        ("activation_store_id", "space forbidden"),
        ("activation_store_id", "x" * 513),
        ("activation_store_id", None),
        ("activation_epoch", 0),
        ("activation_epoch", -1),
        ("activation_epoch", True),
        ("activation_epoch", "1"),
        ("activation_epoch", 1.0),
        ("activation_epoch", 1 << 63),
        ("activation_state_digest", "A" * 64),
        ("activation_state_digest", "a" * 63),
        ("activation_state_digest", None),
    ],
)
def test_enrollment_rejects_invalid_targets(enrollment_source, field, value) -> None:
    _, drained, identity, target = enrollment_source
    with pytest.raises(MetadataConflict):
        authorization_membership.enroll_hosted_governance_bundle(
            drained.files,
            **identity,
            **{**target, field: value},
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_cell_id", "foreign-cell"),
        ("expected_logical_vault_id", "foreign-vault"),
        ("expected_replica_id", "foreign-replica"),
        ("expected_software_version", "other-release"),
        ("expected_recovery_envelope", "foreign-envelope"),
        ("expected_schema_version", 4),
        ("expected_schema_version", True),
    ],
)
def test_enrollment_rejects_foreign_or_non_v3_authority(enrollment_source, field, value) -> None:
    _, drained, identity, target = enrollment_source
    with pytest.raises(MetadataConflict):
        authorization_membership.enroll_hosted_governance_bundle(
            drained.files,
            **{**identity, field: value},
            **target,
        )


@pytest.mark.parametrize("file", ["keyring.json", "control.json", "serving-membership.json"])
def test_enrollment_rejects_missing_or_tampered_custody(enrollment_source, file) -> None:
    _, drained, identity, target = enrollment_source
    missing = {name: raw for name, raw in drained.files.items() if name != file}
    for files in (missing, {**drained.files, file: b"{}"}):
        with pytest.raises(MetadataConflict):
            authorization_membership.enroll_hosted_governance_bundle(
                files,
                **identity,
                **target,
            )


def test_enrollment_refuses_serving_and_expired_generations(enrollment_source) -> None:
    initial, drained, identity, target = enrollment_source
    for files, now in ((initial.files, target["now"]), (drained.files, drained.expires_at)):
        with pytest.raises(MetadataConflict):
            authorization_membership.enroll_hosted_governance_bundle(
                files,
                **identity,
                **{**target, "now": now},
            )


def test_enrollment_refuses_authenticated_drain_without_no_in_flight(enrollment_source) -> None:
    _, drained, identity, target = enrollment_source
    keyring = authorization_custody.parse_keyring(drained.keyring)
    key = keyring.active_key.key
    membership = json.loads(drained.membership)
    replica = membership["replicas"][0]
    replica["no_in_flight"] = False
    replica["mac"] = authorization_membership._mac(
        key,
        authorization_membership._attestation_mac_input(replica),
    )
    membership["mac"] = authorization_membership._mac(
        key,
        authorization_membership._membership_mac_input(membership),
    )
    membership_raw = authorization_membership._canonical(membership)
    control = json.loads(drained.control)
    control["serving_membership_digest"] = hashlib.sha256(membership_raw).hexdigest()
    control["mac"] = authorization_membership._mac(
        key,
        authorization_membership._control_mac_input(control),
    )
    files = {
        **drained.files,
        "control.json": authorization_membership._canonical(control),
        "serving-membership.json": membership_raw,
    }
    assert (
        inspect_hosted_authorization_bundle(files, **identity, now=target["now"]).no_in_flight
        is False
    )
    with pytest.raises(MetadataConflict):
        authorization_membership.enroll_hosted_governance_bundle(files, **identity, **target)


def test_ordinary_transitions_preserve_schema_and_enrollment(enrollment_source) -> None:
    _, drained, identity, target = enrollment_source
    enrolled = authorization_membership.enroll_hosted_governance_bundle(
        drained.files,
        **identity,
        **target,
    )
    for before in (drained, enrolled):
        renewed = transition_hosted_authorization_bundle(
            before.files,
            **identity,
            target_state="DRAINING",
            target_no_in_flight=True,
            now=target["now"] + 1,
            renew=True,
        )
        resumed = transition_hosted_authorization_bundle(
            renewed.files,
            **identity,
            target_state="SERVING",
            target_no_in_flight=False,
            now=target["now"] + 2,
        )
        control, membership = _runtime_membership(resumed, now=target["now"] + 2)
        assert resumed.membership_schema_version == membership.replicas[0].schema_version == 3
        assert control.governance_enrolled is before.governance_enrolled
        assert (
            control.activation_store_id,
            control.activation_epoch,
            control.activation_state_digest,
        ) == (
            before.activation_store_id,
            before.activation_epoch,
            before.activation_state_digest,
        )
