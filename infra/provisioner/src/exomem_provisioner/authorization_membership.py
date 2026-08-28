"""Provisioner-owned bootstrap custody for Hosted authorization sessions.

This module deliberately carries the exact version-one wire contract without
importing the product package.  The provisioner and the cell ship separately;
compatibility is proved by tests that feed these bytes to the runtime parser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from .lifecycle import MetadataConflict

AUTHORIZATION_SESSION_SECRET_NAME: Final = "exomem-authorization-session"
AUTHORIZATION_SESSION_FILES: Final = frozenset(
    {"keyring.json", "control.json", "serving-membership.json"}
)
AUTHORIZATION_SESSION_SCHEMA_VERSION: Final = 4
MAX_BUNDLE_FILE_BYTES: Final = 64 * 1024
MAX_ATTESTATION_TTL_SECONDS: Final = 3_630
DEFAULT_ATTESTATION_TTL_SECONDS: Final = 3_600
_KEY_TTL_SECONDS: Final = 366 * 24 * 60 * 60
_MAX_INTEGER: Final = (1 << 63) - 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,511}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_MAC_DOMAIN = b"exomem.authorization-session.control/v1"
_CONTROL_BASIS_DOMAIN = b"exomem.authorization-session.control-attestation-basis/v1"
_ATTESTATION_MAC_DOMAIN = b"exomem.authorization-session.replica-readiness/v1"
_MEMBERSHIP_MAC_DOMAIN = b"exomem.authorization-session.serving-membership/v1"
_ATTACHMENT_DOMAIN = b"exomem.authorization-session.hosted-attachment/v1"
_KEYRING_ID_DOMAIN = b"exomem.authorization-session.hosted-keyring/v1"
_KEY_ID_DOMAIN = b"exomem.authorization-session.hosted-key/v1"


@dataclass(frozen=True, slots=True)
class HostedAuthorizationBundle:
    keyring: bytes = field(repr=False)
    control: bytes = field(repr=False)
    membership: bytes = field(repr=False)
    epoch: int
    membership_digest: str
    revision: str
    registry_attachment_id: str
    expires_at: int
    replica_state: str
    software_version: str
    issuance_stopped: bool
    no_in_flight: bool

    @property
    def files(self) -> dict[str, bytes]:
        return {
            "keyring.json": self.keyring,
            "control.json": self.control,
            "serving-membership.json": self.membership,
        }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _framed(domain: bytes, fields: tuple[bytes, ...]) -> bytes:
    output = bytearray(domain)
    output.append(0)
    for value in fields:
        output.extend(len(value).to_bytes(4, "big"))
        output.extend(value)
    return bytes(output)


def _digest_identifier(domain: bytes, fields: tuple[bytes, ...], *, prefix: str) -> str:
    return prefix + hashlib.sha256(_framed(domain, fields)).hexdigest()


def _mac(key: bytes, payload: bytes) -> str:
    return (
        base64.urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def _keyring_digest(keyring: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical(keyring)).hexdigest()


def _control_basis_digest(control: Mapping[str, object]) -> str:
    return hashlib.sha256(
        _framed(
            _CONTROL_BASIS_DOMAIN,
            (
                str(control["version"]).encode("ascii"),
                str(control["keyring_id"]).encode("utf-8"),
                str(control["cell_id"]).encode("utf-8"),
                str(control["logical_vault_id"]).encode("utf-8"),
                str(control["registry_attachment_id"]).encode("utf-8"),
                str(control["attachment_epoch"]).encode("ascii"),
            ),
        )
    ).hexdigest()


def _attestation_mac_input(value: Mapping[str, object]) -> bytes:
    accepted = value["accepted_key_ids"]
    if not isinstance(accepted, list):
        raise MetadataConflict("authorization membership attestation is invalid")
    return _framed(
        _ATTESTATION_MAC_DOMAIN,
        (
            str(value["version"]).encode("ascii"),
            str(value["epoch"]).encode("ascii"),
            str(value["replica_id"]).encode("utf-8"),
            str(value["state"]).encode("ascii"),
            str(value["software_version"]).encode("utf-8"),
            str(value["schema_version"]).encode("ascii"),
            str(value["cell_id"]).encode("utf-8"),
            str(value["active_key_id"]).encode("utf-8"),
            json.dumps(accepted, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            str(value["control_digest"]).encode("ascii"),
            str(value["keyring_digest"]).encode("ascii"),
            str(value["attested_at"]).encode("ascii"),
            str(value["expires_at"]).encode("ascii"),
            b"true" if value["issuance_stopped"] is True else b"false",
            b"true" if value["no_in_flight"] is True else b"false",
            str(value["signing_key_id"]).encode("utf-8"),
        ),
    )


def _membership_mac_input(value: Mapping[str, object]) -> bytes:
    replicas = value["replicas"]
    previous = value["previous_epoch_digest"]
    return _framed(
        _MEMBERSHIP_MAC_DOMAIN,
        (
            str(value["version"]).encode("ascii"),
            str(value["epoch"]).encode("ascii"),
            str(value["cell_id"]).encode("utf-8"),
            str(value["logical_vault_id"]).encode("utf-8"),
            b"" if previous is None else str(previous).encode("ascii"),
            str(value["issued_at"]).encode("ascii"),
            str(value["expires_at"]).encode("ascii"),
            _canonical(replicas),
            str(value["signing_key_id"]).encode("utf-8"),
        ),
    )


def _control_mac_input(value: Mapping[str, object]) -> bytes:
    activation_store = value["activation_store_id"]
    activation_epoch = value["activation_epoch"]
    activation_digest = value["activation_state_digest"]
    return _framed(
        _CONTROL_MAC_DOMAIN,
        (
            str(value["version"]).encode("ascii"),
            str(value["keyring_id"]).encode("utf-8"),
            str(value["cell_id"]).encode("utf-8"),
            str(value["logical_vault_id"]).encode("utf-8"),
            str(value["registry_attachment_id"]).encode("utf-8"),
            str(value["attachment_epoch"]).encode("ascii"),
            b"true" if value["governance_enrolled"] is True else b"false",
            b"" if activation_store is None else str(activation_store).encode("utf-8"),
            b"" if activation_epoch is None else str(activation_epoch).encode("ascii"),
            b"" if activation_digest is None else str(activation_digest).encode("ascii"),
            str(value["serving_membership_epoch"]).encode("ascii"),
            str(value["serving_membership_digest"]).encode("ascii"),
            str(value["issued_at"]).encode("ascii"),
            str(value["expires_at"]).encode("ascii"),
            str(value["signing_key_id"]).encode("utf-8"),
        ),
    )


def _required_identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise MetadataConflict("authorization membership identity is invalid")
    return value


def _required_time(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_INTEGER:
        raise MetadataConflict("authorization membership time is invalid")
    return value


def _closed_json(raw: bytes, fields: frozenset[str], *, label: str) -> dict[str, object]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BUNDLE_FILE_BYTES:
        raise MetadataConflict(f"authorization {label} is invalid")

    def closed(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, value in pairs:
            if name in result:
                raise MetadataConflict(f"authorization {label} is invalid")
            result[name] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=closed)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise MetadataConflict(f"authorization {label} is invalid") from error
    if not isinstance(value, dict) or set(value) != fields or _canonical(value) != raw:
        raise MetadataConflict(f"authorization {label} is invalid")
    return value


def build_initial_hosted_authorization_bundle(
    *,
    cell_id: str,
    logical_vault_id: str,
    replica_id: str,
    software_version: str,
    schema_version: int,
    recovery_envelope: str,
    now: int,
    entropy: Callable[[int], bytes] = secrets.token_bytes,
    ttl_seconds: int = DEFAULT_ATTESTATION_TTL_SECONDS,
) -> HostedAuthorizationBundle:
    """Mint the first fail-closed Hosted custody generation before cell startup."""

    cell = _required_identifier(cell_id)
    vault = _required_identifier(logical_vault_id)
    replica = _required_identifier(replica_id)
    release = _required_identifier(software_version)
    current = _required_time(now)
    schema = _required_time(schema_version)
    if (
        not isinstance(recovery_envelope, str)
        or not recovery_envelope
        or not 1 <= ttl_seconds <= MAX_ATTESTATION_TTL_SECONDS
    ):
        raise MetadataConflict("authorization membership bootstrap input is invalid")
    key = entropy(32)
    if not isinstance(key, bytes) or len(key) != 32:
        raise MetadataConflict("authorization membership entropy is invalid")
    identity_fields = (key, cell.encode("utf-8"), vault.encode("utf-8"))
    keyring_id = _digest_identifier(
        _KEYRING_ID_DOMAIN, identity_fields, prefix="hosted-keyring-v1-"
    )
    key_id = _digest_identifier(_KEY_ID_DOMAIN, identity_fields, prefix="hosted-key-v1-")
    attachment = _digest_identifier(
        _ATTACHMENT_DOMAIN,
        (
            recovery_envelope.encode("utf-8"),
            cell.encode("utf-8"),
            vault.encode("utf-8"),
        ),
        prefix="hosted-attachment-v1-",
    )
    expires_at = current + ttl_seconds
    keyring: dict[str, object] = {
        "version": 1,
        "keyring_id": keyring_id,
        "cell_id": cell,
        "logical_vault_id": vault,
        "active_key_id": key_id,
        "accepted_keys": [
            {
                "key_id": key_id,
                "key": base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii"),
                "not_before": current,
                "not_after": current + _KEY_TTL_SECONDS,
            }
        ],
    }
    keyring_raw = _canonical(keyring)
    control_basis: dict[str, object] = {
        "version": 1,
        "keyring_id": keyring_id,
        "cell_id": cell,
        "logical_vault_id": vault,
        "registry_attachment_id": attachment,
        "attachment_epoch": 1,
    }
    attestation: dict[str, object] = {
        "version": 1,
        "epoch": 1,
        "replica_id": replica,
        "state": "SERVING",
        "software_version": release,
        "schema_version": schema,
        "cell_id": cell,
        "active_key_id": key_id,
        "accepted_key_ids": [key_id],
        "control_digest": _control_basis_digest(control_basis),
        "keyring_digest": _keyring_digest(keyring),
        "attested_at": current,
        "expires_at": expires_at,
        "issuance_stopped": False,
        "no_in_flight": False,
        "signing_key_id": key_id,
    }
    attestation["mac"] = _mac(key, _attestation_mac_input(attestation))
    membership: dict[str, object] = {
        "version": 1,
        "epoch": 1,
        "cell_id": cell,
        "logical_vault_id": vault,
        "previous_epoch_digest": None,
        "issued_at": current,
        "expires_at": expires_at,
        "replicas": [attestation],
        "signing_key_id": key_id,
    }
    membership["mac"] = _mac(key, _membership_mac_input(membership))
    membership_raw = _canonical(membership)
    membership_digest = hashlib.sha256(membership_raw).hexdigest()
    control: dict[str, object] = {
        **control_basis,
        "governance_enrolled": False,
        "activation_store_id": None,
        "activation_epoch": None,
        "activation_state_digest": None,
        "serving_membership_epoch": 1,
        "serving_membership_digest": membership_digest,
        "issued_at": current,
        "expires_at": expires_at,
        "signing_key_id": key_id,
    }
    control["mac"] = _mac(key, _control_mac_input(control))
    control_raw = _canonical(control)
    return HostedAuthorizationBundle(
        keyring=keyring_raw,
        control=control_raw,
        membership=membership_raw,
        epoch=1,
        membership_digest=membership_digest,
        revision=hashlib.sha256(keyring_raw + control_raw + membership_raw).hexdigest(),
        registry_attachment_id=attachment,
        expires_at=expires_at,
        replica_state="SERVING",
        software_version=release,
        issuance_stopped=False,
        no_in_flight=False,
    )


def inspect_hosted_authorization_bundle(
    files: Mapping[str, bytes],
    *,
    expected_cell_id: str,
    expected_logical_vault_id: str,
    expected_replica_id: str,
    expected_software_version: str | None,
    expected_schema_version: int,
    expected_recovery_envelope: str,
    now: int,
    _require_fresh: bool = True,
) -> HostedAuthorizationBundle:
    """Authenticate one exact singleton Hosted authorization generation."""

    if set(files) != AUTHORIZATION_SESSION_FILES:
        raise MetadataConflict("authorization session bundle shape is invalid")
    keyring_raw = files["keyring.json"]
    control_raw = files["control.json"]
    membership_raw = files["serving-membership.json"]
    keyring = _closed_json(
        keyring_raw,
        frozenset(
            {
                "version",
                "keyring_id",
                "cell_id",
                "logical_vault_id",
                "active_key_id",
                "accepted_keys",
            }
        ),
        label="keyring",
    )
    entries = keyring["accepted_keys"]
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise MetadataConflict("authorization keyring is invalid")
    entry = entries[0]
    if set(entry) != {"key_id", "key", "not_before", "not_after"}:
        raise MetadataConflict("authorization keyring is invalid")
    encoded_key = entry["key"]
    if not isinstance(encoded_key, str) or len(encoded_key) != 43:
        raise MetadataConflict("authorization keyring is invalid")
    try:
        key = base64.b64decode(encoded_key.encode("ascii") + b"=", altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise MetadataConflict("authorization keyring is invalid") from error
    current = _required_time(now)
    cell = _required_identifier(expected_cell_id)
    vault = _required_identifier(expected_logical_vault_id)
    replica = _required_identifier(expected_replica_id)
    release = (
        None
        if expected_software_version is None
        else _required_identifier(expected_software_version)
    )
    schema = _required_time(expected_schema_version)
    if not isinstance(expected_recovery_envelope, str) or not expected_recovery_envelope:
        raise MetadataConflict("authorization Secret provider authority is absent")
    key_id = _required_identifier(entry["key_id"])
    if (
        keyring["version"] != 1
        or keyring["cell_id"] != cell
        or keyring["logical_vault_id"] != vault
        or keyring["active_key_id"] != key_id
        or len(key) != 32
        or base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii") != encoded_key
        or not _required_time(entry["not_before"]) <= current < _required_time(entry["not_after"])
    ):
        raise MetadataConflict("authorization keyring is invalid")
    control = _closed_json(
        control_raw,
        frozenset(
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
        ),
        label="control",
    )
    expected_attachment = _digest_identifier(
        _ATTACHMENT_DOMAIN,
        (
            expected_recovery_envelope.encode("utf-8"),
            cell.encode("utf-8"),
            vault.encode("utf-8"),
        ),
        prefix="hosted-attachment-v1-",
    )
    supplied_control_mac = control.pop("mac")
    if (
        control["version"] != 1
        or control["keyring_id"] != keyring["keyring_id"]
        or control["cell_id"] != cell
        or control["logical_vault_id"] != vault
        or control["registry_attachment_id"] != expected_attachment
        or _required_time(control["attachment_epoch"]) < 1
        or not isinstance(control["governance_enrolled"], bool)
        or (
            control["governance_enrolled"] is False
            and any(
                control[name] is not None
                for name in (
                    "activation_store_id",
                    "activation_epoch",
                    "activation_state_digest",
                )
            )
        )
        or (
            control["governance_enrolled"] is True
            and (
                not isinstance(control["activation_store_id"], str)
                or not control["activation_store_id"]
                or _required_time(control["activation_epoch"]) < 1
                or not isinstance(control["activation_state_digest"], str)
                or _SHA256.fullmatch(control["activation_state_digest"]) is None
            )
        )
        or _required_time(control["serving_membership_epoch"]) < 1
        or control["signing_key_id"] != key_id
        or _required_time(control["issued_at"]) >= _required_time(control["expires_at"])
        or (
            _require_fresh
            and not _required_time(control["issued_at"])
            <= current
            < _required_time(control["expires_at"])
        )
        or _required_time(control["expires_at"]) - _required_time(control["issued_at"])
        > MAX_ATTESTATION_TTL_SECONDS
        or supplied_control_mac != _mac(key, _control_mac_input(control))
    ):
        raise MetadataConflict("authorization control is invalid")
    membership = _closed_json(
        membership_raw,
        frozenset(
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
        ),
        label="membership",
    )
    replicas = membership["replicas"]
    if not isinstance(replicas, list) or len(replicas) != 1 or not isinstance(replicas[0], dict):
        raise MetadataConflict("authorization membership is invalid")
    attestation = replicas[0]
    if set(attestation) != {
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
    }:
        raise MetadataConflict("authorization membership is invalid")
    supplied_attestation_mac = attestation.get("mac")
    supplied_membership_mac = membership.pop("mac")
    expected_membership_mac = _mac(key, _membership_mac_input(membership))
    expected_attestation_mac = _mac(key, _attestation_mac_input(attestation))
    digest = hashlib.sha256(membership_raw).hexdigest()
    if (
        membership["version"] != 1
        or _required_time(membership["epoch"])
        != _required_time(control["serving_membership_epoch"])
        or membership["cell_id"] != cell
        or membership["logical_vault_id"] != vault
        or (
            membership["previous_epoch_digest"] is not None
            if membership["epoch"] == 1
            else not isinstance(membership["previous_epoch_digest"], str)
            or _SHA256.fullmatch(membership["previous_epoch_digest"]) is None
        )
        or membership["signing_key_id"] != key_id
        or _required_time(membership["issued_at"]) >= _required_time(membership["expires_at"])
        or (
            _require_fresh
            and not _required_time(membership["issued_at"])
            <= current
            < _required_time(membership["expires_at"])
        )
        or digest != control["serving_membership_digest"]
        or membership["issued_at"] != control["issued_at"]
        or membership["expires_at"] != control["expires_at"]
        or _required_time(membership["expires_at"]) - _required_time(membership["issued_at"])
        > MAX_ATTESTATION_TTL_SECONDS
        or supplied_membership_mac != expected_membership_mac
        or supplied_attestation_mac != expected_attestation_mac
        or attestation.get("replica_id") != replica
        or attestation.get("version") != 1
        or attestation.get("epoch") != membership["epoch"]
        or attestation.get("state") not in {"SERVING", "DRAINING"}
        or (release is not None and attestation.get("software_version") != release)
        or attestation.get("schema_version") != schema
        or attestation.get("cell_id") != cell
        or attestation.get("active_key_id") != key_id
        or attestation.get("accepted_key_ids") != [key_id]
        or attestation.get("attested_at") != membership["issued_at"]
        or attestation.get("expires_at") != membership["expires_at"]
        or not isinstance(attestation.get("issuance_stopped"), bool)
        or not isinstance(attestation.get("no_in_flight"), bool)
        or (
            attestation.get("state") == "SERVING"
            and (
                attestation.get("issuance_stopped") is not False
                or attestation.get("no_in_flight") is not False
            )
        )
        or (
            attestation.get("state") == "DRAINING"
            and attestation.get("issuance_stopped") is not True
        )
        or attestation.get("signing_key_id") != key_id
        or attestation.get("control_digest") != _control_basis_digest(control)
        or attestation.get("keyring_digest") != _keyring_digest(keyring)
    ):
        raise MetadataConflict("authorization membership is invalid")
    return HostedAuthorizationBundle(
        keyring=keyring_raw,
        control=control_raw,
        membership=membership_raw,
        epoch=_required_time(membership["epoch"]),
        membership_digest=digest,
        revision=hashlib.sha256(keyring_raw + control_raw + membership_raw).hexdigest(),
        registry_attachment_id=expected_attachment,
        expires_at=_required_time(membership["expires_at"]),
        replica_state=str(attestation["state"]),
        software_version=str(attestation["software_version"]),
        issuance_stopped=bool(attestation["issuance_stopped"]),
        no_in_flight=bool(attestation["no_in_flight"]),
    )


def transition_hosted_authorization_bundle(
    files: Mapping[str, bytes],
    *,
    expected_cell_id: str,
    expected_logical_vault_id: str,
    expected_replica_id: str,
    expected_software_version: str | None,
    expected_schema_version: int,
    expected_recovery_envelope: str,
    target_state: str,
    target_no_in_flight: bool,
    target_software_version: str | None = None,
    now: int,
    ttl_seconds: int = DEFAULT_ATTESTATION_TTL_SECONDS,
    renew: bool = False,
    runtime_attestation: bytes | None = None,
) -> HostedAuthorizationBundle:
    """Publish one authenticated singleton successor, never a liveness inference."""

    if (
        target_state not in {"SERVING", "DRAINING"}
        or not isinstance(target_no_in_flight, bool)
        or (target_state == "SERVING" and target_no_in_flight)
        or not isinstance(renew, bool)
        or (runtime_attestation is not None and not isinstance(runtime_attestation, bytes))
        or not 1 <= ttl_seconds <= MAX_ATTESTATION_TTL_SECONDS
    ):
        raise MetadataConflict("authorization membership transition is invalid")
    current = _required_time(now)
    source = inspect_hosted_authorization_bundle(
        files,
        expected_cell_id=expected_cell_id,
        expected_logical_vault_id=expected_logical_vault_id,
        expected_replica_id=expected_replica_id,
        expected_software_version=expected_software_version,
        expected_schema_version=expected_schema_version,
        expected_recovery_envelope=expected_recovery_envelope,
        now=current,
        _require_fresh=False,
    )
    target_release = _required_identifier(
        source.software_version if target_software_version is None else target_software_version
    )
    if (
        source.replica_state == target_state
        and source.no_in_flight is target_no_in_flight
        and source.software_version == target_release
        and not renew
    ):
        return source
    if source.expires_at <= current and not (
        source.replica_state == "DRAINING" and source.no_in_flight and target_state == "SERVING"
    ):
        raise MetadataConflict("stale authorization membership cannot be renewed")
    if source.replica_state == "SERVING" and target_state == "DRAINING":
        if not target_no_in_flight:
            raise MetadataConflict("authorization drain acknowledgement is incomplete")
    elif source.replica_state == "DRAINING" and target_state == "SERVING":
        if not source.no_in_flight:
            raise MetadataConflict("authorization membership is not fully drained")
    elif source.replica_state != target_state:
        raise MetadataConflict("authorization membership transition is invalid")

    keyring_raw = files["keyring.json"]
    keyring = _closed_json(
        keyring_raw,
        frozenset(
            {
                "version",
                "keyring_id",
                "cell_id",
                "logical_vault_id",
                "active_key_id",
                "accepted_keys",
            }
        ),
        label="keyring",
    )
    entries = keyring["accepted_keys"]
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise MetadataConflict("authorization keyring is invalid")
    entry = entries[0]
    try:
        encoded_key = entry["key"]
        if not isinstance(encoded_key, str):
            raise ValueError
        key = base64.b64decode(encoded_key.encode("ascii") + b"=", altchars=b"-_", validate=True)
    except (KeyError, UnicodeEncodeError, ValueError) as error:
        raise MetadataConflict("authorization keyring is invalid") from error
    if len(key) != 32:
        raise MetadataConflict("authorization keyring is invalid")
    control = _closed_json(
        files["control.json"],
        frozenset(
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
        ),
        label="control",
    )
    control.pop("mac")
    epoch = source.epoch + 1
    key_id = _required_identifier(keyring["active_key_id"])
    accepted_key_ids = sorted(
        _required_identifier(item["key_id"]) for item in entries if isinstance(item, dict)
    )
    if accepted_key_ids != [key_id]:
        raise MetadataConflict("authorization keyring is invalid")
    if runtime_attestation is None:
        expires_at = current + ttl_seconds
        attestation: dict[str, object] = {
            "version": 1,
            "epoch": epoch,
            "replica_id": _required_identifier(expected_replica_id),
            "state": target_state,
            "software_version": target_release,
            "schema_version": _required_time(expected_schema_version),
            "cell_id": _required_identifier(expected_cell_id),
            "active_key_id": key_id,
            "accepted_key_ids": accepted_key_ids,
            "control_digest": _control_basis_digest(control),
            "keyring_digest": _keyring_digest(keyring),
            "attested_at": current,
            "expires_at": expires_at,
            "issuance_stopped": target_state == "DRAINING",
            "no_in_flight": target_no_in_flight,
            "signing_key_id": key_id,
        }
        attestation["mac"] = _mac(key, _attestation_mac_input(attestation))
    else:
        attestation = _closed_json(
            runtime_attestation,
            frozenset(
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
            ),
            label="runtime attestation",
        )
        try:
            attested_at = _required_time(attestation["attested_at"])
            expires_at = _required_time(attestation["expires_at"])
            supplied_mac = attestation["mac"]
            if (
                attestation["version"] != 1
                or attestation["epoch"] != epoch
                or attestation["replica_id"] != _required_identifier(expected_replica_id)
                or attestation["state"] != target_state
                or attestation["software_version"] != target_release
                or attestation["schema_version"] != _required_time(expected_schema_version)
                or attestation["cell_id"] != _required_identifier(expected_cell_id)
                or attestation["active_key_id"] != key_id
                or attestation["accepted_key_ids"] != accepted_key_ids
                or attestation["control_digest"] != _control_basis_digest(control)
                or attestation["keyring_digest"] != _keyring_digest(keyring)
                or not attested_at <= current < expires_at
                or expires_at - attested_at > ttl_seconds
                or expires_at - attested_at > MAX_ATTESTATION_TTL_SECONDS
                or expires_at > _required_time(entry["not_after"])
                or attestation["issuance_stopped"] is not (target_state == "DRAINING")
                or attestation["no_in_flight"] is not target_no_in_flight
                or attestation["signing_key_id"] != key_id
                or not isinstance(supplied_mac, str)
                or not hmac.compare_digest(
                    supplied_mac,
                    _mac(key, _attestation_mac_input(attestation)),
                )
            ):
                raise MetadataConflict("authorization runtime attestation is invalid")
        except (KeyError, TypeError, ValueError):
            raise MetadataConflict("authorization runtime attestation is invalid") from None
    membership: dict[str, object] = {
        "version": 1,
        "epoch": epoch,
        "cell_id": expected_cell_id,
        "logical_vault_id": expected_logical_vault_id,
        "previous_epoch_digest": source.membership_digest,
        "issued_at": attestation["attested_at"],
        "expires_at": expires_at,
        "replicas": [attestation],
        "signing_key_id": key_id,
    }
    membership["mac"] = _mac(key, _membership_mac_input(membership))
    membership_raw = _canonical(membership)
    membership_digest = hashlib.sha256(membership_raw).hexdigest()
    control.update(
        {
            "serving_membership_epoch": epoch,
            "serving_membership_digest": membership_digest,
            "issued_at": attestation["attested_at"],
            "expires_at": expires_at,
        }
    )
    control["mac"] = _mac(key, _control_mac_input(control))
    control_raw = _canonical(control)
    return HostedAuthorizationBundle(
        keyring=keyring_raw,
        control=control_raw,
        membership=membership_raw,
        epoch=epoch,
        membership_digest=membership_digest,
        revision=hashlib.sha256(keyring_raw + control_raw + membership_raw).hexdigest(),
        registry_attachment_id=source.registry_attachment_id,
        expires_at=expires_at,
        replica_state=target_state,
        software_version=target_release,
        issuance_stopped=target_state == "DRAINING",
        no_in_flight=target_no_in_flight,
    )
