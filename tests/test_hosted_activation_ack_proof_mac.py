from __future__ import annotations

import copy
import hashlib
import hmac
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import hmac as cryptography_hmac

from exomem.hosted_activation_ack_protocol import decode_message, encode_message

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/hosted-activation-ack-v1"
VECTOR = FIXTURES / "proof-mac-vector.json"
DOMAIN = b"exomem.hosted-activation-proof/v1\x00"
PUBLIC_TEST_KEY = bytes(range(32))


def _proof(name: str) -> dict[str, object]:
    return decode_message((FIXTURES / name).read_bytes(), "proofResponse")


def _vector() -> dict[str, object]:
    return json.loads(VECTOR.read_text(encoding="utf-8"))


def _body(proof: dict[str, object]) -> bytes:
    validated = decode_message(encode_message(proof, "proofResponse"), "proofResponse")
    body = dict(validated)
    body.pop("mac")
    return json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _cryptography_verify(key: bytes, signed: bytes, expected_mac: str) -> None:
    verifier = cryptography_hmac.HMAC(key, hashes.SHA256())
    verifier.update(signed)
    verifier.verify(bytes.fromhex(expected_mac))


@pytest.mark.parametrize(
    "fixture_name", ["intermediate-child-proof.json", "final-commit-proof.json"]
)
def test_known_answer_is_shared_by_stdlib_and_cryptography(fixture_name: str) -> None:
    vector = _vector()
    proof = _proof(fixture_name)
    body = _body(proof)
    expected = vector["fixtures"][fixture_name]
    signed = DOMAIN + body

    assert vector["public_test_key_hex"] == PUBLIC_TEST_KEY.hex()
    assert vector["mac_domain_hex"] == DOMAIN.hex()
    assert proof["signing_key_id"] == "key-ack-fixture"
    assert hashlib.sha256(body).hexdigest() == expected["signed_body_sha256"]
    assert hmac.new(PUBLIC_TEST_KEY, signed, hashlib.sha256).hexdigest() == expected["expected_mac"]
    assert proof["mac"] == expected["expected_mac"]
    _cryptography_verify(PUBLIC_TEST_KEY, signed, expected["expected_mac"])


def _change_challenge(proof: dict[str, object]) -> None:
    proof["challenge_id"] = "0" * 64


def _change_key_id(proof: dict[str, object]) -> None:
    proof["signing_key_id"] = "key-ack-fixture-rotated"


def _change_component_kind(proof: dict[str, object]) -> None:
    proof["publication_evidence"]["component_kind"] = "hosted-mutation-child/v1"


def _change_successor_tuple(proof: dict[str, object]) -> None:
    proof["publication"]["successor"]["activation_epoch"] += 1


@pytest.mark.parametrize(
    "mutate",
    [_change_challenge, _change_key_id, _change_component_kind, _change_successor_tuple],
    ids=["challenge", "key-id", "child-vs-commit", "successor-tuple"],
)
def test_each_signed_binding_invalidates_the_known_mac(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    proof = _proof("final-commit-proof.json")
    expected_mac = proof["mac"]
    changed = copy.deepcopy(proof)
    mutate(changed)

    with pytest.raises(InvalidSignature):
        _cryptography_verify(PUBLIC_TEST_KEY, DOMAIN + _body(changed), expected_mac)


def test_wrong_key_rejects_a_known_answer() -> None:
    proof = _proof("final-commit-proof.json")

    with pytest.raises(InvalidSignature):
        _cryptography_verify(bytes(reversed(PUBLIC_TEST_KEY)), DOMAIN + _body(proof), proof["mac"])
