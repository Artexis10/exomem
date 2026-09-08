"""Never-served bootstrap drain; not ordinary renewal or migration-plan recovery."""

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
