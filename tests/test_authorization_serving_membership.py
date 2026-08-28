from __future__ import annotations

from dataclasses import replace

import pytest

from exomem.governance import authorization_serving_membership as membership

NOW = 1_800_000_000
CONTROL_DIGEST = "c" * 64
KEYRING_DIGEST = "d" * 64
OLD_KEY = b"o" * 32
NEW_KEY = b"n" * 32
KEYS = {"auth-key-old": OLD_KEY, "auth-key-new": NEW_KEY}


def _replica(
    replica_id: str,
    *,
    epoch: int = 7,
    state: str = "SERVING",
    active_key_id: str = "auth-key-old",
    accepted_key_ids: tuple[str, ...] = ("auth-key-new", "auth-key-old"),
    attested_at: int = NOW - 10,
    expires_at: int = NOW + 60,
    issuance_stopped: bool = False,
    no_in_flight: bool = False,
) -> membership.ReplicaReadinessAttestation:
    return membership.ReplicaReadinessAttestation(
        version=1,
        epoch=epoch,
        replica_id=replica_id,
        state=state,
        software_version="0.48.0",
        schema_version=4,
        cell_id="cell-7",
        active_key_id=active_key_id,
        accepted_key_ids=accepted_key_ids,
        control_digest=CONTROL_DIGEST,
        keyring_digest=KEYRING_DIGEST,
        attested_at=attested_at,
        expires_at=expires_at,
        issuance_stopped=issuance_stopped,
        no_in_flight=no_in_flight,
        signing_key_id=active_key_id,
    )


def _record(
    *replicas: membership.ReplicaReadinessAttestation,
    epoch: int = 7,
    previous_epoch_digest: str | None = "a" * 64,
) -> membership.ServingMembershipEpoch:
    return membership.ServingMembershipEpoch(
        version=1,
        epoch=epoch,
        cell_id="cell-7",
        logical_vault_id="logical-vault-7",
        previous_epoch_digest=previous_epoch_digest,
        issued_at=NOW - 10,
        expires_at=NOW + 60,
        replicas=tuple(replicas),
        signing_key_id="auth-key-old",
    )


def _round_trip(
    record: membership.ServingMembershipEpoch,
) -> membership.ServingMembershipEpoch:
    raw = membership.encode_serving_membership(record, verifier_keys=KEYS)
    return membership.parse_serving_membership(
        raw,
        verifier_keys=KEYS,
        now=NOW,
        expected_cell_id="cell-7",
        expected_logical_vault_id="logical-vault-7",
        expected_epoch=record.epoch,
        expected_digest=membership.serving_membership_digest(raw),
    )


def test_replica_attestation_has_an_independent_canonical_wire_contract() -> None:
    attestation = _replica("replica-a", epoch=8)

    raw = membership.encode_replica_readiness_attestation(
        attestation,
        verifier_keys=KEYS,
    )
    parsed = membership.parse_replica_readiness_attestation(
        raw,
        verifier_keys=KEYS,
        now=NOW,
        expected_epoch=8,
        expected_cell_id="cell-7",
    )

    assert parsed == attestation
    assert membership.encode_replica_readiness_attestation(
        parsed,
        verifier_keys=KEYS,
    ) == raw
    with pytest.raises(membership.ServingMembershipUnavailable):
        membership.parse_replica_readiness_attestation(
            raw.replace(b'"replica-a"', b'"replica-x"'),
            verifier_keys=KEYS,
            now=NOW,
            expected_epoch=8,
            expected_cell_id="cell-7",
        )


def _readiness(
    record: membership.ServingMembershipEpoch,
    *,
    local_replica_id: str = "replica-a",
    live_verifier_key_ids: tuple[str, ...] = (),
) -> membership.ServingMembershipReadiness:
    return membership.evaluate_serving_membership(
        record,
        now=NOW,
        local_replica_id=local_replica_id,
        local_software_version="0.48.0",
        local_schema_version=4,
        expected_cell_id="cell-7",
        expected_control_digest=CONTROL_DIGEST,
        expected_keyring_digest=KEYRING_DIGEST,
        local_active_key_id="auth-key-old",
        local_accepted_key_ids=("auth-key-new", "auth-key-old"),
        valid_verifier_key_ids=("auth-key-new", "auth-key-old"),
        live_verifier_key_ids=live_verifier_key_ids,
    )


