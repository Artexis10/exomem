"""Never-served drain and pre-plan window reissue; not ordinary renewal or plan recovery."""

from __future__ import annotations

import json
from collections.abc import Mapping

from . import authorization_membership as membership
from .conflict_reason import ConflictReason
from .governance_target_recovery import _draining_successor_files
from .lifecycle import MetadataConflict


def drain_fresh_bootstrap_bundle(
    files: Mapping[str, bytes],
    *,
    expected_cell_id: str,
    expected_logical_vault_id: str,
    expected_replica_id: str,
    expected_software_version: str,
    expected_recovery_envelope: str,
    now: int,
) -> membership.HostedAuthorizationBundle:
    """Drain an unenrolled schema-3 genesis, preserving its custody and lineage.

    The caller must prove this exact provision owns never-started storage with
    closed routes and no workload, before any plan or enrollment exists. Keys
    authenticate at real current time; only the genesis window may have elapsed.
    """
    current = membership._required_time(now)
    identity = {
        "expected_cell_id": expected_cell_id,
        "expected_logical_vault_id": expected_logical_vault_id,
        "expected_replica_id": expected_replica_id,
        "expected_software_version": membership._required_identifier(expected_software_version),
        "expected_recovery_envelope": expected_recovery_envelope,
        "expected_schema_version": membership.AUTHORIZATION_BOOTSTRAP_SCHEMA_VERSION,
        "now": current,
    }
    source = membership.inspect_hosted_authorization_bundle(files, **identity, _require_fresh=False)
    if source.governance_enrolled or json.loads(source.control)["issued_at"] > current:
        raise MetadataConflict(
            "authorization bootstrap drain is unavailable",
            reason=ConflictReason.AUTHORIZATION_MEMBERSHIP_TRANSITION_IS_INVALID,
        )
    if (
        source.epoch == 2
        and source.replica_state == "DRAINING"
        and source.no_in_flight
        and source.issuance_stopped
    ):
        return source
    if (
        source.epoch != 1
        or source.replica_state != "SERVING"
        or source.no_in_flight
        or source.issuance_stopped
        or json.loads(source.membership)["previous_epoch_digest"] is not None
    ):
        raise MetadataConflict(
            "authorization bootstrap drain is unavailable",
            reason=ConflictReason.AUTHORIZATION_MEMBERSHIP_TRANSITION_IS_INVALID,
        )
    return membership.inspect_hosted_authorization_bundle(
        _draining_successor_files(source, issued_at=current), **identity
    )


# A runner node whose clock trails the provisioner's must not see the reissued
# window as issued in its future.
_REISSUE_BACKDATE_SECONDS = 120


def refresh_drained_source_bundle(
    files: Mapping[str, bytes],
    *,
    expected_cell_id: str,
    expected_logical_vault_id: str,
    expected_replica_id: str,
    expected_software_version: str | None,
    expected_schema_version: int | None,
    expected_recovery_envelope: str,
    now: int,
) -> membership.HostedAuthorizationBundle:
    """Reissue the window of a drained, unenrolled source before any plan exists.

    The migration Job refuses to inspect or prepare under a closed window, and a
    drained generation has no serving replica left to renew it, so without this
    a migration starting more than one window after the drain strands its cell.
    The successor stays DRAINING with issuance stopped, so it authorizes nothing,
    and keys still authenticate at real current time. The caller must never
    reissue once a plan exists: the prepared plan binds the source window.
    """
    current = membership._required_time(now)
    identity = {
        "expected_cell_id": expected_cell_id,
        "expected_logical_vault_id": expected_logical_vault_id,
        "expected_replica_id": expected_replica_id,
        "expected_software_version": expected_software_version,
        "expected_schema_version": expected_schema_version,
        "expected_recovery_envelope": expected_recovery_envelope,
        "now": current,
    }
    source = membership.inspect_hosted_authorization_bundle(files, **identity, _require_fresh=False)
    if (
        source.governance_enrolled
        or source.replica_state != "DRAINING"
        or not source.issuance_stopped
        or not source.no_in_flight
        or source.epoch >= membership._MAX_INTEGER
    ):
        raise MetadataConflict(
            "authorization source refresh is unavailable",
            reason=ConflictReason.AUTHORIZATION_MEMBERSHIP_TRANSITION_IS_INVALID,
        )
    # Never earlier than the source's own issue time. A source issued after `now`
    # therefore yields a successor issued after `now`, which the authentication
    # below refuses.
    issued_at = max(json.loads(source.control)["issued_at"], current - _REISSUE_BACKDATE_SECONDS)
    return membership.inspect_hosted_authorization_bundle(
        _draining_successor_files(source, issued_at=issued_at), **identity
    )
