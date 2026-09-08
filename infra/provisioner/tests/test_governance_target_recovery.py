from __future__ import annotations

import importlib.util
import json

import pytest
from test_governance_migration_membership import _runtime_verify
from test_governance_migration_membership import _source as _migration_source
from test_governance_readiness import NOW, _identity, _serving_bundle

from exomem_provisioner import authorization_membership as membership
from exomem_provisioner.lifecycle import MetadataConflict


def _recover(source, *, now, issued_at=None, identity=None):
    assert importlib.util.find_spec("exomem_provisioner.governance_target_recovery") is not None
    from exomem_provisioner.governance_target_recovery import recover_expired_serving_bundle

    return recover_expired_serving_bundle(
        source.files,
        **(
            {key: value for key, value in _identity(4).items() if key != "expected_schema_version"}
            | (identity or {})
        ),
        now=now,
        successor_issued_at=now if issued_at is None else issued_at,
    )


def test_target_recovery_is_a_pure_exact_draining_successor_not_a_serving_renewal():
    source = _serving_bundle()
    files = source.files
    current = source.expires_at + 1
    recovered = _recover(source, now=current)
    control, record = _runtime_verify(recovered, now=current)
    assert source.files == files
    assert recovered.keyring == source.keyring
    assert recovered.epoch == source.epoch + 1
    assert record.previous_epoch_digest == source.membership_digest
    assert recovered.replica_state == "DRAINING"
    assert recovered.issuance_stopped is recovered.no_in_flight is True
    assert recovered.governance_enrolled is True
    for field in (
        "registry_attachment_id",
        "activation_store_id",
        "activation_epoch",
        "activation_state_digest",
        "membership_schema_version",
        "software_version",
    ):
        assert getattr(recovered, field) == getattr(source, field)
    before = json.loads(source.control)
    after = json.loads(recovered.control)
    assert {key for key in before if before[key] != after[key]} == {
        "serving_membership_epoch",
        "serving_membership_digest",
        "issued_at",
        "expires_at",
        "mac",
    }
    assert control.issued_at == current
    assert recovered.expires_at == current + membership.DEFAULT_ATTESTATION_TTL_SECONDS


def test_retry_rebuilds_the_same_exact_bytes_even_after_the_draining_window_elapses():
    source = _serving_bundle()
    issued_at = source.expires_at + 1
    expected = _recover(source, now=issued_at)
    assert _recover(source, now=expected.expires_at + 500, issued_at=issued_at) == expected
    # Replanning an uncertain CAS would produce a different legitimate successor.
    assert _recover(source, now=issued_at + 1).revision != expected.revision


def test_recovery_caps_the_window_at_key_expiry(monkeypatch):
    monkeypatch.setattr(membership, "_KEY_TTL_SECONDS", 3670)
    source = _serving_bundle()
    recovered = _recover(source, now=source.expires_at + 1)
    assert recovered.expires_at == NOW + 3670
    _runtime_verify(recovered, now=source.expires_at + 1)


def test_recovery_checks_key_validity_now_not_at_the_committed_issuance_time(monkeypatch):
    monkeypatch.setattr(membership, "_KEY_TTL_SECONDS", 3670)
    source = _serving_bundle()
    issued_at = source.expires_at + 1
    _recover(source, now=issued_at)
    with pytest.raises(MetadataConflict):
        _recover(source, now=NOW + 3670, issued_at=issued_at)


@pytest.mark.parametrize("clock", [NOW + 3, NOW + 4, NOW + 10])
def test_recovery_does_not_accept_future_or_unexpired_source(clock):
    with pytest.raises(MetadataConflict):
        _recover(_serving_bundle(), now=clock)


@pytest.mark.parametrize("issued_at", [True, 0, -1, "1900000000", 1 << 63, NOW, NOW + 99999])
def test_recovery_requires_a_bounded_committed_time_after_source_expiry(issued_at):
    source = _serving_bundle()
    with pytest.raises(MetadataConflict):
        _recover(source, now=source.expires_at + 1, issued_at=issued_at)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_cell_id", "foreign-cell"),
        ("expected_logical_vault_id", "foreign-vault"),
        ("expected_replica_id", "foreign-pod"),
        ("expected_software_version", "foreign-version"),
        ("expected_recovery_envelope", "foreign-envelope"),
    ],
)
def test_recovery_does_not_change_the_expected_identity(field, value):
    source = _serving_bundle()
    with pytest.raises(MetadataConflict):
        _recover(source, now=source.expires_at + 1, identity={field: value})


def test_recovery_does_not_accept_drained_custody_as_an_expired_serving_predecessor():
    source = _serving_bundle()
    drained = membership.transition_hosted_authorization_bundle(
        source.files,
        **_identity(4),
        target_state="DRAINING",
        target_no_in_flight=True,
        now=NOW + 5,
    )
    with pytest.raises(MetadataConflict):
        _recover(drained, now=drained.expires_at + 1)


def test_ordinary_transition_still_refuses_expired_serving_to_draining():
    source = _serving_bundle()
    with pytest.raises(MetadataConflict, match="stale authorization membership cannot be renewed"):
        membership.transition_hosted_authorization_bundle(
            source.files,
            **_identity(4),
            target_state="DRAINING",
            target_no_in_flight=True,
            now=source.expires_at + 1,
        )


@pytest.mark.parametrize("schema,enrolled", [(3, False), (3, True), (4, False)])
def test_recovery_refuses_anything_except_an_enrolled_schema_four_source(schema, enrolled):
    source = _migration_source(schema, drained=True, enrolled=enrolled)
    serving = membership.transition_hosted_authorization_bundle(
        source.files,
        **(_identity(schema) | {"expected_software_version": None}),
        target_state="SERVING",
        target_no_in_flight=False,
        now=NOW + 4,
    )
    with pytest.raises(MetadataConflict):
        _recover(
            serving,
            now=serving.expires_at + 1,
            identity={"expected_software_version": serving.software_version},
        )
