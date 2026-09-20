from __future__ import annotations

import copy
import hashlib
import hmac
import json
import sqlite3

import pytest
from test_hosted_activation_delivery import NOW, bundle
from test_hosted_mutation_journal import _committed_fixture

from exomem.governance import hosted_mutation_journal as journal
from exomem.hosted_activation_ack_protocol import PROTOCOL, encode_message
from exomem.hosted_activation_delivery import parse_delivery_bundle
from exomem.hosted_activation_proof import ActivationProofUnavailable, prove_publication


@pytest.fixture
def proof_case(tmp_path):
    governance, store, _ = _committed_fixture(tmp_path)
    installed = parse_delivery_bundle(
        bundle(activation=7, activation_digest="a" * 64, attachment_epoch=7), now=NOW
    )
    request = {
        "protocol": PROTOCOL,
        "challenge_id": "f" * 64,
        "issued_at": NOW,
        "expires_at": NOW + 3,
        "cell_id": "cell-1",
        "logical_vault_id": "vault-1",
        "registry_attachment_id": "attachment-1",
        "attachment_epoch": 7,
        "expected_bundle_revision": "e" * 64,
        "publication": {
            "publication_event_id": "publication-1",
            "predecessor": {
                "activation_store_id": "store-1",
                "activation_epoch": 7,
                "activation_state_digest": "a" * 64,
            },
            "successor": {
                "activation_store_id": "store-1",
                "activation_epoch": 8,
                "activation_state_digest": "b" * 64,
            },
        },
    }
    arguments = dict(
        installed=installed,
        governance_db_path=governance,
        idempotency_db_path=store.path,
        expected_cell_id="cell-1",
        expected_logical_vault_id="vault-1",
        expected_replica_id="replica-1",
        now=NOW,
    )
    return request, arguments


def test_proof_signs_actual_committed_evidence_during_activation_mismatch(proof_case):
    request, arguments = proof_case
    result = prove_publication(request, **arguments)
    assert result["publication"] == request["publication"]
    assert result["publication_evidence"]["component_kind"] == "hosted-mutation-commit/v1"
    raw = encode_message(result, "proofResponse")
    assert b"Notes/0.md" not in raw
    signed = dict(result)
    mac = signed.pop("mac")
    expected = hmac.new(
        b"a" * 32,
        b"exomem.hosted-activation-proof/v1\x00"
        + json.dumps(
            signed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    assert mac == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("cell_id", "foreign-cell"),
        ("logical_vault_id", "foreign-vault"),
        ("attachment_epoch", 8),
        ("registry_attachment_id", "foreign-attachment"),
        ("issued_at", NOW + 2),
        ("expires_at", NOW),
        ("expires_at", NOW + 4),
    ],
)
def test_foreign_or_stale_challenge_never_gets_signed(proof_case, field, value):
    request, arguments = proof_case
    request[field] = value
    with pytest.raises(ActivationProofUnavailable):
        prove_publication(request, **arguments)


def test_forged_publication_selector_never_gets_signed(proof_case):
    request, arguments = proof_case
    request = copy.deepcopy(request)
    request["publication"]["successor"]["activation_state_digest"] = "c" * 64
    with pytest.raises(ActivationProofUnavailable):
        prove_publication(request, **arguments)


def test_uncommitted_or_tampered_component_never_gets_signed(proof_case):
    request, arguments = proof_case
    with sqlite3.connect(arguments["governance_db_path"]) as connection:
        connection.execute(
            "UPDATE governance_operation_components SET value_hash=? WHERE component_kind LIKE 'hosted-mutation-%'",
            ("0" * 64,),
        )
    with pytest.raises(ActivationProofUnavailable):
        prove_publication(request, **arguments)


def test_signer_requires_returned_evidence_to_match_every_selector_binding(proof_case, monkeypatch):
    request, arguments = proof_case
    proven = journal.read_committed_publication_evidence_for_selector(
        governance_db_path=arguments["governance_db_path"],
        idempotency_db_path=arguments["idempotency_db_path"], selector=request["publication"],
    )
    assert proven is not None
    monkeypatch.setattr(journal, "read_committed_publication_evidence_for_selector", lambda **_: proven)
    for field, value in (("publication_event_id", "different-publication"), ("activation_epoch", 9), ("activation_state_digest", "c" * 64)):
        wrong = copy.deepcopy(request)
        if field == "publication_event_id":
            wrong["publication"][field] = value
        else:
            wrong["publication"]["successor"][field] = value
        with pytest.raises(ActivationProofUnavailable):
            prove_publication(wrong, **arguments)
