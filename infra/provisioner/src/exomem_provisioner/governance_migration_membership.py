"""Pure, evidence-bound schema successors for the offline Hosted migration.

These helpers do not grant provider authority or publish custody. The caller
must obtain evidence from the stopped-cell Job adapter under a current claim,
recheck operation/fence/PVC ownership, and use the existing Secret revision CAS.
No ordinary renewal, drain or resume gains schema-changing authority here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping

from . import authorization_membership as membership
from .conflict_reason import ConflictReason
from .governance_migration_job import (
    MigrationJobEvidence,
    MigrationJobRequest,
    parse_migration_terminal,
)
from .lifecycle import MetadataConflict

_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")


def _refuse() -> MetadataConflict:
    return MetadataConflict(
        "authorization migration membership evidence differs",
        reason=ConflictReason.AUTHORIZATION_MEMBERSHIP_TRANSITION_IS_INVALID,
    )


def repair_governance_schema_claim(
    files: Mapping[str, bytes],
    *,
    request: MigrationJobRequest,
    evidence: MigrationJobEvidence,
    recovery_envelope: str,
    now: int,
) -> membership.HostedAuthorizationBundle:
    """Repair only a fresh, drained, unenrolled actual-v3 schema claim."""

    return _schema_successor(
        files,
        request=request,
        evidence=evidence,
        recovery_envelope=recovery_envelope,
        now=now,
        phase="inspect",
        target_schema=3,
    )


def complete_governance_migration_membership(
    files: Mapping[str, bytes],
    *,
    request: MigrationJobRequest,
    evidence: MigrationJobEvidence,
    recovery_envelope: str,
    now: int,
) -> membership.HostedAuthorizationBundle:
    """Record verified actual-v4 completion without renewing or resuming custody.

    A signed expired window stays expired. Only a separately authorized resume
    may make this fully drained generation serving again.
    """

    return _schema_successor(
        files,
        request=request,
        evidence=evidence,
        recovery_envelope=recovery_envelope,
        now=now,
        phase="commit",
        target_schema=4,
    )


def _schema_successor(
    files: Mapping[str, bytes],
    *,
    request: MigrationJobRequest,
    evidence: MigrationJobEvidence,
    recovery_envelope: str,
    now: int,
    phase: str,
    target_schema: int,
) -> membership.HostedAuthorizationBundle:
    if (
        not isinstance(request, MigrationJobRequest)
        or not isinstance(evidence, MigrationJobEvidence)
        or request.phase != phase
        or any(
            not isinstance(uid, str) or _UID.fullmatch(uid) is None
            for uid in (evidence.job_uid, evidence.pod_uid)
        )
    ):
        raise _refuse()
    # Revalidate the immutable raw terminal, not a caller-owned parsed mapping.
    terminal = parse_migration_terminal(request, evidence._terminal)
    identity = {
        "expected_cell_id": request.metadata.subject_id,
        "expected_logical_vault_id": request.vault_id,
        "expected_replica_id": request.metadata.resource_name + "-0",
        "expected_software_version": None,
        "expected_schema_version": None,
        "expected_recovery_envelope": recovery_envelope,
        "now": now,
        "_require_fresh": phase == "inspect",
    }
    source = membership.inspect_hosted_authorization_bundle(files, **identity)
    control = json.loads(source.control)
    if (
        source.revision != request.custody_revision
        or source.replica_state != "DRAINING"
        or not source.issuance_stopped
        or not source.no_in_flight
        or source.governance_enrolled is not (phase == "commit")
        or terminal["actualSchema"] != target_schema
        or terminal["membershipSchema"] != source.membership_schema_version
        or terminal["governanceEnrolled"] is not source.governance_enrolled
        or control["issued_at"] > now
        or (
            phase == "commit"
            and (
                terminal["activationStoreId"],
                terminal["activationEpoch"],
                terminal["activationStateDigest"],
            )
            != (
                source.activation_store_id,
                source.activation_epoch,
                source.activation_state_digest,
            )
        )
    ):
        raise _refuse()
    if source.membership_schema_version == target_schema:
        return source
    if source.epoch >= membership._MAX_INTEGER:
        raise _refuse()

    # Transform only bytes already authenticated above; preserve their exact
    # key, attachment, enrollment, release and validity window. Reuse the wire
    # signer's canonical framing, then independently inspect the whole result.
    keyring = json.loads(source.keyring)
    key = base64.b64decode(
        keyring["accepted_keys"][0]["key"].encode("ascii") + b"=",
        altchars=b"-_",
        validate=True,
    )
    record = json.loads(source.membership)
    epoch = source.epoch + 1
    replica = record["replicas"][0]
    replica.update(epoch=epoch, schema_version=target_schema)
    replica["mac"] = membership._mac(key, membership._attestation_mac_input(replica))
    record.update(epoch=epoch, previous_epoch_digest=source.membership_digest)
    record["mac"] = membership._mac(key, membership._membership_mac_input(record))
    record_raw = membership._canonical(record)
    control.update(
        serving_membership_epoch=epoch,
        serving_membership_digest=hashlib.sha256(record_raw).hexdigest(),
    )
    control["mac"] = membership._mac(key, membership._control_mac_input(control))
    return membership.inspect_hosted_authorization_bundle(
        {
            "keyring.json": source.keyring,
            "control.json": membership._canonical(control),
            "serving-membership.json": record_raw,
        },
        **{**identity, "expected_schema_version": target_schema},
    )
