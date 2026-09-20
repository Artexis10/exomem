"""Authenticated custody snapshots and monotonic hosted delivery decisions."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from .governance import authorization_custody as custody
from .governance import authorization_serving_membership as membership
from .hosted_activation_ack_protocol import ProtocolError, encode_message

FILENAMES = frozenset({"keyring.json", "control.json", "serving-membership.json"})


class CustodyDeliveryUnavailable(RuntimeError):
    """No verified whole generation is available for installation."""

    code = "ACK_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("hosted custody delivery is unavailable")


class CustodyKeyringUnavailable(CustodyDeliveryUnavailable):
    code = "KEYRING_NOT_AVAILABLE"


@dataclass(frozen=True, slots=True)
class VerifiedDeliveryBundle:
    files: Mapping[str, bytes] = field(repr=False)
    keyring: custody.AuthorizationKeyring
    control: custody.AuthorizationControlRecord
    membership: membership.ServingMembershipEpoch
    revision: str


def require_serving_delivery(bundle: VerifiedDeliveryBundle) -> None:
    """Require usable current-release v4 custody after signature validation."""
    from . import __version__

    replica = bundle.membership.replicas[0]
    if (
        not bundle.control.governance_enrolled
        or bundle.control.activation_store_id is None
        or bundle.control.activation_epoch is None
        or bundle.control.activation_state_digest is None
        or replica.state != "SERVING"
        or replica.issuance_stopped
        or replica.no_in_flight
        or replica.schema_version != 4
        or replica.software_version != __version__
    ):
        raise CustodyDeliveryUnavailable


def parse_delivery_bundle(
    files: Mapping[str, bytes], *, now: int, historical: bool = False
) -> VerifiedDeliveryBundle:
    """Verify complete signed custody; historical mode only supports ordering.

    Historical records authenticate at their original issuance time. They may
    establish a previous generation's floor but never authorize serving or a
    successful acknowledgement at the current time.
    """
    try:
        if (
            set(files) != FILENAMES
            or isinstance(now, bool)
            or not isinstance(now, int)
            or now < 1
            or any(
                not isinstance(raw, bytes) or not 1 <= len(raw) <= 65536 for raw in files.values()
            )
        ):
            raise CustodyDeliveryUnavailable
        snapshot = dict(files)
        keyring = custody.parse_keyring(snapshot["keyring.json"])
        moment = json.loads(snapshot["control.json"])["issued_at"] if historical else now
        if isinstance(moment, bool) or not isinstance(moment, int) or moment > now:
            raise CustodyDeliveryUnavailable
        control = custody._parse_control_record(
            snapshot["control.json"], keyring=keyring, now=moment, allow_expired=historical
        )
        record = membership._parse_serving_membership(
            snapshot["serving-membership.json"],
            verifier_keys={item.key_id: item.key for item in keyring.accepted_keys},
            now=moment,
            expected_cell_id=control.cell_id,
            expected_logical_vault_id=control.logical_vault_id,
            expected_epoch=control.serving_membership_epoch,
            expected_digest=control.serving_membership_digest,
            allow_expired=historical,
        )
        if (
            control.signing_key_id != keyring.active_key_id
            or record.signing_key_id != keyring.active_key_id
            or len(record.replicas) != 1
            or record.issued_at != control.issued_at
            or record.expires_at != control.expires_at
        ):
            raise CustodyDeliveryUnavailable
        replica = record.replicas[0]
        if (
            replica.active_key_id != keyring.active_key_id
            or replica.signing_key_id != keyring.active_key_id
            or replica.accepted_key_ids
            != tuple(sorted(key.key_id for key in keyring.accepted_keys))
            or replica.control_digest != custody.control_attestation_digest(control)
            or replica.keyring_digest != custody.keyring_attestation_digest(keyring)
        ):
            raise CustodyDeliveryUnavailable
        revision = hashlib.sha256(
            snapshot["keyring.json"]
            + snapshot["control.json"]
            + snapshot["serving-membership.json"]
        ).hexdigest()
        return VerifiedDeliveryBundle(
            MappingProxyType(snapshot), keyring, control, record, revision
        )
    except CustodyDeliveryUnavailable:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        raise CustodyDeliveryUnavailable from None


def generation_relation(current: VerifiedDeliveryBundle, candidate: VerifiedDeliveryBundle) -> str:
    """Compare authenticated counters without combining records from generations."""
    before, after = current.control, candidate.control
    identity = ("cell_id", "logical_vault_id", "registry_attachment_id", "attachment_epoch")
    if any(getattr(before, name) != getattr(after, name) for name in identity):
        raise CustodyDeliveryUnavailable
    if (
        before.activation_store_id is not None
        and after.activation_store_id is not None
        and before.activation_store_id != after.activation_store_id
    ):
        raise CustodyDeliveryUnavailable
    if (
        before.activation_epoch == after.activation_epoch
        and before.activation_state_digest != after.activation_state_digest
    ) or (
        before.serving_membership_epoch == after.serving_membership_epoch
        and before.serving_membership_digest != after.serving_membership_digest
    ):
        raise CustodyDeliveryUnavailable
    old = (
        before.activation_epoch or 0,
        before.serving_membership_epoch,
        before.vocabulary_authority_floor,
    )
    new = (
        after.activation_epoch or 0,
        after.serving_membership_epoch,
        after.vocabulary_authority_floor,
    )
    higher = any(right > left for left, right in zip(old, new, strict=True))
    lower = any(right < left for left, right in zip(old, new, strict=True))
    if higher and lower:
        return "incomparable"
    if lower:
        return "stale"
    if higher:
        return "advance"
    if candidate.files != current.files:
        raise CustodyDeliveryUnavailable
    return "same"


def public_response_bundle(
    response: Mapping[str, object], *, keyrings: Sequence[bytes], now: int
) -> VerifiedDeliveryBundle:
    """Verify response records only with already projected matching key bytes."""
    try:
        encode_message(response, "httpBundleResponse")
        matching = [
            raw for raw in keyrings if hashlib.sha256(raw).hexdigest() == response["keyring_sha256"]
        ]
        if not matching:
            raise CustodyKeyringUnavailable

        def decode(value: str) -> bytes:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

        bundle = parse_delivery_bundle(
            {
                "keyring.json": matching[0],
                "control.json": decode(response["control_b64"]),
                "serving-membership.json": decode(response["serving_membership_b64"]),
            },
            now=now,
        )
        if bundle.revision != response["bundle_revision"] or any(
            getattr(bundle.control, name) != response[name]
            for name in (
                "cell_id",
                "logical_vault_id",
                "registry_attachment_id",
                "attachment_epoch",
            )
        ):
            raise CustodyDeliveryUnavailable
        return bundle
    except CustodyDeliveryUnavailable:
        raise
    except (ProtocolError, RuntimeError, ValueError, TypeError, KeyError):
        raise CustodyDeliveryUnavailable from None
