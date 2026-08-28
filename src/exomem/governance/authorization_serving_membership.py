"""Authoritative serving-membership epochs for authorization sessions.

The record is external control-plane state, not a view inferred from process
liveness.  Every admitted replica signs its own current readiness statement and
the control-plane signer authenticates the complete epoch.  Runtime evaluation
is deliberately content-free: it yields only readiness, epoch, and counts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

MAX_MEMBERSHIP_BYTES = 64 * 1024
MAX_SERVING_REPLICAS = 64
MAX_ATTESTATION_TTL_SECONDS = 3_630
_MAX_SIGNED_INTEGER = (1 << 63) - 1
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,511}\Z")
_STATES = frozenset({"SERVING", "DRAINING"})
_RECORD_FIELDS = frozenset(
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
_ATTESTATION_MAC_DOMAIN = b"exomem.authorization-session.replica-readiness/v1"
_RECORD_MAC_DOMAIN = b"exomem.authorization-session.serving-membership/v1"


class ServingMembershipUnavailable(RuntimeError):
    """Content-free refusal for malformed, stale, or contradictory membership."""

    code = "AUTHORIZATION_MEMBERSHIP_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("authorization serving membership is unavailable")


@dataclass(frozen=True, slots=True)
class ReplicaReadinessAttestation:
    version: int
    epoch: int
    replica_id: str
    state: str
    software_version: str
    schema_version: int
    cell_id: str
    active_key_id: str
    accepted_key_ids: tuple[str, ...]
    control_digest: str
    keyring_digest: str
    attested_at: int
    expires_at: int
    issuance_stopped: bool
    no_in_flight: bool
    signing_key_id: str


@dataclass(frozen=True, slots=True)
class ServingMembershipEpoch:
    version: int
    epoch: int
    cell_id: str
    logical_vault_id: str
    previous_epoch_digest: str | None
    issued_at: int
    expires_at: int
    replicas: tuple[ReplicaReadinessAttestation, ...]
    signing_key_id: str
    record_digest: str = field(default="", compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ServingMembershipReadiness:
    ready: bool
    code: str
    epoch: int | None
    serving_replicas: int
    draining_replicas: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.ready, bool)
            or self.code
            not in {
                "AUTHORIZATION_MEMBERSHIP_READY",
                "AUTHORIZATION_MEMBERSHIP_UNAVAILABLE",
            }
            or (self.ready and self.code != "AUTHORIZATION_MEMBERSHIP_READY")
            or (not self.ready and self.code != "AUTHORIZATION_MEMBERSHIP_UNAVAILABLE")
            or (
                self.epoch is not None
                and (
                    isinstance(self.epoch, bool)
                    or not isinstance(self.epoch, int)
                    or self.epoch < 1
                )
            )
            or isinstance(self.serving_replicas, bool)
            or not isinstance(self.serving_replicas, int)
            or not 0 <= self.serving_replicas <= MAX_SERVING_REPLICAS
            or isinstance(self.draining_replicas, bool)
            or not isinstance(self.draining_replicas, int)
            or not 0 <= self.draining_replicas <= MAX_SERVING_REPLICAS
            or self.serving_replicas + self.draining_replicas > MAX_SERVING_REPLICAS
        ):
            raise ServingMembershipUnavailable

    def as_public_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "code": self.code,
            "servingMembershipEpoch": self.epoch,
            "servingReplicaCount": self.serving_replicas,
            "drainingReplicaCount": self.draining_replicas,
        }


def unavailable_readiness() -> ServingMembershipReadiness:
    return ServingMembershipReadiness(
        ready=False,
        code="AUTHORIZATION_MEMBERSHIP_UNAVAILABLE",
        epoch=None,
        serving_replicas=0,
        draining_replicas=0,
    )


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ServingMembershipUnavailable
        result[name] = value
    return result


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ServingMembershipUnavailable
    return value


def _integer(value: object, *, minimum: int = 1) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_SIGNED_INTEGER
    ):
        raise ServingMembershipUnavailable
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise ServingMembershipUnavailable
    return value


def _key_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ServingMembershipUnavailable
    result = tuple(_identifier(item) for item in value)
    if result != tuple(sorted(set(result))):
        raise ServingMembershipUnavailable
    return result


def _decode_mac(value: object) -> bytes:
    if not isinstance(value, str) or len(value) != 43:
        raise ServingMembershipUnavailable
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded + b"=", altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise ServingMembershipUnavailable from None
    if (
        len(decoded) != 32
        or base64.urlsafe_b64encode(decoded).rstrip(b"=") != encoded
    ):
        raise ServingMembershipUnavailable
    return decoded


def _framed(domain: bytes, fields: tuple[bytes, ...]) -> bytes:
    output = bytearray(domain)
    output.append(0)
    for value in fields:
        output.extend(len(value).to_bytes(4, "big"))
        output.extend(value)
    return bytes(output)


def _attestation_value(
    attestation: ReplicaReadinessAttestation,
) -> dict[str, object]:
    return {
        "version": attestation.version,
        "epoch": attestation.epoch,
        "replica_id": attestation.replica_id,
        "state": attestation.state,
        "software_version": attestation.software_version,
        "schema_version": attestation.schema_version,
        "cell_id": attestation.cell_id,
        "active_key_id": attestation.active_key_id,
        "accepted_key_ids": list(attestation.accepted_key_ids),
        "control_digest": attestation.control_digest,
        "keyring_digest": attestation.keyring_digest,
        "attested_at": attestation.attested_at,
        "expires_at": attestation.expires_at,
        "issuance_stopped": attestation.issuance_stopped,
        "no_in_flight": attestation.no_in_flight,
        "signing_key_id": attestation.signing_key_id,
    }


def _attestation_mac_input(value: Mapping[str, object]) -> bytes:
    accepted = value["accepted_key_ids"]
    if not isinstance(accepted, list):
        raise ServingMembershipUnavailable
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
            json.dumps(accepted, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            str(value["control_digest"]).encode("ascii"),
            str(value["keyring_digest"]).encode("ascii"),
            str(value["attested_at"]).encode("ascii"),
            str(value["expires_at"]).encode("ascii"),
            b"true" if value["issuance_stopped"] is True else b"false",
            b"true" if value["no_in_flight"] is True else b"false",
            str(value["signing_key_id"]).encode("utf-8"),
        ),
    )


def _signed_attestation_value(
    attestation: ReplicaReadinessAttestation,
    *,
    verifier_keys: Mapping[str, bytes],
) -> dict[str, object]:
    value = _attestation_value(attestation)
    key = verifier_keys.get(attestation.signing_key_id)
    if not isinstance(key, bytes) or len(key) != 32:
        raise ServingMembershipUnavailable
    value["mac"] = (
        base64.urlsafe_b64encode(
            hmac.new(key, _attestation_mac_input(value), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return value


def _validate_attestation_shape(
    attestation: ReplicaReadinessAttestation,
    *,
    now: int | None,
) -> None:
    if not isinstance(attestation, ReplicaReadinessAttestation) or attestation.version != 1:
        raise ServingMembershipUnavailable
    _integer(attestation.epoch)
    _identifier(attestation.replica_id)
    if attestation.state not in _STATES:
        raise ServingMembershipUnavailable
    _identifier(attestation.software_version)
    _integer(attestation.schema_version)
    _identifier(attestation.cell_id)
    active_key_id = _identifier(attestation.active_key_id)
    accepted_key_ids = tuple(_identifier(item) for item in attestation.accepted_key_ids)
    signing_key_id = _identifier(attestation.signing_key_id)
    attested_at = _integer(attestation.attested_at)
    expires_at = _integer(attestation.expires_at)
    if (
        not 1 <= len(accepted_key_ids) <= 32
        or accepted_key_ids != tuple(sorted(set(accepted_key_ids)))
        or active_key_id not in accepted_key_ids
        or signing_key_id != active_key_id
        or attested_at >= expires_at
        or expires_at - attested_at > MAX_ATTESTATION_TTL_SECONDS
        or (now is not None and not attested_at <= now < expires_at)
        or not isinstance(attestation.issuance_stopped, bool)
        or not isinstance(attestation.no_in_flight, bool)
        or (
            attestation.state == "SERVING"
            and (attestation.issuance_stopped or attestation.no_in_flight)
        )
        or (attestation.state == "DRAINING" and not attestation.issuance_stopped)
    ):
        raise ServingMembershipUnavailable
    _digest(attestation.control_digest)
    _digest(attestation.keyring_digest)


def encode_replica_readiness_attestation(
    attestation: ReplicaReadinessAttestation,
    *,
    verifier_keys: Mapping[str, bytes],
) -> bytes:
    """Return canonical authenticated bytes for one runtime readiness proof."""

    _validate_attestation_shape(attestation, now=None)
    encoded = json.dumps(
        _signed_attestation_value(attestation, verifier_keys=verifier_keys),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not 1 <= len(encoded) <= MAX_MEMBERSHIP_BYTES:
        raise ServingMembershipUnavailable
    return encoded


def _record_value(
    record: ServingMembershipEpoch,
    *,
    verifier_keys: Mapping[str, bytes],
) -> dict[str, object]:
    return {
        "version": record.version,
        "epoch": record.epoch,
        "cell_id": record.cell_id,
        "logical_vault_id": record.logical_vault_id,
        "previous_epoch_digest": record.previous_epoch_digest,
        "issued_at": record.issued_at,
        "expires_at": record.expires_at,
        "replicas": [
            _signed_attestation_value(item, verifier_keys=verifier_keys)
            for item in record.replicas
        ],
        "signing_key_id": record.signing_key_id,
    }


def _record_mac_input(value: Mapping[str, object]) -> bytes:
    replicas = value["replicas"]
    previous = value["previous_epoch_digest"]
    return _framed(
        _RECORD_MAC_DOMAIN,
        (
            str(value["version"]).encode("ascii"),
            str(value["epoch"]).encode("ascii"),
            str(value["cell_id"]).encode("utf-8"),
            str(value["logical_vault_id"]).encode("utf-8"),
            b"" if previous is None else str(previous).encode("ascii"),
            str(value["issued_at"]).encode("ascii"),
            str(value["expires_at"]).encode("ascii"),
            json.dumps(
                replicas,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            str(value["signing_key_id"]).encode("utf-8"),
        ),
    )


def encode_serving_membership(
    record: ServingMembershipEpoch,
    *,
    verifier_keys: Mapping[str, bytes],
) -> bytes:
    """Return canonical authenticated bytes for one control-plane epoch."""

    _validate_record_shape(record, now=None)
    value = _record_value(record, verifier_keys=verifier_keys)
    key = verifier_keys.get(record.signing_key_id)
    if not isinstance(key, bytes) or len(key) != 32:
        raise ServingMembershipUnavailable
    value["mac"] = (
        base64.urlsafe_b64encode(
            hmac.new(key, _record_mac_input(value), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not 1 <= len(encoded) <= MAX_MEMBERSHIP_BYTES:
        raise ServingMembershipUnavailable
    return encoded


def serving_membership_digest(raw: bytes) -> str:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_MEMBERSHIP_BYTES:
        raise ServingMembershipUnavailable
    return hashlib.sha256(raw).hexdigest()


def _parse_attestation(
    value: object,
    *,
    verifier_keys: Mapping[str, bytes],
    now: int,
    expected_epoch: int,
    expected_cell_id: str,
) -> ReplicaReadinessAttestation:
    if not isinstance(value, dict) or set(value) != _ATTESTATION_FIELDS:
        raise ServingMembershipUnavailable
    version = value["version"]
    if isinstance(version, bool) or version != 1:
        raise ServingMembershipUnavailable
    epoch = _integer(value["epoch"])
    state = value["state"]
    if not isinstance(state, str) or state not in _STATES:
        raise ServingMembershipUnavailable
    attested_at = _integer(value["attested_at"])
    expires_at = _integer(value["expires_at"])
    if (
        epoch != expected_epoch
        or _identifier(value["cell_id"]) != expected_cell_id
        or not attested_at <= now < expires_at
        or expires_at - attested_at > MAX_ATTESTATION_TTL_SECONDS
    ):
        raise ServingMembershipUnavailable
    issuance_stopped = value["issuance_stopped"]
    no_in_flight = value["no_in_flight"]
    if not isinstance(issuance_stopped, bool) or not isinstance(no_in_flight, bool):
        raise ServingMembershipUnavailable
    if (state == "SERVING" and (issuance_stopped or no_in_flight)) or (
        state == "DRAINING" and not issuance_stopped
    ):
        raise ServingMembershipUnavailable
    active_key_id = _identifier(value["active_key_id"])
    accepted_key_ids = _key_ids(value["accepted_key_ids"])
    signing_key_id = _identifier(value["signing_key_id"])
    if active_key_id not in accepted_key_ids or signing_key_id != active_key_id:
        raise ServingMembershipUnavailable
    key = verifier_keys.get(signing_key_id)
    if not isinstance(key, bytes) or len(key) != 32:
        raise ServingMembershipUnavailable
    supplied = _decode_mac(value["mac"])
    if not hmac.compare_digest(
        supplied,
        hmac.new(key, _attestation_mac_input(value), hashlib.sha256).digest(),
    ):
        raise ServingMembershipUnavailable
    return ReplicaReadinessAttestation(
        version=1,
        epoch=epoch,
        replica_id=_identifier(value["replica_id"]),
        state=state,
        software_version=_identifier(value["software_version"]),
        schema_version=_integer(value["schema_version"]),
        cell_id=expected_cell_id,
        active_key_id=active_key_id,
        accepted_key_ids=accepted_key_ids,
        control_digest=_digest(value["control_digest"]),
        keyring_digest=_digest(value["keyring_digest"]),
        attested_at=attested_at,
        expires_at=expires_at,
        issuance_stopped=issuance_stopped,
        no_in_flight=no_in_flight,
        signing_key_id=signing_key_id,
    )


def parse_replica_readiness_attestation(
    raw: bytes,
    *,
    verifier_keys: Mapping[str, bytes],
    now: int,
    expected_epoch: int,
    expected_cell_id: str,
) -> ReplicaReadinessAttestation:
    """Authenticate exact canonical bytes emitted by one admitted replica."""

    try:
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_MEMBERSHIP_BYTES:
            raise ServingMembershipUnavailable
        current = _integer(now)
        epoch = _integer(expected_epoch)
        cell_id = _identifier(expected_cell_id)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
        parsed = _parse_attestation(
            value,
            verifier_keys=verifier_keys,
            now=current,
            expected_epoch=epoch,
            expected_cell_id=cell_id,
        )
        _validate_attestation_shape(parsed, now=current)
        if encode_replica_readiness_attestation(
            parsed,
            verifier_keys=verifier_keys,
        ) != raw:
            raise ServingMembershipUnavailable
        return parsed
    except ServingMembershipUnavailable:
        raise
    except (
        AttributeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise ServingMembershipUnavailable from None


def parse_serving_membership(
    raw: bytes,
    *,
    verifier_keys: Mapping[str, bytes],
    now: int,
    expected_cell_id: str,
    expected_logical_vault_id: str,
    expected_epoch: int,
    expected_digest: str,
) -> ServingMembershipEpoch:
    """Authenticate one exact, current serving-membership epoch."""

    try:
        current = _integer(now)
        digest = serving_membership_digest(raw)
        if digest != _digest(expected_digest):
            raise ServingMembershipUnavailable
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
        if not isinstance(value, dict) or set(value) != _RECORD_FIELDS:
            raise ServingMembershipUnavailable
        if value["version"] != 1 or isinstance(value["version"], bool):
            raise ServingMembershipUnavailable
        epoch = _integer(value["epoch"])
        cell_id = _identifier(value["cell_id"])
        logical_vault_id = _identifier(value["logical_vault_id"])
        if (
            epoch != _integer(expected_epoch)
            or cell_id != _identifier(expected_cell_id)
            or logical_vault_id != _identifier(expected_logical_vault_id)
        ):
            raise ServingMembershipUnavailable
        previous = value["previous_epoch_digest"]
        if epoch == 1:
            if previous is not None:
                raise ServingMembershipUnavailable
        else:
            previous = _digest(previous)
        issued_at = _integer(value["issued_at"])
        expires_at = _integer(value["expires_at"])
        if (
            not issued_at <= current < expires_at
            or expires_at - issued_at > MAX_ATTESTATION_TTL_SECONDS
        ):
            raise ServingMembershipUnavailable
        raw_replicas = value["replicas"]
        if (
            not isinstance(raw_replicas, list)
            or not 1 <= len(raw_replicas) <= MAX_SERVING_REPLICAS
        ):
            raise ServingMembershipUnavailable
        replicas = tuple(
            _parse_attestation(
                item,
                verifier_keys=verifier_keys,
                now=current,
                expected_epoch=epoch,
                expected_cell_id=cell_id,
            )
            for item in raw_replicas
        )
        replica_ids = tuple(item.replica_id for item in replicas)
        if replica_ids != tuple(sorted(set(replica_ids))):
            raise ServingMembershipUnavailable
        signing_key_id = _identifier(value["signing_key_id"])
        signing_key = verifier_keys.get(signing_key_id)
        if not isinstance(signing_key, bytes) or len(signing_key) != 32:
            raise ServingMembershipUnavailable
        if not hmac.compare_digest(
            _decode_mac(value["mac"]),
            hmac.new(
                signing_key,
                _record_mac_input(value),
                hashlib.sha256,
            ).digest(),
        ):
            raise ServingMembershipUnavailable
        record = ServingMembershipEpoch(
            version=1,
            epoch=epoch,
            cell_id=cell_id,
            logical_vault_id=logical_vault_id,
            previous_epoch_digest=previous,
            issued_at=issued_at,
            expires_at=expires_at,
            replicas=replicas,
            signing_key_id=signing_key_id,
            record_digest=digest,
        )
        _validate_record_shape(record, now=current)
        if encode_serving_membership(record, verifier_keys=verifier_keys) != raw:
            raise ServingMembershipUnavailable
        return record
    except ServingMembershipUnavailable:
        raise
    except (
        AttributeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise ServingMembershipUnavailable from None


def _validate_record_shape(
    record: ServingMembershipEpoch,
    *,
    now: int | None,
) -> None:
    if not isinstance(record, ServingMembershipEpoch) or record.version != 1:
        raise ServingMembershipUnavailable
    epoch = _integer(record.epoch)
    _identifier(record.cell_id)
    _identifier(record.logical_vault_id)
    if epoch == 1:
        if record.previous_epoch_digest is not None:
            raise ServingMembershipUnavailable
    else:
        _digest(record.previous_epoch_digest)
    issued_at = _integer(record.issued_at)
    expires_at = _integer(record.expires_at)
    if (
        issued_at >= expires_at
        or expires_at - issued_at > MAX_ATTESTATION_TTL_SECONDS
        or (now is not None and not issued_at <= now < expires_at)
        or not 1 <= len(record.replicas) <= MAX_SERVING_REPLICAS
    ):
        raise ServingMembershipUnavailable
    identifiers = tuple(item.replica_id for item in record.replicas)
    if identifiers != tuple(sorted(set(identifiers))):
        raise ServingMembershipUnavailable
    _identifier(record.signing_key_id)
    for item in record.replicas:
        _validate_attestation_shape(item, now=now)
        if (
            item.epoch != epoch
            or item.cell_id != record.cell_id
            or item.attested_at > issued_at
            or item.expires_at < expires_at
        ):
            raise ServingMembershipUnavailable


def evaluate_serving_membership(
    record: ServingMembershipEpoch,
    *,
    now: int,
    local_replica_id: str,
    local_software_version: str,
    local_schema_version: int,
    expected_cell_id: str,
    expected_control_digest: str,
    expected_keyring_digest: str,
    local_active_key_id: str,
    local_accepted_key_ids: tuple[str, ...],
    valid_verifier_key_ids: tuple[str, ...],
    live_verifier_key_ids: tuple[str, ...],
) -> ServingMembershipReadiness:
    """Evaluate issuance/resumption readiness without exposing member details."""

    serving = sum(item.state == "SERVING" for item in record.replicas)
    draining = len(record.replicas) - serving
    unavailable = ServingMembershipReadiness(
        ready=False,
        code="AUTHORIZATION_MEMBERSHIP_UNAVAILABLE",
        epoch=record.epoch if isinstance(record.epoch, int) else None,
        serving_replicas=serving,
        draining_replicas=draining,
    )
    try:
        current = _integer(now)
        _validate_record_shape(record, now=current)
        local_id = _identifier(local_replica_id)
        cell_id = _identifier(expected_cell_id)
        control_digest = _digest(expected_control_digest)
        keyring_digest = _digest(expected_keyring_digest)
        active_key = _identifier(local_active_key_id)
        accepted_keys = tuple(_identifier(item) for item in local_accepted_key_ids)
        valid_keys = tuple(_identifier(item) for item in valid_verifier_key_ids)
        live_keys = tuple(_identifier(item) for item in live_verifier_key_ids)
        if (
            accepted_keys != tuple(sorted(set(accepted_keys)))
            or valid_keys != tuple(sorted(set(valid_keys)))
        ):
            return unavailable
        local = next((item for item in record.replicas if item.replica_id == local_id), None)
        if (
            record.cell_id != cell_id
            or local is None
            or local.state != "SERVING"
            or local.software_version != _identifier(local_software_version)
            or local.schema_version != _integer(local_schema_version)
            or local.active_key_id != active_key
            or local.accepted_key_ids != accepted_keys
        ):
            return unavailable
        serving_members = tuple(item for item in record.replicas if item.state == "SERVING")
        if not serving_members:
            return unavailable
        for item in record.replicas:
            if (
                item.epoch != record.epoch
                or item.cell_id != cell_id
                or not item.attested_at <= current < item.expires_at
                or item.control_digest != control_digest
                or item.keyring_digest != keyring_digest
            ):
                return unavailable
        intersection = set(serving_members[0].accepted_key_ids)
        for item in serving_members[1:]:
            intersection.intersection_update(item.accepted_key_ids)
        active_keys = {item.active_key_id for item in serving_members}
        if (
            not active_keys.issubset(intersection)
            or not intersection.issubset(set(accepted_keys))
            or not set(live_keys).issubset(intersection)
            or not active_keys.issubset(set(valid_keys))
            or record.signing_key_id not in valid_keys
            or not set(live_keys).issubset(set(valid_keys))
        ):
            return unavailable
        return ServingMembershipReadiness(
            ready=True,
            code="AUTHORIZATION_MEMBERSHIP_READY",
            epoch=record.epoch,
            serving_replicas=serving,
            draining_replicas=draining,
        )
    except (AttributeError, TypeError, ValueError, ServingMembershipUnavailable):
        return unavailable


def validate_membership_successor(
    previous: ServingMembershipEpoch,
    current: ServingMembershipEpoch,
    *,
    now: int,
) -> None:
    """Enforce explicit epoch transitions; silence can never remove a replica."""

    try:
        _validate_record_shape(previous, now=None)
        _validate_record_shape(current, now=now)
        if (
            not previous.record_digest
            or current.epoch != previous.epoch + 1
            or current.previous_epoch_digest != previous.record_digest
            or current.cell_id != previous.cell_id
            or current.logical_vault_id != previous.logical_vault_id
            or current.issued_at < previous.issued_at
        ):
            raise ServingMembershipUnavailable
        before = {item.replica_id: item for item in previous.replicas}
        after = {item.replica_id: item for item in current.replicas}
        for replica_id, prior in before.items():
            successor = after.get(replica_id)
            if successor is None:
                if not (
                    prior.state == "DRAINING"
                    and prior.issuance_stopped
                    and prior.no_in_flight
                ):
                    raise ServingMembershipUnavailable
                continue
            if prior.state == "SERVING" and successor.state == "DRAINING":
                if not successor.issuance_stopped:
                    raise ServingMembershipUnavailable
            elif prior.state == "DRAINING" and successor.state == "SERVING":
                if successor.epoch != current.epoch:
                    raise ServingMembershipUnavailable
            elif prior.state != successor.state:
                raise ServingMembershipUnavailable
            if prior.no_in_flight and successor.state == "DRAINING" and not successor.no_in_flight:
                raise ServingMembershipUnavailable
    except ServingMembershipUnavailable:
        raise
    except (AttributeError, TypeError, ValueError):
        raise ServingMembershipUnavailable from None
