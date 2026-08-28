from __future__ import annotations

import hashlib

from exomem.governance import authorization_custody, authorization_serving_membership

from exomem_provisioner.authorization_membership import (
    build_initial_hosted_authorization_bundle,
    inspect_hosted_authorization_bundle,
)


def _entropy(length: int) -> bytes:
    assert length == 32
    return bytes(range(32))


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
    assert bundle.revision == hashlib.sha256(
        bundle.keyring + bundle.control + bundle.membership
    ).hexdigest()


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
