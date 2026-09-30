"""Bounded private preparation contract for exact hosted mutation recovery.

Bindings from this module authenticate prepared data only. They do not prove
that a canonical effect committed and cannot be rendered as a public terminal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping

VERSION = "exomem.prepared-canonical-mutation/v1"
MAX_CANONICAL_BYTES = 4 * 1024 * 1024
MAX_RECIPE_DEPTH = 32
MAX_RECIPE_NODES = 16384
MAX_REQUIRED_CHILDREN = 256

_MAX_SIGNED_SQLITE_INTEGER = 9223372036854775807
_DESCRIPTOR_DOMAIN = b"exomem.prepared-canonical-mutation-descriptor-binding/v1\x00"
_CHILD_RESULT_DOMAIN = b"exomem.prepared-canonical-mutation-child-result-binding/v1\x00"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPT_TOKEN = re.compile(r"[0-9a-f]{24}\Z")
_CHILD_KINDS = frozenset({"catalog", "policy", "sidecar"})
_DESCRIPTOR_FIELDS = (
    "version",
    "scoped_idempotency_digest",
    "command_digest",
    "attempt_id",
    "commit_token",
    "command",
    "selector_digest",
    "cell_id",
    "logical_vault_id",
    "registry_attachment_id",
    "attachment_epoch",
    "activation_store_id",
    "required_children",
    "result_recipe",
)


class RecoveryDescriptorError(ValueError):
    """Prepared recovery metadata is malformed or exceeds its private bounds."""


def _closed(value: object, fields: tuple[str, ...]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise RecoveryDescriptorError("invalid closed object")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise RecoveryDescriptorError("invalid digest")
    return value


def _attempt_token(value: object) -> str:
    if not isinstance(value, str) or _ATTEMPT_TOKEN.fullmatch(value) is None:
        raise RecoveryDescriptorError("invalid attempt token")
    return value


def _custody_identifier(value: object) -> str:
    # Keep this byte-for-byte grammar aligned with authorization_custody._bounded_identifier.
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 0x20 for character in value)
    ):
        raise RecoveryDescriptorError("invalid custody identity")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise RecoveryDescriptorError("invalid custody identity") from None
    if len(encoded) > 512:
        raise RecoveryDescriptorError("invalid custody identity")
    return value


def _positive_sqlite_integer(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_SIGNED_SQLITE_INTEGER
    ):
        raise RecoveryDescriptorError("invalid positive SQLite integer")
    return value


def _canonical_json(value: object, *, maximum_bytes: int = MAX_CANONICAL_BYTES) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError):
        raise RecoveryDescriptorError("invalid JSON data") from None
    if not 1 <= len(encoded) <= maximum_bytes:
        raise RecoveryDescriptorError("canonical JSON exceeds its bound")
    return encoded


def _validate_json_value(
    value: object,
    *,
    depth: int,
    counter: list[int],
    active: set[int],
) -> object:
    if depth > MAX_RECIPE_DEPTH:
        raise RecoveryDescriptorError("recipe exceeds its depth bound")
    counter[0] += 1
    if counter[0] > MAX_RECIPE_NODES:
        raise RecoveryDescriptorError("recipe exceeds its node bound")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RecoveryDescriptorError("non-finite JSON number is invalid")
        return value
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise RecoveryDescriptorError("cyclic JSON is invalid")
        active.add(identity)
        try:
            return [
                _validate_json_value(
                    item,
                    depth=depth + 1,
                    counter=counter,
                    active=active,
                )
                for item in value
            ]
        finally:
            active.remove(identity)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise RecoveryDescriptorError("cyclic JSON is invalid")
        active.add(identity)
        try:
            result: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise RecoveryDescriptorError("JSON object keys must be strings")
                result[key] = _validate_json_value(
                    item,
                    depth=depth + 1,
                    counter=counter,
                    active=active,
                )
            return result
        finally:
            active.remove(identity)
    raise RecoveryDescriptorError("value is not JSON data")


def _validate_children(value: object) -> tuple[list[dict[str, object]], set[str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_REQUIRED_CHILDREN:
        raise RecoveryDescriptorError("invalid required child manifest")
    children: list[dict[str, object]] = []
    identifiers: list[str] = []
    requirements: dict[str, list[str]] = {}
    for raw_child in value:
        child = _closed(raw_child, ("id", "kind", "plan_sha256", "requires"))
        child_id = _custody_identifier(child["id"])
        if child_id in requirements:
            raise RecoveryDescriptorError("duplicate required child")
        kind = child["kind"]
        if not isinstance(kind, str) or kind not in _CHILD_KINDS:
            raise RecoveryDescriptorError("unknown required child kind")
        plan_sha256 = _digest(child["plan_sha256"])
        raw_requires = child["requires"]
        if not isinstance(raw_requires, list):
            raise RecoveryDescriptorError("invalid child prerequisites")
        requires = [_custody_identifier(item) for item in raw_requires]
        if len(set(requires)) != len(requires):
            raise RecoveryDescriptorError("duplicate child prerequisite")
        requirements[child_id] = requires
        identifiers.append(child_id)
        children.append(
            {
                "id": child_id,
                "kind": kind,
                "plan_sha256": plan_sha256,
                "requires": requires,
            }
        )

    known = set(identifiers)
    if any(required not in known for requires in requirements.values() for required in requires):
        raise RecoveryDescriptorError("unknown child prerequisite")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(child_id: str) -> None:
        if child_id in visiting:
            raise RecoveryDescriptorError("cyclic child prerequisites")
        if child_id in visited:
            return
        visiting.add(child_id)
        for required in requirements[child_id]:
            visit(required)
        visiting.remove(child_id)
        visited.add(child_id)

    for child_id in identifiers:
        visit(child_id)

    positions = {child_id: index for index, child_id in enumerate(identifiers)}
    if any(
        positions[required] >= positions[child_id]
        for child_id, requires in requirements.items()
        for required in requires
    ):
        raise RecoveryDescriptorError("child prerequisites must appear earlier")
    return children, known


def _validate_recipe(
    value: object,
    *,
    child_ids: set[str],
    depth: int = 1,
    counter: list[int] | None = None,
    active: set[int] | None = None,
) -> dict[str, object]:
    if depth > MAX_RECIPE_DEPTH:
        raise RecoveryDescriptorError("recipe exceeds its depth bound")
    if counter is None:
        counter = [0]
    if active is None:
        active = set()
    counter[0] += 1
    if counter[0] > MAX_RECIPE_NODES:
        raise RecoveryDescriptorError("recipe exceeds its node bound")
    if not isinstance(value, Mapping):
        raise RecoveryDescriptorError("invalid recipe node")
    identity = id(value)
    if identity in active:
        raise RecoveryDescriptorError("cyclic recipe is invalid")
    active.add(identity)
    try:
        kind = value.get("kind")
        if kind == "literal":
            node = _closed(value, ("kind", "value"))
            return {
                "kind": "literal",
                "value": _validate_json_value(
                    node["value"],
                    depth=depth + 1,
                    counter=counter,
                    active=active,
                ),
            }
        if kind == "object":
            node = _closed(value, ("kind", "members"))
            members = node["members"]
            if not isinstance(members, Mapping):
                raise RecoveryDescriptorError("invalid recipe object members")
            result: dict[str, dict[str, object]] = {}
            for name, member in members.items():
                if not isinstance(name, str):
                    raise RecoveryDescriptorError("recipe member names must be strings")
                result[name] = _validate_recipe(
                    member,
                    child_ids=child_ids,
                    depth=depth + 1,
                    counter=counter,
                    active=active,
                )
            return {"kind": "object", "members": result}
        if kind == "list":
            node = _closed(value, ("kind", "items"))
            items = node["items"]
            if not isinstance(items, list):
                raise RecoveryDescriptorError("invalid recipe list items")
            return {
                "kind": "list",
                "items": [
                    _validate_recipe(
                        item,
                        child_ids=child_ids,
                        depth=depth + 1,
                        counter=counter,
                        active=active,
                    )
                    for item in items
                ],
            }
        if kind == "child-field":
            node = _closed(value, ("kind", "child_id", "field"))
            child_id = _custody_identifier(node["child_id"])
            field = node["field"]
            if child_id not in child_ids:
                raise RecoveryDescriptorError("recipe references an unknown child")
            if not isinstance(field, str) or not field:
                raise RecoveryDescriptorError("recipe child field is invalid")
            return {"kind": "child-field", "child_id": child_id, "field": field}
        raise RecoveryDescriptorError("unknown recipe node kind")
    finally:
        active.remove(identity)


def validate_descriptor(value: object) -> dict[str, object]:
    """Validate and copy one closed private recovery descriptor."""

    descriptor = _closed(value, _DESCRIPTOR_FIELDS)
    if descriptor["version"] != VERSION:
        raise RecoveryDescriptorError("unknown recovery descriptor version")
    children, child_ids = _validate_children(descriptor["required_children"])
    result = {
        "version": VERSION,
        "scoped_idempotency_digest": _digest(descriptor["scoped_idempotency_digest"]),
        "command_digest": _digest(descriptor["command_digest"]),
        "attempt_id": _attempt_token(descriptor["attempt_id"]),
        "commit_token": _attempt_token(descriptor["commit_token"]),
        "command": _custody_identifier(descriptor["command"]),
        "selector_digest": _digest(descriptor["selector_digest"]),
        "cell_id": _custody_identifier(descriptor["cell_id"]),
        "logical_vault_id": _custody_identifier(descriptor["logical_vault_id"]),
        "registry_attachment_id": _custody_identifier(descriptor["registry_attachment_id"]),
        "attachment_epoch": _positive_sqlite_integer(descriptor["attachment_epoch"]),
        "activation_store_id": _custody_identifier(descriptor["activation_store_id"]),
        "required_children": children,
        "result_recipe": _validate_recipe(descriptor["result_recipe"], child_ids=child_ids),
    }
    _canonical_json(result)
    return result


def prepare_descriptor(
    *,
    scoped_idempotency_digest: str,
    command_digest: str,
    attempt_id: str,
    commit_token: str,
    command: str,
    selector_digest: str,
    cell_id: str,
    logical_vault_id: str,
    registry_attachment_id: str,
    attachment_epoch: int,
    activation_store_id: str,
    required_children: list[dict[str, object]],
    result_recipe: dict[str, object],
) -> dict[str, object]:
    """Prepare validated data only; this does not commit or prove any effect."""

    return validate_descriptor(
        {
            "version": VERSION,
            "scoped_idempotency_digest": scoped_idempotency_digest,
            "command_digest": command_digest,
            "attempt_id": attempt_id,
            "commit_token": commit_token,
            "command": command,
            "selector_digest": selector_digest,
            "cell_id": cell_id,
            "logical_vault_id": logical_vault_id,
            "registry_attachment_id": registry_attachment_id,
            "attachment_epoch": attachment_epoch,
            "activation_store_id": activation_store_id,
            "required_children": required_children,
            "result_recipe": result_recipe,
        }
    )


def encode_descriptor(value: object) -> bytes:
    """Return canonical compact UTF-8 for a validated descriptor."""

    return _canonical_json(validate_descriptor(value))


def descriptor_digest(value: object) -> str:
    """Return the canonical SHA-256 digest of a validated descriptor."""

    return hashlib.sha256(encode_descriptor(value)).hexdigest()


def _attempt_secret(value: object) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise RecoveryDescriptorError("attempt secret must contain exactly 32 bytes")
    return value


def bind_descriptor(value: object, *, attempt_secret: bytes) -> str:
    """Authenticate the canonical descriptor digest under the private attempt secret."""

    secret = _attempt_secret(attempt_secret)
    digest = bytes.fromhex(descriptor_digest(value))
    return hmac.new(secret, _DESCRIPTOR_DOMAIN + digest, hashlib.sha256).hexdigest()


def verify_descriptor_binding(value: object, binding: object, *, attempt_secret: bytes) -> bool:
    """Constant-time verify one prepared descriptor binding."""

    expected = bind_descriptor(value, attempt_secret=attempt_secret)
    valid = isinstance(binding, str) and _DIGEST.fullmatch(binding) is not None
    candidate = binding if valid else "0" * 64
    matches = hmac.compare_digest(expected, candidate)
    return matches and valid


def _child(value: Mapping[str, object], child_id: object) -> Mapping[str, object]:
    identity = _custody_identifier(child_id)
    for child in value["required_children"]:
        if child["id"] == identity:
            return child
    raise RecoveryDescriptorError("unknown required child")


def _child_result_binding_payload(
    descriptor: object,
    *,
    child_id: object,
    result: object,
) -> bytes:
    validated = validate_descriptor(descriptor)
    child = _child(validated, child_id)
    _, payload = _prepared_child_payload(validated, child=child, result=result)
    return payload


def _prepared_child_payload(
    descriptor: Mapping[str, object],
    *,
    child: Mapping[str, object],
    result: object,
) -> tuple[dict[str, object], bytes]:
    if not isinstance(result, Mapping):
        raise RecoveryDescriptorError("prepared child result must be a JSON object")
    result_value = _validate_json_value(result, depth=1, counter=[0], active=set())
    if not isinstance(result_value, dict):
        raise RecoveryDescriptorError("prepared child result must be a JSON object")
    payload = {
        "descriptor_sha256": descriptor_digest(descriptor),
        "attempt_id": descriptor["attempt_id"],
        "child_id": child["id"],
        "plan_sha256": child["plan_sha256"],
        "result": result_value,
    }
    return result_value, _canonical_json(payload)


def validate_prepared_payloads(
    descriptor: object,
    results: object,
) -> dict[str, dict[str, object]]:
    """Validate the complete prepared child set and its aggregate private bound.

    This is preparation validation only. Exact canonical receipts must later
    establish commit before any caller can render a terminal result.
    """

    validated = validate_descriptor(descriptor)
    if not isinstance(results, Mapping):
        raise RecoveryDescriptorError("prepared child results must be an object")
    child_ids = [child["id"] for child in validated["required_children"]]
    if set(results) != set(child_ids) or any(not isinstance(key, str) for key in results):
        raise RecoveryDescriptorError("prepared child results do not match the manifest")
    total_bytes = len(encode_descriptor(validated))
    prepared: dict[str, dict[str, object]] = {}
    for child in validated["required_children"]:
        child_id = child["id"]
        result_value, payload = _prepared_child_payload(
            validated,
            child=child,
            result=results[child_id],
        )
        total_bytes += len(payload)
        if total_bytes > MAX_CANONICAL_BYTES:
            raise RecoveryDescriptorError("prepared recovery metadata exceeds its aggregate bound")
        prepared[child_id] = result_value
    return prepared


def bind_child_prepared_result(
    descriptor: object,
    *,
    child_id: str,
    result: Mapping[str, object],
    attempt_secret: bytes,
) -> str:
    """Authenticate private prepared result data; this is not committed proof."""

    secret = _attempt_secret(attempt_secret)
    payload = _child_result_binding_payload(descriptor, child_id=child_id, result=result)
    return hmac.new(secret, _CHILD_RESULT_DOMAIN + payload, hashlib.sha256).hexdigest()


def verify_child_prepared_result_binding(
    descriptor: object,
    *,
    child_id: str,
    result: Mapping[str, object],
    binding: object,
    attempt_secret: bytes,
) -> bool:
    """Constant-time verify authenticated preparation without asserting commit."""

    expected = bind_child_prepared_result(
        descriptor,
        child_id=child_id,
        result=result,
        attempt_secret=attempt_secret,
    )
    valid = isinstance(binding, str) and _DIGEST.fullmatch(binding) is not None
    candidate = binding if valid else "0" * 64
    matches = hmac.compare_digest(expected, candidate)
    return matches and valid


__all__ = [
    "MAX_CANONICAL_BYTES",
    "MAX_RECIPE_DEPTH",
    "MAX_RECIPE_NODES",
    "MAX_REQUIRED_CHILDREN",
    "RecoveryDescriptorError",
    "VERSION",
    "bind_child_prepared_result",
    "bind_descriptor",
    "descriptor_digest",
    "encode_descriptor",
    "prepare_descriptor",
    "validate_descriptor",
    "validate_prepared_payloads",
    "verify_child_prepared_result_binding",
    "verify_descriptor_binding",
]
