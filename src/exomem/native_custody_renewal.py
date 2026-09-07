"""Mechanical same-authority renewal for the private native deployment controller.

The caller establishes durable owner permission for renewal. Historical checks
below authenticate recovery inputs only; they never admit an expired record to
serving. The only returned custody is verified at the caller's actual clock.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from . import reserved_paths, writer_lease
from .governance import authorization_custody as custody
from .governance import authorization_serving_membership as membership

CHECKPOINT_NAME = "standalone-custody-renewal-v1.json"
_BYTE_FIELDS = frozenset(
    {"source_control", "target_control", "source_membership", "target_membership"}
)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True, slots=True)
class _RenewalPlan:
    vault_root: str = field(repr=False)
    keyring_path: str = field(repr=False)
    control_path: str = field(repr=False)
    membership_path: str = field(repr=False)
    attachment_id: str
    replica_id: str
    software_version: str
    keyring_digest: str
    source_control: bytes = field(repr=False)
    source_membership: bytes = field(repr=False)
    target_control: bytes = field(repr=False)
    target_membership: bytes = field(repr=False)
    completed: bool = False

    def encode(self) -> bytes:
        value: dict[str, Any] = {"version": 1}
        for item in fields(self):
            raw = getattr(self, item.name)
            value[item.name] = (
                base64.b64encode(raw).decode("ascii") if item.name in _BYTE_FIELDS else raw
            )
        value["digest"] = _digest(_json(value))
        encoded = _json(value)
        if len(encoded) > custody.MAX_CUSTODY_FILE_BYTES:
            raise custody.AuthorizationCustodyUnavailable
        return encoded


def _decode_checkpoint(raw: bytes) -> _RenewalPlan:
    try:
        value = json.loads(raw, object_pairs_hook=custody._closed_object)
        if (
            not isinstance(value, dict)
            or set(value) != {item.name for item in fields(_RenewalPlan)} | {"version", "digest"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["completed"]) is not bool
        ):
            raise custody.AuthorizationCustodyUnavailable
        digest = value.pop("digest")
        if digest != _digest(_json(value)):
            raise custody.AuthorizationCustodyUnavailable
        del value["version"]
        for name, item in value.items():
            if name == "completed":
                continue
            if not isinstance(item, str) or not item:
                raise custody.AuthorizationCustodyUnavailable
            if name in _BYTE_FIELDS:
                value[name] = base64.b64decode(item, validate=True)
                if not 1 <= len(value[name]) <= custody.MAX_CUSTODY_FILE_BYTES:
                    raise custody.AuthorizationCustodyUnavailable
            elif len(item) > 4096:
                raise custody.AuthorizationCustodyUnavailable
        plan = _RenewalPlan(**value)
        if plan.encode() != raw:
            raise custody.AuthorizationCustodyUnavailable
        return plan
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        raise custody.AuthorizationCustodyUnavailable from None


def _historical_issued_at(raw: bytes, *, actual_now: int) -> int:
    """Read a candidate timestamp; the caller must authenticate the whole record."""

    try:
        value = json.loads(raw, object_pairs_hook=custody._closed_object)
        issued_at = custody._bounded_time(value["issued_at"])
        if issued_at > actual_now:
            raise custody.AuthorizationCustodyUnavailable
        return issued_at
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        raise custody.AuthorizationCustodyUnavailable from None


def _historical_control(
    raw: bytes,
    *,
    keyring: custody.AuthorizationKeyring,
    actual_now: int,
) -> custody.AuthorizationControlRecord:
    """Authenticate signed history at issuance, without asserting current validity."""

    record = custody.parse_control_record(
        raw, keyring=keyring, now=_historical_issued_at(raw, actual_now=actual_now)
    )
    if (
        record.version != 2
        or record.vocabulary_authority_floor != 2
        or not record.governance_enrolled
    ):
        raise custody.AuthorizationCustodyUnavailable
    signing = next(item for item in keyring.accepted_keys if item.key_id == record.signing_key_id)
    if not signing.not_before <= actual_now < signing.not_after:
        raise custody.AuthorizationCustodyUnavailable
    return record


def _historical_membership(
    raw: bytes,
    *,
    keyring: custody.AuthorizationKeyring,
    control: custody.AuthorizationControlRecord,
    replica_id: str,
    actual_now: int,
) -> membership.ServingMembershipEpoch:
    """Authenticate a singleton's historical readiness, never its serving admission."""

    issued_at = _historical_issued_at(raw, actual_now=actual_now)
    record = custody._parse_migration_membership(
        raw,
        keyring=keyring,
        control=control,
        now=issued_at,
        epoch=control.serving_membership_epoch,
        digest=control.serving_membership_digest,
    )
    custody._require_standalone_membership(
        record,
        keyring=keyring,
        control=control,
        replica_id=replica_id,
        state="SERVING",
        no_in_flight=False,
    )
    if (
        record.signing_key_id != keyring.active_key_id
        or record.replicas[0].signing_key_id != keyring.active_key_id
        or record.issued_at < control.issued_at
    ):
        raise custody.AuthorizationCustodyUnavailable
    return record


