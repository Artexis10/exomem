"""Finite owner-reviewed standalone deployment-floor publication."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from . import reserved_paths, state_migration, vocabulary_authority, writer_lease
from .governance import authorization_custody as custody
from .governance import authorization_serving_membership as membership
from .vocabulary_authority import DeploymentFloorProof, TrustedOwnerDecision

_PLAN_TTL_SECONDS = 300
_BYTE_FIELDS = frozenset(
    {"source_control", "target_control", "source_membership", "target_membership"}
)
_PATH_FIELDS = frozenset({"vault_root", "keyring_path", "control_path", "membership_path"})
_TIME_FIELDS = frozenset({"prepared_at", "expires_at"})


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class FloorPlan:
    """Exact signed successor, retained privately across the offline stop window."""

    vault_root: Path = field(repr=False)
    keyring_path: Path = field(repr=False)
    control_path: Path = field(repr=False)
    membership_path: Path = field(repr=False)
    attachment_id: str
    replica_id: str
    runtime: str
    software_version: str
    prepared_at: int
    expires_at: int
    keyring_digest: str
    source_control: bytes = field(repr=False)
    target_control: bytes = field(repr=False)
    source_membership: bytes = field(repr=False)
    target_membership: bytes = field(repr=False)
    review_digest: str

    def as_dict(self) -> dict[str, Any]:
        """Detached finite review facts; no keys or private publication bytes."""

        source = json.loads(self.source_control)
        target = json.loads(self.target_control)
        return {
            "action": "publish_standalone_vocabulary_floor",
            "vault_root": str(self.vault_root),
            "attachment_id": self.attachment_id,
            "logical_vault_id": source["logical_vault_id"],
            "cell_id": source["cell_id"],
            "replica_id": self.replica_id,
            "runtime": self.runtime,
            "software_version": self.software_version,
            "source_floor": source.get("vocabulary_authority_floor", 1),
            "target_floor": target["vocabulary_authority_floor"],
            "source_membership_epoch": source["serving_membership_epoch"],
            "target_membership_epoch": target["serving_membership_epoch"],
            "activation_generation": source["activation_epoch"],
            "keyring_digest": self.keyring_digest,
            "source_control_digest": _digest(self.source_control),
            "target_control_digest": _digest(self.target_control),
            "source_membership_digest": _digest(self.source_membership),
            "target_membership_digest": _digest(self.target_membership),
            "prepared_at": self.prepared_at,
            "expires_at": self.expires_at,
            "review_digest": self.review_digest,
        }

    def as_record(self) -> dict[str, Any]:
        """Lossless private storage form, never a browser review response."""

        record: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in _BYTE_FIELDS:
                value = base64.b64encode(value).decode("ascii")
            elif item.name in _PATH_FIELDS:
                value = str(value)
            record[item.name] = value
        return record

    @classmethod
    def from_record(cls, record: object) -> FloorPlan:
        """Reject expanded or corrupted private records before custody is opened."""

        try:
            if not isinstance(record, dict) or set(record) != {item.name for item in fields(cls)}:
                raise custody.AuthorizationCustodyUnavailable
            decoded: dict[str, Any] = {}
            for name, value in record.items():
                if name in _TIME_FIELDS:
                    value = custody._bounded_time(value)
                else:
                    maximum = 4 * custody.MAX_CUSTODY_FILE_BYTES if name in _BYTE_FIELDS else 4096
                    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
                        raise custody.AuthorizationCustodyUnavailable
                    if name in _BYTE_FIELDS:
                        value = base64.b64decode(value, validate=True)
                        if not 1 <= len(value) <= custody.MAX_CUSTODY_FILE_BYTES:
                            raise custody.AuthorizationCustodyUnavailable
                    elif name in _PATH_FIELDS:
                        value = Path(value)
                        if not value.is_absolute():
                            raise custody.AuthorizationCustodyUnavailable
                decoded[name] = value
            plan = cls(**decoded)
            _require_plan_digest(plan)
            return plan
        except (ValueError, TypeError, binascii.Error):
            raise custody.AuthorizationCustodyUnavailable from None


def _review_digest(plan: FloorPlan) -> str:
    record = plan.as_record()
    del record["review_digest"]
    return _digest(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())


def _require_plan_digest(plan: FloorPlan) -> None:
    if (
        not isinstance(plan, FloorPlan)
        or plan.review_digest != _review_digest(plan)
        or not plan.prepared_at < plan.expires_at <= plan.prepared_at + _PLAN_TTL_SECONDS
    ):
        raise custody.AuthorizationCustodyUnavailable


def _canonical_root(vault_root: Path) -> Path:
    root = Path(vault_root)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise custody.AuthorizationCustodyUnavailable
    return root


def prepare_standalone_floor(vault_root: Path, *, now: int) -> FloorPlan:
    """Read and sign one exact-v4 singleton successor without publishing it."""

    moment = custody._bounded_time(now)
    root = _canonical_root(vault_root)
    current = custody.load_authorization_custody(root, now=moment)
    control = current.control
    if (
        control.version != 1
        or control.vocabulary_authority_floor != 1
        or not control.governance_enrolled
        or current.serving_membership is None
        or control.registry_attachment_id != custody.standalone_attachment_id(root)
    ):
        raise custody.AuthorizationCustodyUnavailable
    custody.require_current_standalone_registry(current, now=moment, require_serving=True)
    custody._require_attachment_target_authority(
        root, control=control, now=moment, invalidate_sessions=False
    )
    external = custody.load_external_custody(root)
    membership_path, source_membership, replica_id = custody._standalone_membership_file(
        root, external=external
    )
    if (
        custody.parse_keyring(external.keyring) != current.keyring
        or custody.parse_control_record(external.control, keyring=current.keyring, now=moment)
        != control
        or membership.serving_membership_digest(source_membership)
        != current.serving_membership.record_digest
    ):
        raise custody.AuthorizationCustodyUnavailable
    custody._require_standalone_membership(
        current.serving_membership,
        keyring=current.keyring,
        control=control,
        replica_id=replica_id,
        state="SERVING",
        no_in_flight=False,
    )
    target = replace(
        control,
        version=2,
        vocabulary_authority_floor=2,
        serving_membership_epoch=control.serving_membership_epoch + 1,
        serving_membership_digest="0" * 64,
    )
    target_membership = custody._standalone_membership_bytes(
        keyring=current.keyring,
        control=target,
        replica_id=replica_id,
        previous_epoch_digest=current.serving_membership.record_digest,
        attested_at=moment,
    )
    successor = custody._standalone_membership_successor(
        target_membership,
        keyring=current.keyring,
        expected_control=control,
        target_control=target,
        replica_id=replica_id,
        target_state="SERVING",
        target_no_in_flight=False,
        now=moment,
    )
    membership.validate_membership_successor(current.serving_membership, successor, now=moment)
    target = replace(target, serving_membership_digest=successor.record_digest)
    key = next(key for key in current.keyring.accepted_keys if key.key_id == target.signing_key_id)
    target_control = custody._signed_control_bytes(target, signing_key=key.key)
    # Parsing the target proves this runtime admits the authenticated v2 contract.
    if custody.parse_control_record(target_control, keyring=current.keyring, now=moment) != target:
        raise custody.AuthorizationCustodyUnavailable
    plan = FloorPlan(
        vault_root=root,
        keyring_path=external.keyring_path,
        control_path=external.control_path,
        membership_path=membership_path,
        attachment_id=control.registry_attachment_id,
        replica_id=replica_id,
        runtime=vocabulary_authority.RUNTIME_FLOOR,
        software_version=custody.runtime_software_version(),
        prepared_at=moment,
        expires_at=min(
            moment + _PLAN_TTL_SECONDS,
            control.expires_at,
            current.serving_membership.expires_at,
            successor.expires_at,
        ),
        keyring_digest=_digest(external.keyring),
        source_control=external.control,
        target_control=target_control,
        source_membership=source_membership,
        target_membership=target_membership,
        review_digest="",
    )
    plan = replace(plan, review_digest=_review_digest(plan))
    _require_plan_digest(plan)
    return plan


def publish_standalone_floor(
    vault_root: Path,
    plan: FloorPlan,
    *,
    decision: TrustedOwnerDecision,
    offline_authority: state_migration.OfflineMigrationAuthority,
    now: int,
) -> DeploymentFloorProof:
    """Publish only reviewed bytes after the maintenance runner proves service stop."""

    state_migration._require_offline_authority(offline_authority)
    moment = custody._bounded_time(now)
    _require_plan_digest(plan)
    vocabulary_authority.VocabularyAuthority._require_owner(
        decision, binding=plan.review_digest, now=moment
    )
    if decision.expires_at <= moment:
        raise vocabulary_authority.VocabularyAuthorityDenied("trusted owner decision expired")
    if not plan.prepared_at <= moment < plan.expires_at:
        raise custody.AuthorizationCustodyUnavailable
    root = _canonical_root(vault_root)
    target, successor, _continued = _complete_floor_plan(root, plan, now=moment)
    verified = custody.load_authorization_custody(root, now=moment)
    if verified.control != target or verified.serving_membership != successor:
        raise custody.AuthorizationCustodyUnavailable
    return _current_floor_proof(root, verified, runtime=plan.runtime, now=moment)


def _current_floor_proof(
    root: Path,
    verified: custody.AuthorizationCustody,
    *,
    runtime: str,
    now: int,
) -> DeploymentFloorProof:
    custody.require_current_standalone_registry(verified, now=now, require_serving=True)
    custody._require_attachment_target_authority(
        root, control=verified.control, now=now, invalidate_sessions=False
    )
    return vocabulary_authority._deployment_floor_for_adapter(
        runtime=runtime, generation=verified.control.activation_epoch
    )


def recover_standalone_floor(
    vault_root: Path,
    plan: FloorPlan,
    *,
    decision: TrustedOwnerDecision,
    started_at: int,
    offline_authority: state_migration.OfflineMigrationAuthority,
    now: int,
    allow_same_authority_renewal: bool = False,
) -> DeploymentFloorProof:
    """Finish a proven partial publication under its original, possibly expired consent.

    The private maintenance runner supplies its durable start timestamp and any
    separately accepted renewal permission. Unchanged source bytes never prove
    that publication began, regardless of a ledger's applying status.
    """

    state_migration._require_offline_authority(offline_authority)
    moment = custody._bounded_time(now)
    started = custody._bounded_time(started_at)
    _require_plan_digest(plan)
    # Authenticate the original decision at the recorded start; do not extend it.
    vocabulary_authority.VocabularyAuthority._require_owner(
        decision, binding=plan.review_digest, now=started
    )
    if (
        not plan.prepared_at <= started < min(plan.expires_at, decision.expires_at)
        or started > moment
        or type(allow_same_authority_renewal) is not bool
    ):
        raise custody.AuthorizationCustodyUnavailable
    root = _canonical_root(vault_root)
    with writer_lease.get_manager().consistency_guard(
        root, operation="standalone-floor-recovery", holder_kind="deployment-controller"
    ):
        target, successor, continued = _complete_floor_plan(
            root,
            plan,
            now=moment,
            historical_recovery=True,
            allow_renewal_continuation=allow_same_authority_renewal,
        )
        if continued or moment >= min(target.expires_at, successor.expires_at):
            if not allow_same_authority_renewal:
                raise custody.AuthorizationCustodyUnavailable
            from .native_custody_renewal import renew_standalone_custody

            verified = renew_standalone_custody(root, now=moment)
            if verified.control != replace(
                target,
                issued_at=verified.control.issued_at,
                expires_at=verified.control.expires_at,
                serving_membership_epoch=verified.control.serving_membership_epoch,
                serving_membership_digest=verified.control.serving_membership_digest,
            ):
                raise custody.AuthorizationCustodyUnavailable
        else:
            verified = custody.load_authorization_custody(root, now=moment)
            if verified.control != target or verified.serving_membership != successor:
                raise custody.AuthorizationCustodyUnavailable
        return _current_floor_proof(root, verified, runtime=plan.runtime, now=moment)


def _require_exact_floor_renewal_checkpoint(root: Path, plan: FloorPlan) -> None:
    """Admit only renewal whose stored source is this exact reviewed floor target."""

    from .native_custody_renewal import CHECKPOINT_NAME, _decode_checkpoint

    checkpoint = _decode_checkpoint(
        custody._load_file(plan.control_path.parent / CHECKPOINT_NAME).data
    )
    if (
        checkpoint.vault_root != str(root)
        or checkpoint.source_control != plan.target_control
        or checkpoint.source_membership != plan.target_membership
        or checkpoint.keyring_digest != plan.keyring_digest
        or checkpoint.attachment_id != plan.attachment_id
    ):
        raise custody.AuthorizationCustodyUnavailable


def _complete_floor_plan(
    root: Path,
    plan: FloorPlan,
    *,
    now: int,
    historical_recovery: bool = False,
    allow_renewal_continuation: bool = False,
) -> tuple[custody.AuthorizationControlRecord, membership.ServingMembershipEpoch, bool]:
    moment = now
    with reserved_paths._identity_coordination_scope(root):
        if (
            root != plan.vault_root
            or custody.standalone_attachment_id(root) != plan.attachment_id
            or plan.runtime != vocabulary_authority.RUNTIME_FLOOR
            or plan.software_version != custody.runtime_software_version()
        ):
            raise custody.AuthorizationCustodyUnavailable
        external = custody.load_external_custody(root)
        membership_path, observed_membership, replica_id = custody._standalone_membership_file(
            root, external=external
        )
        if (
            external.keyring_path != plan.keyring_path
            or external.control_path != plan.control_path
            or membership_path != plan.membership_path
            or replica_id != plan.replica_id
            or _digest(external.keyring) != plan.keyring_digest
        ):
            raise custody.AuthorizationCustodyUnavailable
        keyring = custody.parse_keyring(external.keyring)
        source_time = target_time = previous_time = successor_time = moment
        if historical_recovery:
            from .native_custody_renewal import _historical_issued_at

            if not keyring.active_key.not_before <= moment < keyring.active_key.not_after:
                raise custody.AuthorizationCustodyUnavailable
            source_time = _historical_issued_at(plan.source_control, actual_now=moment)
            target_time = _historical_issued_at(plan.target_control, actual_now=moment)
            previous_time = _historical_issued_at(plan.source_membership, actual_now=moment)
            successor_time = _historical_issued_at(plan.target_membership, actual_now=moment)
        source = custody.parse_control_record(plan.source_control, keyring=keyring, now=source_time)
        target = custody.parse_control_record(plan.target_control, keyring=keyring, now=target_time)
        if historical_recovery:
            for control in (source, target):
                signing = next(
                    key for key in keyring.accepted_keys if key.key_id == control.signing_key_id
                )
                if not signing.not_before <= moment < signing.not_after:
                    raise custody.AuthorizationCustodyUnavailable
        if (
            source.version != 1
            or source.vocabulary_authority_floor != 1
            or not source.governance_enrolled
            or source.registry_attachment_id != plan.attachment_id
            or target
            != replace(
                source,
                version=2,
                vocabulary_authority_floor=2,
                serving_membership_epoch=source.serving_membership_epoch + 1,
                serving_membership_digest=membership.serving_membership_digest(
                    plan.target_membership
                ),
            )
        ):
            raise custody.AuthorizationCustodyUnavailable
        previous = custody._parse_migration_membership(
            plan.source_membership,
            keyring=keyring,
            control=source,
            now=previous_time,
            epoch=source.serving_membership_epoch,
            digest=source.serving_membership_digest,
        )
        custody._require_standalone_membership(
            previous,
            keyring=keyring,
            control=source,
            replica_id=replica_id,
            state="SERVING",
            no_in_flight=False,
        )
        successor = custody._standalone_membership_successor(
            plan.target_membership,
            keyring=keyring,
            expected_control=source,
            target_control=target,
            replica_id=replica_id,
            target_state="SERVING",
            target_no_in_flight=False,
            now=successor_time,
        )
        # In recovery this establishes historical continuity, never serving readiness.
        membership.validate_membership_successor(previous, successor, now=successor_time)
        if successor.issued_at != plan.prepared_at or plan.expires_at > min(
            source.expires_at, previous.expires_at, successor.expires_at
        ):
            raise custody.AuthorizationCustodyUnavailable
        custody._require_attachment_target_authority(
            root, control=source, now=moment, invalidate_sessions=False
        )
        source_pair = (plan.source_control, plan.source_membership)
        partial_pair = (plan.source_control, plan.target_membership)
        target_pair = (plan.target_control, plan.target_membership)
        observed_pair = (external.control, observed_membership)
        if historical_recovery and observed_pair == source_pair:
            raise custody.AuthorizationCustodyUnavailable
        if observed_pair not in (source_pair, partial_pair, target_pair):
            if historical_recovery and allow_renewal_continuation:
                _require_exact_floor_renewal_checkpoint(root, plan)
                return target, successor, True
            raise custody.AuthorizationCustodyUnavailable
        _path, _raw, registry, _host_key = custody._load_host_registry(source, now=moment)
        registry_source = custody._host_registry_matches_control(
            registry, source, state="SERVING", no_in_flight=False
        )
        registry_target = custody._host_registry_matches_control(
            registry, target, state="SERVING", no_in_flight=False
        )
        if not registry_source and not (observed_pair == target_pair and registry_target):
            raise custody.AuthorizationCustodyUnavailable
        if observed_pair == source_pair:
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
            now=moment,
        )
        published = custody.load_external_custody(root)
        final_path, final_membership, final_replica = custody._standalone_membership_file(
            root, external=published
        )
        if (
            published.control != plan.target_control
            or final_membership != plan.target_membership
            or _digest(published.keyring) != plan.keyring_digest
            or published.control_path != plan.control_path
            or published.keyring_path != plan.keyring_path
            or final_path != plan.membership_path
            or final_replica != plan.replica_id
        ):
            raise custody.AuthorizationCustodyUnavailable
        _path, _raw, registry, _host_key = custody._load_host_registry(target, now=moment)
        if not custody._host_registry_matches_control(
            registry, target, state="SERVING", no_in_flight=False
        ):
            raise custody.AuthorizationCustodyUnavailable
        return target, successor, False
