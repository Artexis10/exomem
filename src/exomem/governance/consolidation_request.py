"""Closed, pure request validation for governed vault consolidation."""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import NoReturn, cast

REQUEST_SCHEMA_NAME = "exomem.consolidate-memory-request/v1"
ACTIONS = (
    "start",
    "status",
    "reconcile",
    "plan",
    "approve",
    "apply",
    "verify",
    "recover",
    "abort",
    "rollback",
    "retire-source",
)

_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_PAGE_ORDINAL = (1 << 31) - 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


class ConsolidationRequestUnavailable(RuntimeError):
    """Content-free refusal for an invalid consolidation request."""

    code = "CONSOLIDATION_REQUEST_INVALID"

    def __init__(self) -> None:
        super().__init__("consolidation request is unavailable")


@dataclass(frozen=True, slots=True)
class _Object:
    required: tuple[tuple[str, object], ...]
    optional: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class _OneOf:
    choices: tuple[_Object, ...]


@dataclass(frozen=True, slots=True)
class _Variant:
    action: str
    required: tuple[tuple[str, object], ...]
    optional: tuple[tuple[str, object], ...] = ()


def _fields(**fields: object) -> tuple[tuple[str, object], ...]:
    return tuple(fields.items())


_DIGEST_FIELDS = {
    "expected_reconciliation_digest": "digest",
    "expected_policy_bundle_digest": "digest",
    "expected_principal_attestation_set_digest": "digest",
    "expected_verification_plan_digest": "digest",
    "expected_rollback_contingency_digest": "digest",
    "expected_source_retention_digest": "digest",
    "expected_control_basis_digest": "digest",
}
_CUTOVER_OPTIONS = _Object(
    _fields(**_DIGEST_FIELDS),
    _fields(expected_rehearsal_proof_digest="digest"),
)
_ROLLBACK_OPTIONS = _Object(
    _fields(
        target_kind=("enum", ("pre-cutover", "post-cutover-forward")),
        target_snapshot_ref="ref",
        target_snapshot_digest="digest",
        expected_current_census_digest="digest",
        treatment_set_ref="ref",
        treatment_set_digest="digest",
        surviving_copy_ledger_digest="digest",
        expected_retirement_state_digest="digest",
    )
)
_RETIREMENT_OPTION_FIELDS = _fields(
    archive_artifact_ref="ref",
    archive_artifact_digest="digest",
    archive_terms_digest="digest",
    source_retention_proof_ref="ref",
    source_retention_proof_digest="digest",
    surviving_copy_ledger_digest="digest",
    retained_irrecoverable_statement_digest="digest",
)


def _retirement_options(disposition: str, rollback_mode: str) -> _Object:
    conditional: tuple[tuple[str, object], ...] = ()
    if disposition == "transfer":
        conditional += _fields(custodian_receipt_ref="ref", custodian_receipt_digest="digest")
    if rollback_mode == "forward-only":
        conditional += _fields(forward_snapshot_ref="ref", forward_snapshot_digest="digest")
    return _Object(
        _RETIREMENT_OPTION_FIELDS
        + _fields(
            archive_disposition=("const", disposition),
            post_retirement_rollback_mode=("const", rollback_mode),
        )
        + conditional
    )


_RETIREMENT_OPTIONS = _OneOf(
    tuple(
        _retirement_options(disposition, rollback_mode)
        for disposition in ("retain", "transfer", "external-destruction")
        for rollback_mode in ("pre-cutover-reversible", "forward-only")
    )
)

_BASE = _fields(schema=("const", REQUEST_SCHEMA_NAME))
_MUTATION = _fields(operation_id="uuid", run_id="uuid", expected_run_revision="revision")
_CONTEXT = _fields(successor_context_ref="ref", successor_context_digest="digest")


def _join(*parts: tuple[tuple[str, object], ...]) -> tuple[tuple[str, object], ...]:
    return tuple(item for part in parts for item in part)