def _renewed_expiry(
    source: custody.AuthorizationControlRecord,
    keyring: custody.AuthorizationKeyring,
    *,
    issued_at: int,
) -> int:
    signing = next(item for item in keyring.accepted_keys if item.key_id == source.signing_key_id)
    lifetime = min(source.expires_at - source.issued_at, custody._DEFAULT_KEY_TTL_SECONDS)
    return min(issued_at + lifetime, signing.not_after, keyring.active_key.not_after)


def _validate_historical_plan(
    plan: _RenewalPlan,
    *,
    root: Path,
    external: custody.ExternalAuthorizationCustody,
    keyring: custody.AuthorizationKeyring,
    membership_path: Path,
    replica_id: str,
    actual_now: int,
    require_current_activation: bool = True,
) -> tuple[
    custody.AuthorizationControlRecord,
    custody.AuthorizationControlRecord,
    membership.ServingMembershipEpoch,
]:
    """Validate the exact same-authority transition, including expired checkpoints."""

    if (
        plan.vault_root != str(root)
        or plan.keyring_path != str(external.keyring_path)
        or plan.control_path != str(external.control_path)
        or plan.membership_path != str(membership_path)
        or plan.attachment_id != custody.standalone_attachment_id(root)
        or plan.replica_id != replica_id
        or plan.software_version != custody.runtime_software_version()
        or plan.keyring_digest != _digest(external.keyring)
    ):
        raise custody.AuthorizationCustodyUnavailable
    source = _historical_control(plan.source_control, keyring=keyring, actual_now=actual_now)
    target = _historical_control(plan.target_control, keyring=keyring, actual_now=actual_now)
    if source.registry_attachment_id != plan.attachment_id or target != replace(
        source,
        issued_at=target.issued_at,
        expires_at=_renewed_expiry(source, keyring, issued_at=target.issued_at),
        serving_membership_epoch=source.serving_membership_epoch + 1,
        serving_membership_digest=membership.serving_membership_digest(plan.target_membership),
    ):
        raise custody.AuthorizationCustodyUnavailable
    previous = _historical_membership(
        plan.source_membership,
        keyring=keyring,
        control=source,
        replica_id=replica_id,
        actual_now=actual_now,
    )
    successor = _historical_membership(
        plan.target_membership,
        keyring=keyring,
        control=target,
        replica_id=replica_id,
        actual_now=actual_now,
    )
    if successor.issued_at != target.issued_at or successor.expires_at != min(
        target.expires_at, target.issued_at + membership.MAX_ATTESTATION_TTL_SECONDS
    ):
        raise custody.AuthorizationCustodyUnavailable
    try:
        # This check authenticates history only; final admission below uses actual_now.
        membership.validate_membership_successor(previous, successor, now=successor.issued_at)
    except membership.ServingMembershipUnavailable:
        raise custody.AuthorizationCustodyUnavailable from None
    if require_current_activation:
        custody._require_attachment_target_authority(
            root, control=target, now=actual_now, invalidate_sessions=False
        )
    return source, target, successor