def test_two_current_replicas_with_mixed_active_keys_are_ready() -> None:
    record = _round_trip(
        _record(
            _replica("replica-a"),
            _replica("replica-b", active_key_id="auth-key-new"),
        )
    )

    readiness = _readiness(record)

    assert readiness.ready is True
    assert readiness.code == "AUTHORIZATION_MEMBERSHIP_READY"
    assert readiness.epoch == 7
    assert readiness.serving_replicas == 2
    assert readiness.draining_replicas == 0
    assert readiness.as_public_dict() == {
        "ready": True,
        "code": "AUTHORIZATION_MEMBERSHIP_READY",
        "servingMembershipEpoch": 7,
        "servingReplicaCount": 2,
        "drainingReplicaCount": 0,
    }


@pytest.mark.parametrize(
    "record",
    [
        _record(
            _replica("replica-a"),
            _replica("replica-b", expires_at=NOW),
        ),
        _record(
            _replica("replica-a"),
            _replica("replica-b", epoch=6),
        ),
        _record(
            _replica("replica-a"),
            _replica(
                "replica-b",
                active_key_id="auth-key-new",
                accepted_key_ids=("auth-key-new",),
            ),
        ),
    ],
)
def test_stale_epoch_silent_member_or_broken_intersection_blocks(
    record: membership.ServingMembershipEpoch,
) -> None:
    try:
        verified = _round_trip(record)
    except membership.ServingMembershipUnavailable:
        return

    assert _readiness(verified).ready is False


def test_membership_authentication_and_control_binding_are_not_optional() -> None:
    raw = membership.encode_serving_membership(
        _record(_replica("replica-a"), _replica("replica-b")),
        verifier_keys=KEYS,
    )
    tampered = raw.replace(b'"replica-b"', b'"replica-x"')

    with pytest.raises(membership.ServingMembershipUnavailable):
        membership.parse_serving_membership(
            tampered,
            verifier_keys=KEYS,
            now=NOW,
            expected_cell_id="cell-7",
            expected_logical_vault_id="logical-vault-7",
            expected_epoch=7,
            expected_digest=membership.serving_membership_digest(tampered),
        )

    verified = _round_trip(_record(_replica("replica-a"), _replica("replica-b")))
    assert replace(
        _readiness(verified),
        ready=False,
        code="AUTHORIZATION_MEMBERSHIP_UNAVAILABLE",
    ).ready is False
    assert membership.evaluate_serving_membership(
        verified,
        now=NOW,
        local_replica_id="replica-a",
        local_software_version="0.48.0",
        local_schema_version=4,
        expected_cell_id="cell-7",
        expected_control_digest="e" * 64,
        expected_keyring_digest=KEYRING_DIGEST,
        local_active_key_id="auth-key-old",
        local_accepted_key_ids=("auth-key-new", "auth-key-old"),
        valid_verifier_key_ids=("auth-key-new", "auth-key-old"),
        live_verifier_key_ids=(),
    ).ready is False


def test_live_old_key_row_blocks_key_removal_even_after_issuance_switch() -> None:
    record = _round_trip(
        _record(
            _replica(
                "replica-a",
                active_key_id="auth-key-new",
                accepted_key_ids=("auth-key-new",),
            ),
            _replica(
                "replica-b",
                active_key_id="auth-key-new",
                accepted_key_ids=("auth-key-new",),
            ),
        )
    )

    readiness = membership.evaluate_serving_membership(
        record,
        now=NOW,
        local_replica_id="replica-a",
        local_software_version="0.48.0",
        local_schema_version=4,
        expected_cell_id="cell-7",
        expected_control_digest=CONTROL_DIGEST,
        expected_keyring_digest=KEYRING_DIGEST,
        local_active_key_id="auth-key-new",
        local_accepted_key_ids=("auth-key-new",),
        valid_verifier_key_ids=("auth-key-new",),
        live_verifier_key_ids=("auth-key-old",),
    )

    assert readiness.ready is False
    assert readiness.code == "AUTHORIZATION_MEMBERSHIP_UNAVAILABLE"


def test_expired_active_key_cannot_be_self_waived_by_an_attestation() -> None:
    record = _round_trip(
        _record(_replica("replica-a"), _replica("replica-b"))
    )

    readiness = membership.evaluate_serving_membership(
        record,
        now=NOW,
        local_replica_id="replica-a",
        local_software_version="0.48.0",
        local_schema_version=4,
        expected_cell_id="cell-7",
        expected_control_digest=CONTROL_DIGEST,
        expected_keyring_digest=KEYRING_DIGEST,
        local_active_key_id="auth-key-old",
        local_accepted_key_ids=("auth-key-new", "auth-key-old"),
        valid_verifier_key_ids=("auth-key-new",),
        live_verifier_key_ids=(),
    )

    assert readiness.ready is False
    assert readiness.code == "AUTHORIZATION_MEMBERSHIP_UNAVAILABLE"