_VARIANTS = (
    _Variant(
        "start",
        _join(
            _BASE,
            _fields(
                action=("const", "start"),
                operation_id="uuid",
                expected_run_revision=("const", 0),
                run_mode=("const", "cloned-rehearsal"),
                source_artifact_ref="ref",
                source_attestation_ref="ref",
            ),
        ),
        _fields(clone_binding_ref="ref"),
    ),
    _Variant(
        "start",
        _join(
            _BASE,
            _fields(
                action=("const", "start"),
                operation_id="uuid",
                expected_run_revision=("const", 0),
                run_mode=("const", "real-cutover"),
                source_artifact_ref="ref",
                source_attestation_ref="ref",
            ),
        ),
    ),
    _Variant(
        "status",
        _join(_BASE, _fields(action=("const", "status"), run_id="uuid")),
        _fields(
            expected_run_revision="revision",
            detail=("enum", ("summary", "owner-detail")),
            cursor="ref",
            limit="limit",
        ),
    ),
    _Variant(
        "reconcile",
        _join(
            _BASE,
            _MUTATION,
            _fields(
                expected_inventory_digest="digest",
                decision_set_ref="ref",
                decision_set_digest="digest",
            ),
            _fields(action=("const", "reconcile")),
        ),
    ),
    _Variant(
        "plan",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "plan"),
                plan_kind=("const", "cutover"),
                operation=("const", "materialize"),
                cutover_options=_CUTOVER_OPTIONS,
            ),
        ),
    ),
    _Variant(
        "plan",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "plan"),
                plan_kind=("const", "rollback"),
                operation=("const", "materialize"),
                rollback_options=_ROLLBACK_OPTIONS,
            ),
        ),
    ),
    _Variant(
        "plan",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "plan"),
                plan_kind=("const", "retirement"),
                operation=("const", "materialize"),
                retirement_options=_RETIREMENT_OPTIONS,
            ),
        ),
    ),
    _Variant(
        "plan",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "plan"),
                plan_kind=("enum", ("cutover", "rollback", "retirement")),
                operation=("const", "render"),
                render_step=("const", "begin"),
                plan_digest="digest",
            ),
        ),
    ),
    _Variant(
        "plan",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "plan"),
                plan_kind=("enum", ("cutover", "rollback", "retirement")),
                operation=("const", "render"),
                render_step=("const", "page"),
                plan_digest="digest",
                render_session_ref="ref",
                page_ordinal="page",
            ),
        ),
    ),
    _Variant(
        "plan",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "plan"),
                plan_kind=("enum", ("cutover", "rollback", "retirement")),
                operation=("const", "render"),
                render_step=("const", "acknowledge"),
                plan_digest="digest",
                render_session_ref="ref",
                page_ordinal="page",
                acknowledged_page_digest="digest",
            ),
        ),
    ),
    _Variant(
        "plan",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "plan"),
                plan_kind=("enum", ("cutover", "rollback", "retirement")),
                operation=("const", "render"),
                render_step=("const", "complete"),
                plan_digest="digest",
                render_session_ref="ref",
            ),
        ),
    ),
    _Variant(
        "approve",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "approve"),
                plan_kind=("enum", ("cutover", "rollback", "retirement")),
                plan_digest="digest",
                rendering_completeness_ref="ref",
                rendering_completeness_digest="digest",
            ),
        ),
    ),
    _Variant(
        "apply",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "apply"),
                cutover_plan_digest="digest",
                approval_token_ref="ref",
                approval_token_digest="digest",
            ),
        ),
    ),
    _Variant(
        "verify",
        _join(
            _BASE,
            _MUTATION,
            _fields(
                action=("const", "verify"),
                verification_kind=("enum", ("in-process", "transport")),
                expected_plan_digest="digest",
                expected_verification_basis_digest="digest",
            ),
        ),
    ),
    _Variant(
        "recover",
        _join(
            _BASE,
            _MUTATION,
            _fields(action=("const", "recover"), expected_journal_digest="digest"),
        ),
        _fields(expected_intent_event_id="digest"),
    ),
    _Variant(
        "abort",
        _join(
            _BASE,
            _MUTATION,
            _fields(
                action=("const", "abort"),
                expected_journal_digest="digest",
                reason_code=(
                    "enum",
                    (
                        "owner-cancelled",
                        "preimage-unavailable",
                        "verification-failed",
                        "maintenance-window-expired",
                    ),
                ),
            ),
        ),
        _fields(reason="reason"),
    ),
    _Variant(
        "rollback",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "rollback"), rollback_mode=("const", "nonterminal-contingency")
            ),
        ),
    ),
    _Variant(
        "rollback",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "rollback"),
                rollback_mode=("const", "terminal-plan"),
                rollback_plan_digest="digest",
                rollback_token_ref="ref",
                rollback_token_digest="digest",
            ),
        ),
    ),
    _Variant(
        "retire-source",
        _join(
            _BASE,
            _MUTATION,
            _CONTEXT,
            _fields(
                action=("const", "retire-source"),
                phase=("const", "clearance"),
                retirement_plan_digest="digest",
                retirement_token_ref="ref",
                retirement_token_digest="digest",
            ),
        ),
    ),
    _Variant(
        "retire-source",
        _join(
            _BASE,
            _MUTATION,
            _fields(
                action=("const", "retire-source"),
                phase=("const", "finalize"),
                retirement_plan_digest="digest",
                retirement_lifecycle_ref="ref",
                completion_attestation_ref="ref",
                completion_attestation_digest="digest",
            ),
        ),
    ),
)


