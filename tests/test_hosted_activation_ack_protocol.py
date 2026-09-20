from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from exomem.hosted_activation_ack_protocol import ProtocolError, decode_message, encode_message

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "infra/contracts/exomem-hosted-activation-ack-v1.schema.json"
FIXTURES = ROOT / "tests/fixtures/hosted-activation-ack-v1"
PROTOCOL = "exomem.hosted-activation-ack/v1"


def _fixture(name: str = "final-commit-proof.json") -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _proof_request() -> dict[str, object]:
    value = _fixture()
    for field in ("publication_evidence", "signing_key_id", "mac"):
        value.pop(field)
    return value


def _publication() -> dict[str, object]:
    return copy.deepcopy(_fixture()["publication"])


def _messages() -> dict[str, dict[str, object]]:
    request_id = "c" * 64
    activation = copy.deepcopy(_fixture()["publication"]["successor"])
    return {
        "proofRequest": _proof_request(),
        "proofResponse": _fixture(),
        "udsCheckRequest": {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "operation": "check",
            "budget_ms": 5000,
            "publication": None,
        },
        "udsAckRequest": {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "operation": "ack",
            "budget_ms": 5000,
            "publication": _publication(),
        },
        "udsResponse": {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "status": "acknowledged",
            "bundle_revision": "d" * 64,
            "activation": activation,
            "code": "ACKNOWLEDGED",
            "retry_after_ms": 0,
        },
        "httpAckRequest": {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "budget_ms": 5000,
            "publication": _publication(),
        },
        "httpBundleResponse": {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "outcome": "advanced",
            "cell_id": "cell-ack-fixture",
            "logical_vault_id": "vault-ack-fixture",
            "registry_attachment_id": "hosted-attachment-v1-" + "a" * 64,
            "attachment_epoch": 7,
            "bundle_revision": "d" * 64,
            "keyring_sha256": "e" * 64,
            "control_b64": "e30",
            "serving_membership_b64": "e30",
        },
        "errorResponse": {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "code": "ACK_UNAVAILABLE",
            "retry_after_ms": 250,
        },
    }


@pytest.mark.parametrize("kind", sorted(_messages()))
def test_each_message_kind_round_trips_as_canonical_utf8(kind: str) -> None:
    value = _messages()[kind]

    encoded = encode_message(value, kind)

    assert encoded == json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert decode_message(encoded, kind) == value


@pytest.mark.parametrize(
    "fixture_name", ["intermediate-child-proof.json", "final-commit-proof.json"]
)
def test_proof_fixtures_validate_with_independent_codec_and_schema(
    fixture_name: str,
) -> None:
    raw = (FIXTURES / fixture_name).read_bytes()
    decoded = decode_message(raw, "proofResponse")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    Draft202012Validator({"$ref": "#/$defs/proofResponse", "$defs": schema["$defs"]}).validate(
        decoded
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"protocol":"exomem.hosted-activation-ack/v1","protocol":"duplicate"}',
        b"\xff",
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":1.0}',
        b'{"value":123456789012345678901234567890}',
        (b'{"unexpected":' + b"[" * 32 + b"0" + b"]" * 32 + b"}"),
    ],
)
def test_parser_rejects_ambiguous_or_unbounded_json(raw: bytes) -> None:
    with pytest.raises(ProtocolError) as error:
        decode_message(raw, "errorResponse")

    assert error.value.code == "MALFORMED_REQUEST"


def test_shape_validation_rejects_unknown_missing_nested_and_bool_integer_fields() -> None:
    valid = _messages()["udsAckRequest"]
    mutations = []
    mutations.append({**valid, "unexpected": "value"})
    missing = dict(valid)
    missing.pop("request_id")
    mutations.append(missing)
    nested = copy.deepcopy(valid)
    nested["publication"]["successor"]["unexpected"] = "value"
    mutations.append(nested)
    mutations.append({**valid, "budget_ms": True})
    mutations.append({**valid, "publication": None})

    for mutation in mutations:
        with pytest.raises(ProtocolError):
            encode_message(mutation, "udsAckRequest")


def test_protocol_errors_are_content_free() -> None:
    secret = "do-not-echo-this-value"
    with pytest.raises(ProtocolError) as error:
        decode_message(json.dumps({"secret": secret}).encode(), "errorResponse")

    assert error.value.code == "MALFORMED_REQUEST"
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    ("kind", "limit"),
    [
        ("proofRequest", 8 * 1024),
        ("proofResponse", 16 * 1024),
        ("udsCheckRequest", 8 * 1024),
        ("udsAckRequest", 8 * 1024),
        ("udsResponse", 8 * 1024),
        ("httpAckRequest", 8 * 1024),
        ("httpBundleResponse", 192 * 1024),
        ("errorResponse", 8 * 1024),
    ],
)
def test_decode_rejects_messages_over_the_kind_byte_limit(kind: str, limit: int) -> None:
    with pytest.raises(ProtocolError):
        decode_message(b" " * (limit + 1), kind)


def test_custody_records_require_strict_canonical_unpadded_base64url() -> None:
    bundle = _messages()["httpBundleResponse"]

    for invalid in ("", "e30=", "+w", "AB"):
        with pytest.raises(ProtocolError):
            encode_message({**bundle, "control_b64": invalid}, "httpBundleResponse")


def test_custody_records_accept_exactly_64_kib_and_reject_more() -> None:
    bundle = _messages()["httpBundleResponse"]
    at_limit = base64.urlsafe_b64encode(b"x" * (64 * 1024)).rstrip(b"=").decode("ascii")
    over_limit = base64.urlsafe_b64encode(b"x" * (64 * 1024 + 1)).rstrip(b"=").decode("ascii")

    encode_message({**bundle, "control_b64": at_limit}, "httpBundleResponse")
    with pytest.raises(ProtocolError):
        encode_message({**bundle, "control_b64": over_limit}, "httpBundleResponse")


def test_null_request_id_is_only_valid_for_malformed_request_errors() -> None:
    error = _messages()["errorResponse"]

    malformed = {**error, "request_id": None, "code": "MALFORMED_REQUEST"}
    assert decode_message(encode_message(malformed, "errorResponse"), "errorResponse") == malformed
    with pytest.raises(ProtocolError):
        encode_message({**malformed, "code": "AUTHENTICATION_FAILED"}, "errorResponse")


def test_status_code_correlation_remains_a_handler_policy() -> None:
    response = _messages()["udsResponse"]
    response["status"] = "ready"
    response["code"] = "ACK_PENDING"

    encode_message(response, "udsResponse")


def test_unknown_kind_and_non_bytes_input_fail_closed() -> None:
    with pytest.raises(ProtocolError):
        decode_message(b"{}", "unknown")
    with pytest.raises(ProtocolError):
        decode_message("{}", "errorResponse")  # type: ignore[arg-type]