def _new_plan(
    root: Path,
    *,
    external: custody.ExternalAuthorizationCustody,
    keyring: custody.AuthorizationKeyring,
    membership_path: Path,
    membership_raw: bytes,
    replica_id: str,
    now: int,
) -> _RenewalPlan:
    source = _historical_control(external.control, keyring=keyring, actual_now=now)
    if source.registry_attachment_id != custody.standalone_attachment_id(root):
        raise custody.AuthorizationCustodyUnavailable
    previous = _historical_membership(
        membership_raw,
        keyring=keyring,
        control=source,
        replica_id=replica_id,
        actual_now=now,
    )
    _path, _raw, registry, _host_key = custody._load_host_registry(source, now=now)
    if not custody._host_registry_matches_control(
        registry, source, state="SERVING", no_in_flight=False
    ):
        raise custody.AuthorizationCustodyUnavailable
    target = replace(
        source,
        issued_at=now,
        expires_at=_renewed_expiry(source, keyring, issued_at=now),
        serving_membership_epoch=source.serving_membership_epoch + 1,
        serving_membership_digest="0" * 64,
    )
    target_membership = custody._standalone_membership_bytes(
        keyring=keyring,
        control=target,
        replica_id=replica_id,
        previous_epoch_digest=previous.record_digest,
        attested_at=now,
    )
    successor = custody._standalone_membership_successor(
        target_membership,
        keyring=keyring,
        expected_control=source,
        target_control=target,
        replica_id=replica_id,
        target_state="SERVING",
        target_no_in_flight=False,
        now=now,
    )
    try:
        membership.validate_membership_successor(previous, successor, now=now)
    except membership.ServingMembershipUnavailable:
        raise custody.AuthorizationCustodyUnavailable from None
    target = replace(target, serving_membership_digest=successor.record_digest)
    signing = next(item for item in keyring.accepted_keys if item.key_id == target.signing_key_id)
    return _RenewalPlan(
        vault_root=str(root),
        keyring_path=str(external.keyring_path),
        control_path=str(external.control_path),
        membership_path=str(membership_path),
        attachment_id=source.registry_attachment_id,
        replica_id=replica_id,
        software_version=custody.runtime_software_version(),
        keyring_digest=_digest(external.keyring),
        source_control=external.control,
        source_membership=membership_raw,
        target_control=custody._signed_control_bytes(target, signing_key=signing.key),
        target_membership=target_membership,
    )


