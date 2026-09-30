"""Never-served bootstrap expiry is not serving-renewal authority."""

import importlib
import json

import pytest
from test_governance_provision_effects import bootstrap
from test_governance_readiness import METADATA, SOFTWARE_VERSION

from exomem_provisioner.authorization_membership import (
    enroll_hosted_governance_bundle,
    transition_hosted_authorization_bundle,
)
from exomem_provisioner.lifecycle import MetadataConflict


def drain(source, now):
    helper = importlib.import_module("exomem_provisioner.governance_provision_membership")
    return helper.drain_fresh_bootstrap_bundle(
        source.files,
        expected_cell_id=METADATA.subject_id,
        expected_logical_vault_id=METADATA.tenant_id,
        expected_replica_id=METADATA.resource_name + "-0",
        expected_software_version=SOFTWARE_VERSION,
        expected_recovery_envelope="original-envelope",
        now=now,
    )


def test_expired_genesis_can_only_become_schema3_unenrolled_draining_successor():
    source = bootstrap()
    successor = drain(source, source.expires_at + 1)
    assert successor.keyring == source.keyring
    assert successor.membership_schema_version == 3
    assert not successor.governance_enrolled
    assert successor.replica_state == "DRAINING"
    assert successor.no_in_flight and successor.issuance_stopped
    assert successor.epoch == source.epoch + 1
    assert json.loads(successor.membership)["previous_epoch_digest"] == source.membership_digest
    assert drain(successor, source.expires_at + 2).files == successor.files


def test_fresh_genesis_drain_preserves_existing_transition_bytes():
    source = bootstrap()
    now = json.loads(source.control)["issued_at"] + 5
    actual = drain(source, now)
    expected = transition_hosted_authorization_bundle(
        source.files,
        expected_cell_id=METADATA.subject_id,
        expected_logical_vault_id=METADATA.tenant_id,
        expected_replica_id=METADATA.resource_name + "-0",
        expected_software_version=SOFTWARE_VERSION,
        expected_recovery_envelope="original-envelope",
        expected_schema_version=3,
        target_state="DRAINING",
        target_no_in_flight=True,
        now=now,
    )
    assert actual.files == expected.files


@pytest.mark.parametrize("offset", [-1, 100_000_000])
def test_bootstrap_recovery_never_backdates_or_accepts_expired_keys(offset):
    source = bootstrap()
    with pytest.raises(MetadataConflict):
        drain(source, json.loads(source.control)["issued_at"] + offset)


def test_normal_transition_still_refuses_expired_bootstrap():
    source = bootstrap()
    with pytest.raises(MetadataConflict):
        transition_hosted_authorization_bundle(
            source.files,
            expected_cell_id=METADATA.subject_id,
            expected_logical_vault_id=METADATA.tenant_id,
            expected_replica_id=METADATA.resource_name + "-0",
            expected_software_version=SOFTWARE_VERSION,
            expected_recovery_envelope="original-envelope",
            expected_schema_version=3,
            target_state="DRAINING",
            target_no_in_flight=True,
            now=source.expires_at + 1,
        )


def test_bootstrap_exception_cannot_recover_enrolled_custody():
    initial = bootstrap()
    source = drain(initial, initial.expires_at + 1)
    enrolled = enroll_hosted_governance_bundle(
        source.files,
        expected_cell_id=METADATA.subject_id,
        expected_logical_vault_id=METADATA.tenant_id,
        expected_replica_id=METADATA.resource_name + "-0",
        expected_software_version=SOFTWARE_VERSION,
        expected_recovery_envelope="original-envelope",
        expected_schema_version=3,
        activation_store_id="activation-alpha",
        activation_epoch=1,
        activation_state_digest="a" * 64,
        now=initial.expires_at + 2,
    )
    with pytest.raises(MetadataConflict):
        drain(enrolled, initial.expires_at + 3)


@pytest.mark.parametrize("redrain", [False, True])
def test_bootstrap_exception_cannot_drain_a_resumed_serving_generation(redrain):
    initial = bootstrap()
    source = drain(initial, initial.expires_at + 1)
    resumed = transition_hosted_authorization_bundle(
        source.files,
        expected_cell_id=METADATA.subject_id,
        expected_logical_vault_id=METADATA.tenant_id,
        expected_replica_id=METADATA.resource_name + "-0",
        expected_software_version=SOFTWARE_VERSION,
        expected_recovery_envelope="original-envelope",
        expected_schema_version=3,
        target_state="SERVING",
        target_no_in_flight=False,
        now=initial.expires_at + 2,
    )
    if redrain:
        resumed = transition_hosted_authorization_bundle(
            resumed.files,
            expected_cell_id=METADATA.subject_id,
            expected_logical_vault_id=METADATA.tenant_id,
            expected_replica_id=METADATA.resource_name + "-0",
            expected_software_version=SOFTWARE_VERSION,
            expected_recovery_envelope="original-envelope",
            expected_schema_version=3,
            target_state="DRAINING",
            target_no_in_flight=True,
            now=initial.expires_at + 3,
        )
        assert resumed.epoch == 4
    with pytest.raises(MetadataConflict):
        drain(resumed, resumed.expires_at + 1)
