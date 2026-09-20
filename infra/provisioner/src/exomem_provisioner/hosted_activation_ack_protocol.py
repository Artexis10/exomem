# Generated from src/exomem/hosted_activation_ack_protocol.py; do not edit.

"""Strict JSON codec for the hosted activation acknowledgement v1 wire contract."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping

PROTOCOL = "exomem.hosted-activation-ack/v1"
_MAX_INTEGER = (1 << 63) - 1
_MAX_DEPTH = 8
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,511}\Z")
_ERROR_CODES = frozenset(
    "MALFORMED_REQUEST AUTHENTICATION_FAILED ACTIVATION_CONFLICT ACK_CAPACITY_EXCEEDED "
    "ACK_UNAVAILABLE ACK_DEADLINE_EXCEEDED ACK_PENDING KEYRING_NOT_AVAILABLE "
    "ACKNOWLEDGED ACK_READY".split()
)
_LIMITS = {
    "proofRequest": 8 * 1024,
    "proofResponse": 16 * 1024,
    "udsCheckRequest": 8 * 1024,
    "udsAckRequest": 8 * 1024,
    "udsResponse": 8 * 1024,
    "httpAckRequest": 8 * 1024,
    "httpBundleResponse": 192 * 1024,
    "errorResponse": 8 * 1024,
}


class ProtocolError(ValueError):
    """Content-free refusal for a malformed wire message."""

    code = "MALFORMED_REQUEST"

    def __init__(self) -> None:
        super().__init__("malformed hosted activation acknowledgement message")


def _closed(value: object, fields: str) -> Mapping[str, object]:
    expected = set(fields.split())
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or any(not isinstance(field, str) for field in value)
    ):
        raise ProtocolError
    return value


def _protocol(value: object) -> None:
    if value != PROTOCOL:
        raise ProtocolError


def _digest(value: object) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProtocolError


def _identifier(value: object) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ProtocolError


def _integer(value: object, *, minimum: int = 1, maximum: int = _MAX_INTEGER) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProtocolError


def _enum(value: object, choices: frozenset[str]) -> None:
    if not isinstance(value, str) or value not in choices:
        raise ProtocolError


def _activation(value: object) -> None:
    item = _closed(value, "activation_store_id activation_epoch activation_state_digest")
    _identifier(item["activation_store_id"])
    _integer(item["activation_epoch"])
    _digest(item["activation_state_digest"])


def _publication(value: object) -> None:
    item = _closed(value, "publication_event_id predecessor successor")
    _identifier(item["publication_event_id"])
    _activation(item["predecessor"])
    _activation(item["successor"])


_PROOF_FIELDS = (
    "protocol challenge_id issued_at expires_at cell_id logical_vault_id "
    "registry_attachment_id attachment_epoch expected_bundle_revision publication"
)


def _proof(value: object, *, response: bool) -> None:
    fields = _PROOF_FIELDS + (" publication_evidence signing_key_id mac" if response else "")
    item = _closed(value, fields)
    _protocol(item["protocol"])
    _digest(item["challenge_id"])
    _integer(item["issued_at"])
    _integer(item["expires_at"])
    for field in ("cell_id", "logical_vault_id", "registry_attachment_id"):
        _identifier(item[field])
    _integer(item["attachment_epoch"])
    _digest(item["expected_bundle_revision"])
    _publication(item["publication"])
    if response:
        evidence = _closed(item["publication_evidence"], "component_kind component_sha256")
        _enum(
            evidence["component_kind"],
            frozenset({"hosted-mutation-child/v1", "hosted-mutation-commit/v1"}),
        )
        _digest(evidence["component_sha256"])
        _identifier(item["signing_key_id"])
        _digest(item["mac"])


def _uds_request(value: object, *, operation: str) -> None:
    item = _closed(value, "protocol request_id operation budget_ms publication")
    _protocol(item["protocol"])
    _digest(item["request_id"])
    if item["operation"] != operation:
        raise ProtocolError
    _integer(item["budget_ms"], maximum=5000)
    if operation == "check":
        if item["publication"] is not None:
            raise ProtocolError
    else:
        _publication(item["publication"])


def _uds_response(value: object) -> None:
    item = _closed(
        value,
        "protocol request_id status bundle_revision activation code retry_after_ms",
    )
    _protocol(item["protocol"])
    _digest(item["request_id"])
    _enum(
        item["status"], frozenset({"ready", "acknowledged", "pending", "conflict", "unavailable"})
    )
    if item["bundle_revision"] is not None:
        _digest(item["bundle_revision"])
    if item["activation"] is not None:
        _activation(item["activation"])
    _enum(item["code"], _ERROR_CODES)
    _integer(item["retry_after_ms"], minimum=0, maximum=5000)


def _http_ack_request(value: object) -> None:
    item = _closed(value, "protocol request_id budget_ms publication")
    _protocol(item["protocol"])
    _digest(item["request_id"])
    _integer(item["budget_ms"], maximum=5000)
    _publication(item["publication"])


def _custody_record(value: object) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 87382 or "=" in value:
        raise ProtocolError
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise ProtocolError from None
    if not 1 <= len(decoded) <= 64 * 1024:
        raise ProtocolError
    if base64.urlsafe_b64encode(decoded).rstrip(b"=") != encoded:
        raise ProtocolError


def _http_bundle(value: object) -> None:
    item = _closed(
        value,
        "protocol request_id outcome cell_id logical_vault_id registry_attachment_id "
        "attachment_epoch bundle_revision keyring_sha256 control_b64 serving_membership_b64",
    )
    _protocol(item["protocol"])
    _digest(item["request_id"])
    _enum(item["outcome"], frozenset({"advanced", "unchanged", "current"}))
    for field in ("cell_id", "logical_vault_id", "registry_attachment_id"):
        _identifier(item[field])
    _integer(item["attachment_epoch"])
    _digest(item["bundle_revision"])
    _digest(item["keyring_sha256"])
    _custody_record(item["control_b64"])
    _custody_record(item["serving_membership_b64"])


def _error_response(value: object) -> None:
    item = _closed(value, "protocol request_id code retry_after_ms")
    _protocol(item["protocol"])
    if item["request_id"] is None:
        if item["code"] != "MALFORMED_REQUEST":
            raise ProtocolError
    else:
        _digest(item["request_id"])
    _enum(item["code"], _ERROR_CODES)
    _integer(item["retry_after_ms"], minimum=0, maximum=5000)


_VALIDATORS: dict[str, Callable[[object], None]] = {
    "proofRequest": lambda value: _proof(value, response=False),
    "proofResponse": lambda value: _proof(value, response=True),
    "udsCheckRequest": lambda value: _uds_request(value, operation="check"),
    "udsAckRequest": lambda value: _uds_request(value, operation="ack"),
    "udsResponse": _uds_response,
    "httpAckRequest": _http_ack_request,
    "httpBundleResponse": _http_bundle,
    "errorResponse": _error_response,
}


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ProtocolError
        result[name] = value
    return result


def _parse_integer(raw: str) -> int:
    digits = raw[1:] if raw.startswith("-") else raw
    if len(digits) > 19:
        raise ProtocolError
    return int(raw)


def _reject_number(_: str) -> None:
    raise ProtocolError


def _check_depth(value: object, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise ProtocolError
    if isinstance(value, dict):
        for nested in value.values():
            _check_depth(nested, depth + 1)
    elif isinstance(value, list):
        for nested in value:
            _check_depth(nested, depth + 1)


def _validator(kind: str) -> tuple[Callable[[object], None], int]:
    try:
        return _VALIDATORS[kind], _LIMITS[kind]
    except (KeyError, TypeError):
        raise ProtocolError from None


def decode_message(raw: bytes, kind: str) -> dict[str, object]:
    """Decode and validate one bounded strict-JSON protocol message."""

    validator, limit = _validator(kind)
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= limit:
        raise ProtocolError
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object,
            parse_float=_reject_number,
            parse_int=_parse_integer,
            parse_constant=_reject_number,
        )
        _check_depth(value)
        validator(value)
    except (json.JSONDecodeError, ProtocolError, RecursionError, UnicodeDecodeError, ValueError):
        raise ProtocolError from None
    return dict(value)


def encode_message(value: Mapping[str, object], kind: str) -> bytes:
    """Validate and encode one canonical compact UTF-8 protocol message."""

    validator, limit = _validator(kind)
    try:
        validator(value)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (ProtocolError, TypeError, UnicodeEncodeError, ValueError):
        raise ProtocolError from None
    if len(encoded) > limit:
        raise ProtocolError
    return encoded
