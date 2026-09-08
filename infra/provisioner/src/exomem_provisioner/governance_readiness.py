"""Closed verification of the private hosted governance-readiness proof."""

from __future__ import annotations

import json
import re

from .authorization_membership import HostedAuthorizationBundle
from .conflict_reason import ConflictReason
from .lifecycle import MetadataConflict

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,511}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_INTEGER = (1 << 63) - 1
_PROOF_FIELDS = frozenset(
    {
        "schemaVersion",
        "actualSchema",
        "cellId",
        "vaultId",
        "replicaId",
        "softwareVersion",
        "governanceEnrolled",
        "activationStoreId",
        "activationEpoch",
        "activationStateDigest",
        "custodyRevision",
        "membershipEpoch",
        "membershipDigest",
        "storeAgreement",
    }
)
_CONTROL_FIELDS = frozenset(
    {
        "version",
        "keyring_id",
        "cell_id",
        "logical_vault_id",
        "registry_attachment_id",
        "attachment_epoch",
        "governance_enrolled",
        "activation_store_id",
        "activation_epoch",
        "activation_state_digest",
        "serving_membership_epoch",
        "serving_membership_digest",
        "issued_at",
        "expires_at",
        "signing_key_id",
        "mac",
    }
)
_MEMBERSHIP_FIELDS = frozenset(
    {
        "version",
        "epoch",
        "cell_id",
        "logical_vault_id",
        "previous_epoch_digest",
        "issued_at",
        "expires_at",
        "replicas",
        "signing_key_id",
        "mac",
    }
)
_ATTESTATION_FIELDS = frozenset(
    {
        "version",
        "epoch",
        "replica_id",
        "state",
        "software_version",
        "schema_version",
        "cell_id",
        "active_key_id",
        "accepted_key_ids",
        "control_digest",
        "keyring_digest",
        "attested_at",
        "expires_at",
        "issuance_stopped",
        "no_in_flight",
        "signing_key_id",
        "mac",
    }
)


def _refuse() -> MetadataConflict:
    return MetadataConflict(
        "governance readiness is unavailable",
        reason=ConflictReason.AUTHORIZATION_MEMBERSHIP_IS_INVALID,
    )


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result


def _closed_json(raw: bytes, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise ValueError
    value = json.loads(raw, object_pairs_hook=_closed_object)
    if type(value) is not dict or set(value) != fields:
        raise ValueError
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_INTEGER:
        raise ValueError
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError
    return value


def verify_governance_readiness(
    proof: object,
    *,
    bundle: HostedAuthorizationBundle,
    cell_id: str,
    vault_id: str,
    replica_id: str,
    software_version: str,
) -> None:
    """Refuse a private readiness proof unless it exactly matches custody."""

    try:
        if type(proof) is not dict or type(bundle) is not HostedAuthorizationBundle:
            raise ValueError
        if set(proof) != _PROOF_FIELDS:
            raise ValueError
        cell = _identifier(cell_id)
        vault = _identifier(vault_id)
        replica = _identifier(replica_id)
        release = _identifier(software_version)
        if (
            _integer(proof["schemaVersion"]) != 1
            or _integer(proof["actualSchema"]) != 4
            or proof["cellId"] != cell
            or proof["vaultId"] != vault
            or proof["replicaId"] != replica
            or proof["softwareVersion"] != release
            or proof["governanceEnrolled"] is not True
            or proof["storeAgreement"] is not True
            or _identifier(proof["activationStoreId"]) != proof["activationStoreId"]
            or _integer(proof["activationEpoch"]) != proof["activationEpoch"]
            or _digest(proof["activationStateDigest"]) != proof["activationStateDigest"]
            or _digest(proof["custodyRevision"]) != proof["custodyRevision"]
            or _integer(proof["membershipEpoch"]) != proof["membershipEpoch"]
            or _digest(proof["membershipDigest"]) != proof["membershipDigest"]
            or bundle.membership_schema_version != 4
            or bundle.governance_enrolled is not True
            or bundle.replica_state != "SERVING"
            or bundle.issuance_stopped is not False
            or bundle.no_in_flight is not False
            or bundle.software_version != release
            or bundle.activation_store_id != proof["activationStoreId"]
            or bundle.activation_epoch != proof["activationEpoch"]
            or bundle.activation_state_digest != proof["activationStateDigest"]
            or bundle.revision != proof["custodyRevision"]
            or _integer(bundle.epoch) != proof["membershipEpoch"]
            or bundle.membership_digest != proof["membershipDigest"]
        ):
            raise ValueError
        control = _closed_json(bundle.control, _CONTROL_FIELDS)
        membership = _closed_json(bundle.membership, _MEMBERSHIP_FIELDS)
        replicas = membership["replicas"]
        if (
            control["cell_id"] != cell
            or control["logical_vault_id"] != vault
            or control["governance_enrolled"] is not True
            or control["activation_store_id"] != proof["activationStoreId"]
            or control["activation_epoch"] != proof["activationEpoch"]
            or control["activation_state_digest"] != proof["activationStateDigest"]
            or control["serving_membership_epoch"] != proof["membershipEpoch"]
            or control["serving_membership_digest"] != proof["membershipDigest"]
            or membership["epoch"] != proof["membershipEpoch"]
            or membership["cell_id"] != cell
            or membership["logical_vault_id"] != vault
            or not isinstance(replicas, list)
            or len(replicas) != 1
            or type(replicas[0]) is not dict
            or set(replicas[0]) != _ATTESTATION_FIELDS
            or replicas[0]["replica_id"] != replica
            or replicas[0]["software_version"] != release
            or replicas[0]["schema_version"] != 4
            or replicas[0]["state"] != "SERVING"
            or replicas[0]["issuance_stopped"] is not False
            or replicas[0]["no_in_flight"] is not False
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise _refuse() from None