def _finish_checkpoint(
    root: Path,
    *,
    checkpoint: Path,
    plan: _RenewalPlan,
    encoded: bytes,
    now: int,
) -> tuple[_RenewalPlan, bytes, membership.ServingMembershipEpoch]:
    external = custody.load_external_custody(root)
    keyring = custody.parse_keyring(external.keyring)
    membership_path, observed_membership, replica_id = custody._standalone_membership_file(
        root, external=external
    )
    source, target, successor = _validate_historical_plan(
        plan,
        root=root,
        external=external,
        keyring=keyring,
        membership_path=membership_path,
        replica_id=replica_id,
        actual_now=now,
    )
    source_pair = (plan.source_control, plan.source_membership)
    partial_pair = (plan.source_control, plan.target_membership)
    target_pair = (plan.target_control, plan.target_membership)
    observed = (external.control, observed_membership)
    if observed not in (source_pair, partial_pair, target_pair) or (
        plan.completed and observed != target_pair
    ):
        raise custody.AuthorizationCustodyUnavailable
    _path, _raw, registry, _host_key = custody._load_host_registry(source, now=now)
    registry_source = custody._host_registry_matches_control(
        registry, source, state="SERVING", no_in_flight=False
    )
    registry_target = custody._host_registry_matches_control(
        registry, target, state="SERVING", no_in_flight=False
    )
    if (
        not registry_source
        and not (observed == target_pair and registry_target)
        or plan.completed
        and not registry_target
    ):
        raise custody.AuthorizationCustodyUnavailable
    if observed == source_pair:
        custody._replace_control_bytes(
            membership_path, expected=plan.source_membership, target=plan.target_membership
        )
    if external.control == plan.source_control:
        custody._replace_control_bytes(
            external.control_path, expected=plan.source_control, target=plan.target_control
        )
    custody._advance_host_registry(
        source_control=source,
        source_state="SERVING",
        source_no_in_flight=False,
        target_control=target,
        target_state="SERVING",
        target_no_in_flight=False,
        now=now,
    )
    published = custody.load_external_custody(root)
    final_path, final_membership, final_replica = custody._standalone_membership_file(
        root, external=published
    )
    if (
        published.control != plan.target_control
        or final_membership != plan.target_membership
        or published.keyring != external.keyring
        or published.control_path != external.control_path
        or published.keyring_path != external.keyring_path
        or final_path != membership_path
        or final_replica != replica_id
    ):
        raise custody.AuthorizationCustodyUnavailable
    _path, _raw, registry, _host_key = custody._load_host_registry(target, now=now)
    if not custody._host_registry_matches_control(
        registry, target, state="SERVING", no_in_flight=False
    ):
        raise custody.AuthorizationCustodyUnavailable
    if not plan.completed:
        completed = replace(plan, completed=True)
        completed_raw = completed.encode()
        custody._replace_control_bytes(checkpoint, expected=encoded, target=completed_raw)
        plan, encoded = completed, completed_raw
    return plan, encoded, successor


def _reconcile_completed_activation(
    root: Path,
    *,
    plan: _RenewalPlan,
    encoded: bytes,
    checkpoint: Path,
    external: custody.ExternalAuthorizationCustody,
    keyring: custody.AuthorizationKeyring,
    member_path: Path,
    member_raw: bytes,
    replica_id: str,
    now: int,
) -> tuple[_RenewalPlan, bytes]:
    """Retire completed renewal only for a proven direct activation publication."""

    from .governance import store

    if not plan.completed or member_raw != plan.target_membership:
        raise custody.AuthorizationCustodyUnavailable
    _source, prior, _successor = _validate_historical_plan(
        plan,
        root=root,
        external=external,
        keyring=keyring,
        membership_path=member_path,
        replica_id=replica_id,
        actual_now=now,
        require_current_activation=False,
    )
    current = custody.load_authorization_custody(root, now=now)
    if current.control != replace(
        prior,
        activation_epoch=prior.activation_epoch + 1,
        activation_state_digest=current.control.activation_state_digest,
    ):
        raise custody.AuthorizationCustodyUnavailable
    custody.require_current_standalone_registry(current, now=now, require_serving=True)
    custody._require_attachment_target_authority(
        root, control=current.control, now=now, invalidate_sessions=False
    )
    connection = store.open_active_governance_read_connection(root)
    try:
        rows = connection.execute(
            "SELECT predecessor_activation_state_digest, publication_kind "
            "FROM governance_tuple_publications WHERE target_activation_state_digest=? "
            "AND activation_epoch=?",
            (current.control.activation_state_digest, current.control.activation_epoch),
        ).fetchall()
    finally:
        connection.close()
    if (
        len(rows) != 1
        or rows[0][0] != prior.activation_state_digest
        or rows[0][1] not in {"policy", "catalog"}
    ):
        raise custody.AuthorizationCustodyUnavailable
    successor = _new_plan(
        root,
        external=external,
        keyring=keyring,
        membership_path=member_path,
        membership_raw=member_raw,
        replica_id=replica_id,
        now=now,
    )
    _validate_historical_plan(
        successor,
        root=root,
        external=external,
        keyring=keyring,
        membership_path=member_path,
        replica_id=replica_id,
        actual_now=now,
    )
    successor_raw = successor.encode()
    custody._replace_control_bytes(checkpoint, expected=encoded, target=successor_raw)
    return successor, successor_raw