def _fail() -> NoReturn:
    raise ConsolidationRequestUnavailable from None


def _text(value: object, *, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str) or len(value) < minimum or "\x00" in value:
        _fail()
    if unicodedata.normalize("NFC", value) != value:
        _fail()
    try:
        if len(value.encode("utf-8")) > maximum:
            _fail()
    except UnicodeEncodeError:
        _fail()
    return value


def _rule_schema(rule: object) -> dict[str, object]:
    if isinstance(rule, _Object):
        return _object_schema(rule)
    if isinstance(rule, _OneOf):
        return {"oneOf": [_object_schema(choice) for choice in rule.choices]}
    if rule == "uuid":
        return {"$ref": "#/$defs/uuid4"}
    if rule == "digest":
        return {"$ref": "#/$defs/digest"}
    if rule == "ref":
        return {"$ref": "#/$defs/ref"}
    if rule == "revision":
        return {"$ref": "#/$defs/revision"}
    if rule == "page":
        return {"$ref": "#/$defs/page_ordinal"}
    if rule == "limit":
        return {"$ref": "#/$defs/limit"}
    if rule == "reason":
        return {"$ref": "#/$defs/reason"}
    kind, argument = cast(tuple[str, object], rule)
    if kind == "const":
        schema: dict[str, object] = {"const": argument}
        if isinstance(argument, int):
            schema["type"] = "integer"
        return schema
    if kind == "enum":
        return {"type": "string", "enum": list(cast(tuple[str, ...], argument))}
    raise AssertionError(rule)


def _object_schema(spec: _Object | _Variant) -> dict[str, object]:
    required = dict(spec.required)
    optional = dict(spec.optional)
    properties = required.copy()
    properties.update(optional)
    return {
        "type": "object",
        "properties": {name: _rule_schema(rule) for name, rule in properties.items()},
        "required": list(required),
        "additionalProperties": False,
    }


def _build_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": REQUEST_SCHEMA_NAME,
        "oneOf": [_object_schema(variant) for variant in _VARIANTS],
        "$defs": {
            "uuid4": {
                "type": "string",
                "pattern": (
                    "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                    "[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\\s\\S])"
                ),
            },
            "digest": {"type": "string", "pattern": "^[0-9a-f]{64}(?![\\s\\S])"},
            "ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "not": {"pattern": "\\u0000"},
            },
            "revision": {"type": "integer", "minimum": 0, "maximum": _MAX_SAFE_INTEGER},
            "page_ordinal": {"type": "integer", "minimum": 0, "maximum": _MAX_PAGE_ORDINAL},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "reason": {
                "type": "string",
                "minLength": 0,
                "maxLength": 500,
                "not": {"pattern": "\\u0000"},
            },
        },
    }


