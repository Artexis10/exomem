from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace

import pytest

from exomem.governance import authorization_custody as custody
from exomem.hosted_activation_ack_protocol import PROTOCOL
from exomem.hosted_activation_delivery import (
    CustodyDeliveryUnavailable,
    generation_relation,
    parse_delivery_bundle,
    public_response_bundle,
)

NOW = 1_790_000_000


def bundle(
    *,
    activation=1,
    membership=1,
    issued=NOW,
    key=b"a" * 32,
    state="SERVING",
    attachment_epoch=1,
    activation_digest=None,
):
    keyring = custody.AuthorizationKeyring(
        version=1,
        keyring_id="keyring-1",
        cell_id="cell-1",
        logical_vault_id="vault-1",
        active_key_id="key-1",
        accepted_keys=(
            custody.AuthorizationVerifierKey(
                key_id="key-1",
                key=key,
                not_before=NOW - 1000,
                not_after=NOW + 100000,
            ),
        ),
    )
    control = custody.AuthorizationControlRecord(
        version=1,
        keyring_id=keyring.keyring_id,
        cell_id="cell-1",
        logical_vault_id="vault-1",
        registry_attachment_id="attachment-1",
        attachment_epoch=attachment_epoch,
        governance_enrolled=True,
        activation_store_id="store-1",
        activation_epoch=activation,
        activation_state_digest=activation_digest or str(activation % 10) * 64,
        serving_membership_epoch=membership,
        serving_membership_digest="0" * 64,
        issued_at=issued,
        expires_at=issued + 3600,
        signing_key_id="key-1",
    )
    raw_membership = custody._standalone_membership_bytes(
        keyring=keyring,
        control=control,
        replica_id="replica-1",
        state=state,
        issuance_stopped=state != "SERVING",
        no_in_flight=state != "SERVING",
        previous_epoch_digest="a" * 64 if membership > 1 else None,
    )
    control = replace(control, serving_membership_digest=hashlib.sha256(raw_membership).hexdigest())
    raw_keyring = json.dumps(
        {
            "version": 1,
            "keyring_id": keyring.keyring_id,
            "cell_id": keyring.cell_id,
            "logical_vault_id": keyring.logical_vault_id,
            "active_key_id": "key-1",
            "accepted_keys": [
                {
                    "key_id": "key-1",
                    "key": base64.urlsafe_b64encode(key).rstrip(b"=").decode(),
                    "not_before": NOW - 1000,
                    "not_after": NOW + 100000,
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "keyring.json": raw_keyring,
        "control.json": custody._signed_control_bytes(control, signing_key=key),
        "serving-membership.json": raw_membership,
    }


def test_parse_verifies_the_complete_signed_bundle():
    parsed = parse_delivery_bundle(bundle(), now=NOW)
    assert parsed.control.activation_epoch == 1
    assert parsed.membership.epoch == 1
    assert (
        parsed.revision
        == hashlib.sha256(
            parsed.files["keyring.json"]
            + parsed.files["control.json"]
            + parsed.files["serving-membership.json"]
        ).hexdigest()
    )
    with pytest.raises(TypeError):
        parsed.files["control.json"] = b"changed"


@pytest.mark.parametrize("filename", ["keyring.json", "control.json", "serving-membership.json"])
def test_substituted_record_is_refused(filename):
    files = bundle()
    files[filename] = bundle(key=b"b" * 32)[filename]
    with pytest.raises(CustodyDeliveryUnavailable):
        parse_delivery_bundle(files, now=NOW)


@pytest.mark.parametrize(
    "a,m,expected",
    [
        (1, 1, "same"),
        (2, 1, "advance"),
        (1, 2, "advance"),
        (2, 2, "advance"),
    ],
)
def test_two_dimensional_generation_order(a, m, expected):
    original = parse_delivery_bundle(bundle(), now=NOW)
    candidate = parse_delivery_bundle(bundle(activation=a, membership=m), now=NOW)
    assert generation_relation(original, candidate) == expected
    if expected == "advance":
        assert generation_relation(candidate, original) == "stale"


def test_incomparable_activation_and_renewal_require_authority():
    activated = parse_delivery_bundle(bundle(activation=2), now=NOW)
    renewed = parse_delivery_bundle(bundle(membership=2), now=NOW)
    assert generation_relation(activated, renewed) == "incomparable"
    assert generation_relation(renewed, activated) == "incomparable"


def test_equal_epoch_different_signed_digest_is_a_conflict():
    original = parse_delivery_bundle(bundle(), now=NOW)
    changed = bundle()
    control = replace(original.control, activation_state_digest="e" * 64)
    changed["control.json"] = custody._signed_control_bytes(control, signing_key=b"a" * 32)
    with pytest.raises(CustodyDeliveryUnavailable):
        generation_relation(original, parse_delivery_bundle(changed, now=NOW))


def test_historical_bundle_only_supplies_a_comparison_floor():
    files = bundle()
    with pytest.raises(CustodyDeliveryUnavailable):
        parse_delivery_bundle(files, now=NOW + 4000)
    old = parse_delivery_bundle(files, now=NOW + 4000, historical=True)
    new = parse_delivery_bundle(bundle(membership=2, issued=NOW + 4000), now=NOW + 4000)
    assert generation_relation(old, new) == "advance"


def response(files):
    parsed = parse_delivery_bundle(files, now=NOW)
    return {
        "protocol": PROTOCOL,
        "request_id": "a" * 64,
        "outcome": "advanced",
        "cell_id": "cell-1",
        "logical_vault_id": "vault-1",
        "registry_attachment_id": "attachment-1",
        "attachment_epoch": 1,
        "bundle_revision": parsed.revision,
        "keyring_sha256": hashlib.sha256(files["keyring.json"]).hexdigest(),
        "control_b64": base64.urlsafe_b64encode(files["control.json"]).rstrip(b"=").decode(),
        "serving_membership_b64": base64.urlsafe_b64encode(files["serving-membership.json"])
        .rstrip(b"=")
        .decode(),
    }


def test_fast_response_uses_only_a_digest_matching_projected_keyring():
    files = bundle(activation=2)
    parsed = public_response_bundle(response(files), keyrings=[files["keyring.json"]], now=NOW)
    assert parsed.control.activation_epoch == 2
    assert parsed.files == files
    with pytest.raises(CustodyDeliveryUnavailable):
        public_response_bundle(
            response(files), keyrings=[bundle(key=b"b" * 32)["keyring.json"]], now=NOW
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("cell_id", "foreign-cell"),
        ("registry_attachment_id", "foreign-attachment"),
        ("attachment_epoch", 2),
        ("bundle_revision", "f" * 64),
    ],
)
def test_fast_response_identity_and_revision_are_not_self_asserted(field, value):
    files = bundle()
    forged = {**response(files), field: value}
    with pytest.raises(CustodyDeliveryUnavailable):
        public_response_bundle(forged, keyrings=[files["keyring.json"]], now=NOW)
