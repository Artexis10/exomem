"""Pure custody transformation for stopped, enrolled target-start recovery.

The lifecycle must durably commit the intended revision before publishing it,
then re-prove current claim, maintenance, route closure and PVC/pod absence at
every CAS. These bytes are not effect authority or an ordinary renewal path.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping

from . import authorization_membership as membership
from .conflict_reason import ConflictReason
from .lifecycle import MetadataConflict

# Part of the r1 checkpoint protocol: changing this changes replayed bytes.
_RECOVERY_WINDOW_SECONDS = 3600


def recover_expired_serving_bundle(
    files: Mapping[str, bytes],
    *,
    expected_cell_id: str,
    expected_logical_vault_id: str,
    expected_replica_id: str,
    expected_software_version: str,
    expected_recovery_envelope: str,
    now: int,
    successor_issued_at: int,
) -> membership.HostedAuthorizationBundle:
    """Rebuild one exact DRAINING successor, authenticating at current time.

    A persisted issuance time makes acknowledgement-loss retries deterministic.
    The resulting window may already have elapsed on retry: changing it could
    race a delayed original CAS. Only later drained-to-serving recovery creates
    a fresh serving window; signing-key expiry is never waived here.
    """

    current = membership._required_time(now)
    issued_at = membership._required_time(successor_issued_at)
    identity = {
        "expected_cell_id": expected_cell_id,
        "expected_logical_vault_id": expected_logical_vault_id,
        "expected_replica_id": expected_replica_id,
        "expected_software_version": membership._required_identifier(expected_software_version),
        "expected_schema_version": 4,
        "expected_recovery_envelope": expected_recovery_envelope,
        "now": current,
        "_require_fresh": False,
    }
    source = membership.inspect_hosted_authorization_bundle(files, **identity)
    if (
        source.governance_enrolled is not True
        or source.replica_state != "SERVING"
        or source.issuance_stopped is not False
        or source.no_in_flight is not False
        or not source.expires_at <= issued_at <= current
        or source.epoch >= membership._MAX_INTEGER
    ):
        raise MetadataConflict(
            "authorization target recovery is unavailable",
            reason=ConflictReason.AUTHORIZATION_MEMBERSHIP_TRANSITION_IS_INVALID,
        )

    # Parse only already authenticated canonical bytes, retaining key/attachment,
    # software/schema and the enrolled activation tuple. Reuse the wire signer.
    keyring = json.loads(source.keyring)
    entry = keyring["accepted_keys"][0]
    key = base64.urlsafe_b64decode(entry["key"] + "=")
    expires_at = min(issued_at + _RECOVERY_WINDOW_SECONDS, entry["not_after"])
    control = json.loads(source.control)
    record = json.loads(source.membership)
    replica = record["replicas"][0]
    epoch = source.epoch + 1
    replica.update(
        epoch=epoch,
        state="DRAINING",
        issuance_stopped=True,
        no_in_flight=True,
        attested_at=issued_at,
        expires_at=expires_at,
    )
    replica["mac"] = membership._mac(key, membership._attestation_mac_input(replica))
    record.update(
        epoch=epoch,
        previous_epoch_digest=source.membership_digest,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    record["mac"] = membership._mac(key, membership._membership_mac_input(record))
    record_raw = membership._canonical(record)
    control.update(
        serving_membership_epoch=epoch,
        serving_membership_digest=hashlib.sha256(record_raw).hexdigest(),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    control["mac"] = membership._mac(key, membership._control_mac_input(control))
    return membership.inspect_hosted_authorization_bundle(
        {
            "keyring.json": source.keyring,
            "control.json": membership._canonical(control),
            "serving-membership.json": record_raw,
        },
        **identity,
    )
