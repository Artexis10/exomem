"""Pure successor transformation; authentication/CAS belong to the caller."""

from __future__ import annotations

import json

import pytest
from test_governance_migration_membership import (
    NOW,
    TARGET,
    _identity,
    _run,
    _runtime_verify,
    _source,
)

from exomem_provisioner import authorization_membership as membership
from exomem_provisioner.lifecycle import MetadataConflict


@pytest.fixture
def serving():
    migrated = _run(_source(3, enrolled=True), "commit")
    bundle = membership.transition_hosted_authorization_bundle(
        migrated.files,
        **_identity(4),
        target_state="SERVING",
        target_no_in_flight=False,
        now=NOW + 4,
    )
    kwargs = {
        **_identity(4),
        "expected_registry_attachment_id": bundle.registry_attachment_id,
        "activation_store_id": TARGET["activation_store_id"],
        "predecessor_epoch": TARGET["activation_epoch"],
        "predecessor_digest": TARGET["activation_state_digest"],
        "successor_epoch": TARGET["activation_epoch"] + 1,
        "successor_digest": "f" * 64,
        "now": NOW + 5,
    }
    return bundle, kwargs


def test_successor_changes_only_signed_activation_fields(serving):
    source, kwargs = serving
    result = membership.advance_hosted_activation_bundle(source.files, **kwargs)
    before, after = json.loads(source.control), json.loads(result.control)
    for key in ("activation_epoch", "activation_state_digest", "mac"):
        before.pop(key)
        after.pop(key)
    assert before == after
    assert result.keyring == source.keyring
    assert result.membership == source.membership
    assert result.revision != source.revision
    control, _ = _runtime_verify(result, now=NOW + 5)
    assert control.activation_epoch == kwargs["successor_epoch"]
    assert control.activation_state_digest == kwargs["successor_digest"]
    assert result.expires_at == source.expires_at


def test_exact_successor_replay_preserves_every_byte(serving):
    source, kwargs = serving
    result = membership.advance_hosted_activation_bundle(source.files, **kwargs)
    assert membership.advance_hosted_activation_bundle(result.files, **kwargs).files == result.files


@pytest.mark.parametrize(
    "field,value",
    [
        ("activation_store_id", "foreign-store"),
        ("predecessor_epoch", 1),
        ("predecessor_digest", "1" * 64),
        ("successor_epoch", 0),
        ("successor_epoch", True),
        ("successor_epoch", 1 << 63),
        ("successor_epoch", 500),
        ("successor_digest", "F" * 64),
        ("successor_digest", "f" * 63),
        ("expected_registry_attachment_id", "foreign-attachment"),
        ("expected_cell_id", "foreign-cell"),
        ("expected_logical_vault_id", "foreign-vault"),
    ],
)
def test_invalid_or_foreign_successor_is_refused(serving, field, value):
    source, kwargs = serving
    original = dict(source.files)
    with pytest.raises(MetadataConflict):
        membership.advance_hosted_activation_bundle(source.files, **{**kwargs, field: value})
    assert source.files == original


def test_expired_authority_cannot_acknowledge_or_renew(serving):
    source, kwargs = serving
    with pytest.raises(MetadataConflict):
        membership.advance_hosted_activation_bundle(
            source.files, **{**kwargs, "now": source.expires_at + 1}
        )


def test_renewal_fields_are_preserved_from_current_bundle(serving):
    source, kwargs = serving
    renewed = membership.transition_hosted_authorization_bundle(
        source.files,
        **_identity(4),
        target_state="SERVING",
        target_no_in_flight=False,
        renew=True,
        now=NOW + 6,
    )
    result = membership.advance_hosted_activation_bundle(
        renewed.files, **{**kwargs, "now": NOW + 7}
    )
    assert result.keyring == renewed.keyring
    assert result.membership == renewed.membership
    assert result.epoch == renewed.epoch
    assert result.expires_at == renewed.expires_at