def test_maximum_session_ttl_plus_skew_boundary_is_exact() -> None:
    exact_expiry = NOW - 10 + membership.MAX_ATTESTATION_TTL_SECONDS
    exact = _round_trip(
        replace(
            _record(
                _replica("replica-a", expires_at=exact_expiry),
                _replica("replica-b", expires_at=exact_expiry),
            ),
            expires_at=exact_expiry,
        )
    )

    assert _readiness(exact).ready is True

    overlong_expiry = exact_expiry + 1
    overlong = replace(
        _record(
            _replica("replica-a", expires_at=overlong_expiry),
            _replica("replica-b", expires_at=overlong_expiry),
        ),
        expires_at=overlong_expiry,
    )
    with pytest.raises(membership.ServingMembershipUnavailable):
        membership.encode_serving_membership(overlong, verifier_keys=KEYS)


def test_replica_removal_requires_drain_stop_ack_and_committed_successors() -> None:
    epoch_7 = _round_trip(
        _record(_replica("replica-a"), _replica("replica-b"))
    )
    illegal_epoch_8 = _round_trip(
        _record(
            _replica("replica-a", epoch=8),
            epoch=8,
            previous_epoch_digest=membership.serving_membership_digest(
                membership.encode_serving_membership(epoch_7, verifier_keys=KEYS)
            ),
        )
    )
    with pytest.raises(membership.ServingMembershipUnavailable):
        membership.validate_membership_successor(epoch_7, illegal_epoch_8, now=NOW)

    draining_epoch_8 = _round_trip(
        _record(
            _replica("replica-a", epoch=8),
            _replica(
                "replica-b",
                epoch=8,
                state="DRAINING",
                issuance_stopped=True,
            ),
            epoch=8,
            previous_epoch_digest=membership.serving_membership_digest(
                membership.encode_serving_membership(epoch_7, verifier_keys=KEYS)
            ),
        )
    )
    membership.validate_membership_successor(epoch_7, draining_epoch_8, now=NOW)

    acknowledged_epoch_9 = _round_trip(
        _record(
            _replica("replica-a", epoch=9),
            _replica(
                "replica-b",
                epoch=9,
                state="DRAINING",
                issuance_stopped=True,
                no_in_flight=True,
            ),
            epoch=9,
            previous_epoch_digest=membership.serving_membership_digest(
                membership.encode_serving_membership(draining_epoch_8, verifier_keys=KEYS)
            ),
        )
    )
    membership.validate_membership_successor(draining_epoch_8, acknowledged_epoch_9, now=NOW)

    removed_epoch_10 = _round_trip(
        _record(
            _replica("replica-a", epoch=10),
            epoch=10,
            previous_epoch_digest=membership.serving_membership_digest(
                membership.encode_serving_membership(acknowledged_epoch_9, verifier_keys=KEYS)
            ),
        )
    )
    membership.validate_membership_successor(acknowledged_epoch_9, removed_epoch_10, now=NOW)


def test_rejoin_must_attest_the_current_epoch_before_serving() -> None:
    current = _round_trip(
        _record(
            _replica("replica-a"),
            _replica(
                "replica-b",
                state="DRAINING",
                issuance_stopped=True,
                no_in_flight=True,
            ),
        )
    )
    stale_rejoin = _record(
        _replica("replica-a", epoch=8),
        _replica("replica-b", epoch=7),
        epoch=8,
        previous_epoch_digest=membership.serving_membership_digest(
            membership.encode_serving_membership(current, verifier_keys=KEYS)
        ),
    )

    with pytest.raises(membership.ServingMembershipUnavailable):
        _round_trip(stale_rejoin)

    current_rejoin = _round_trip(
        _record(
            _replica("replica-a", epoch=8),
            _replica("replica-b", epoch=8),
            epoch=8,
            previous_epoch_digest=membership.serving_membership_digest(
                membership.encode_serving_membership(current, verifier_keys=KEYS)
            ),
        )
    )
    membership.validate_membership_successor(current, current_rejoin, now=NOW)