REQUEST_SCHEMA = _build_schema()


def request_schema() -> dict[str, object]:
    """Return a fresh JSON Schema for the closed request union."""

    return copy.deepcopy(REQUEST_SCHEMA)


def _validate_rule(value: object, rule: object) -> object:
    if isinstance(rule, _Object):
        return _validate_object(value, rule)
    if isinstance(rule, _OneOf):
        for choice in rule.choices:
            try:
                return _validate_object(value, choice)
            except ConsolidationRequestUnavailable:
                continue
        _fail()
    if rule == "uuid":
        if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
            _fail()
        return value
    if rule == "digest":
        if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
            _fail()
        return value
    if rule == "ref":
        return _text(value, maximum=512)
    if rule == "reason":
        return _text(value, maximum=500, minimum=0)
    if rule == "revision":
        maximum = _MAX_SAFE_INTEGER
    elif rule == "page":
        maximum = _MAX_PAGE_ORDINAL
    elif rule == "limit":
        maximum = 200
    else:
        kind, argument = cast(tuple[str, object], rule)
        if kind == "const":
            if value != argument or (isinstance(argument, int) and type(value) is not int):
                _fail()
            return value
        if kind == "enum":
            if not isinstance(value, str) or value not in cast(tuple[str, ...], argument):
                _fail()
            return value
        raise AssertionError(rule)
    minimum = 1 if rule == "limit" else 0
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail()
    return value


def _validate_object(value: object, spec: _Object | _Variant) -> dict[str, object]:
    if type(value) is not dict:
        _fail()
    required = dict(spec.required)
    optional = dict(spec.optional)
    if not set(required) <= set(value) <= set(required) | set(optional):
        _fail()
    return {
        name: _validate_rule(item, required[name] if name in required else optional[name])
        for name, item in value.items()
    }


def _validate_cutover_condition(request: dict[str, object], trusted_run_mode: str | None) -> None:
    if (
        request.get("action") != "plan"
        or request.get("operation") != "materialize"
        or request.get("plan_kind") != "cutover"
    ):
        return
    options = request["cutover_options"]
    assert type(options) is dict
    has_proof = "expected_rehearsal_proof_digest" in options
    if trusted_run_mode is None:
        _fail()
    if trusted_run_mode == "real-cutover" and not has_proof:
        _fail()
    if trusted_run_mode == "cloned-rehearsal" and has_proof:
        _fail()


def decode_request_json(raw: bytes) -> dict[str, object]:
    """Decode UTF-8 JSON without discarding duplicate fields or numeric types."""

    def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
    except (UnicodeError, ValueError, RecursionError):
        _fail()
    if type(value) is not dict:
        _fail()
    return value


def _validate_request_fields(value: object) -> dict[str, object]:
    """Validate fields before a trusted adapter can use a run identifier.

    This is not admission: the adapter must still call ``validate_request``
    with the authenticated stored run mode before dispatch.
    """
    if (
        type(value) is not dict
        or type(value.get("action")) is not str
        or value["action"] not in ACTIONS
    ):
        _fail()
    for variant in _VARIANTS:
        if variant.action != value["action"]:
            continue
        try:
            return _validate_object(value, variant)
        except ConsolidationRequestUnavailable:
            continue
    _fail()


def validate_request(value: object, *, trusted_run_mode: str | None = None) -> dict[str, object]:
    """Validate decoded input without coercion or granting authority.

    Cutover materialization requires the authenticated stored run mode; absent
    state refuses admission instead of skipping the rehearsal-proof condition.
    """
    if trusted_run_mode not in {None, "cloned-rehearsal", "real-cutover"}:
        _fail()
    request = _validate_request_fields(value)
    _validate_cutover_condition(request, trusted_run_mode)
    return request


__all__ = [
    "ACTIONS",
    "ConsolidationRequestUnavailable",
    "REQUEST_SCHEMA",
    "request_schema",
    "decode_request_json",
    "validate_request",
]