def renew_standalone_custody(vault_root: Path, *, now: int) -> custody.AuthorizationCustody:
    """Renew one explicitly owner-enabled standalone attachment, with exact recovery."""

    moment = custody._bounded_time(now)
    root = Path(vault_root)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise custody.AuthorizationCustodyUnavailable
    manager = writer_lease.get_manager()
    with manager.consistency_guard(
        root, operation="native-custody-renewal", holder_kind="deployment-controller"
    ):
        with reserved_paths._identity_coordination_scope(root):
            external = custody.load_external_custody(root)
            keyring = custody.parse_keyring(external.keyring)
            if not keyring.active_key.not_before <= moment < keyring.active_key.not_after:
                raise custody.AuthorizationCustodyUnavailable
            member_path, member_raw, replica_id = custody._standalone_membership_file(
                root, external=external
            )
            checkpoint = external.control_path.parent / CHECKPOINT_NAME
            if checkpoint in {external.keyring_path, external.control_path, member_path}:
                raise custody.AuthorizationCustodyUnavailable
            existing = os.path.lexists(checkpoint)
            if existing:
                encoded = custody._load_file(checkpoint).data
                plan = _decode_checkpoint(encoded)
                if plan.completed and external.control != plan.target_control:
                    plan, encoded = _reconcile_completed_activation(
                        root,
                        plan=plan,
                        encoded=encoded,
                        checkpoint=checkpoint,
                        external=external,
                        keyring=keyring,
                        member_path=member_path,
                        member_raw=member_raw,
                        replica_id=replica_id,
                        now=moment,
                    )
            else:
                plan = _new_plan(
                    root,
                    external=external,
                    keyring=keyring,
                    membership_path=member_path,
                    membership_raw=member_raw,
                    replica_id=replica_id,
                    now=moment,
                )
                _validate_historical_plan(
                    plan,
                    root=root,
                    external=external,
                    keyring=keyring,
                    membership_path=member_path,
                    replica_id=replica_id,
                    actual_now=moment,
                )
                encoded = plan.encode()
                custody._publish_private_file(checkpoint, encoded)
            plan, encoded, successor = _finish_checkpoint(
                root, checkpoint=checkpoint, plan=plan, encoded=encoded, now=moment
            )
            renewal_at = (
                successor.issued_at
                + min(
                    successor.expires_at - successor.issued_at,
                    membership.MAX_ATTESTATION_TTL_SECONDS,
                )
                // 2
            )
            if existing and moment >= renewal_at:
                # An expired partial target was completed durably above, before this next plan.
                external = custody.load_external_custody(root)
                member_path, member_raw, replica_id = custody._standalone_membership_file(
                    root, external=external
                )
                plan = _new_plan(
                    root,
                    external=external,
                    keyring=keyring,
                    membership_path=member_path,
                    membership_raw=member_raw,
                    replica_id=replica_id,
                    now=moment,
                )
                _validate_historical_plan(
                    plan,
                    root=root,
                    external=external,
                    keyring=keyring,
                    membership_path=member_path,
                    replica_id=replica_id,
                    actual_now=moment,
                )
                next_encoded = plan.encode()
                custody._replace_control_bytes(checkpoint, expected=encoded, target=next_encoded)
                plan, encoded, successor = _finish_checkpoint(
                    root, checkpoint=checkpoint, plan=plan, encoded=next_encoded, now=moment
                )
            verified = custody.load_authorization_custody(root, now=moment)
            if (
                verified.serving_membership != successor
                or custody._load_file(verified.control_path).data != plan.target_control
            ):
                raise custody.AuthorizationCustodyUnavailable
            custody.require_current_standalone_registry(verified, now=moment, require_serving=True)
            custody._require_attachment_target_authority(
                root, control=verified.control, now=moment, invalidate_sessions=False
            )
            return verified
