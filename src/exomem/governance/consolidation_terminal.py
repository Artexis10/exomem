"""Closed logical terminals for governed vault consolidation.

This module is a pure serializer and grants no authority.  The command
coordinator must supply its complete validated request, exact resulting state,
and any already-resolved successor projection.  Authenticated owner, run, and
context resolution remain an integration dependency of that coordinator.
"""

from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import NoReturn, cast

from . import consolidation_plan, consolidation_request, consolidation_successor

TERMINAL_SCHEMA_NAME = "exomem.consolidation-terminal/v1"

_MAX_SAFE_INTEGER = (1 << 53) - 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_PLAN_KINDS = ("cutover", "rollback", "retirement")
_STATUS_PLAN_CONTEXTS = frozenset(
    {"render-begin", "render-page", "render-acknowledge", "render-complete"}
)
_PAIR = ("successor_context_ref", "successor_context_digest")
_ROWS = {
    "start": (
        ("source_fingerprint", "destination_snapshot_fingerprint"),
        ("source_objects", "source_bytes", "destination_objects", "destination_bytes"),
        ("status", "reconcile"),
    ),
    "status": (
        ("run_state_digest", "journal_digest"),
        ("completed_effects", "pending_effects", "blocked_effects", "warnings"),
        (
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
        ),
    ),
    "reconcile": (
        ("inventory_digest", "reconciliation_digest", "mapping_set_digest"),
        ("c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8", "unresolved", "mappings"),
        ("status", "reconcile", "plan"),
    ),
    "plan:materialize": (
        ("plan_digest", "control_basis_digest", "plan_successor_automaton_digest"),
        ("content_actions", "policy_documents", "impact_rows", "render_pages"),
        ("status", "plan"),
    ),
    "plan:render-begin": (
        ("plan_digest", "render_session_digest"),
        ("render_pages", "acknowledged_pages"),
        ("status", "plan"),
    ),
    "plan:render-page": (
        ("plan_digest", "render_page_digest"),
        ("page_ordinal", "page_rows", "render_pages"),
        ("status", "plan"),
    ),
    "plan:render-acknowledge": (
        ("plan_digest", "render_ack_digest"),
        ("acknowledged_pages", "render_pages"),
        ("status", "plan"),
    ),
    "plan:render-complete": (
        ("plan_digest", "rendering_completeness_digest"),
        ("acknowledged_pages", "render_pages"),
        ("status", "approve"),
    ),
    "approve": (
        ("plan_digest", "approval_token_digest"),
        ("acknowledged_pages", "render_pages"),
        ("status", "apply", "rollback", "retire-source"),
    ),
    "apply": (
        ("cutover_terminal_digest", "post_cutover_census_digest", "apply_predecessor_digest"),
        (
            "policy_documents",
            "content_batches",
            "content_actions",
            "rebuild_kinds",
            "in_process_probes",
            "transport_probes",
        ),
        ("status", "plan", "apply", "verify", "recover", "abort", "rollback", "retire-source"),
    ),
    "verify": (
        ("verification_basis_digest", "verification_terminal_digest"),
        ("positive_probes", "negative_probes", "passed_probes", "failed_probes"),
        ("status", "plan", "apply", "verify", "recover", "rollback", "retire-source"),
    ),
    "recover": (
        ("journal_digest", "recovery_terminal_digest"),
        ("classified_effects", "repaired_effects", "blocked_effects"),
        ("status", "plan", "apply", "verify", "recover", "abort", "rollback", "retire-source"),
    ),
    "abort": (
        ("prior_census_digest", "abort_terminal_digest"),
        ("restored_entries", "removed_candidates", "evidence_events"),
        ("status",),
    ),
    "rollback:nonterminal-contingency": (
        (
            "cutover_plan_digest",
            "original_apply_journal_digest",
            "rollback_contingency_digest",
            "target_census_digest",
            "rollback_terminal_digest",
        ),
        (
            "restored_entries",
            "retained_entries",
            "reapplied_entries",
            "discarded_entries",
            "rebuild_kinds",
            "verification_probes",
        ),
        ("status", "plan", "recover", "verify", "rollback"),
    ),
    "rollback:terminal-plan": (
        ("rollback_plan_digest", "target_census_digest", "rollback_terminal_digest"),
        (
            "restored_entries",
            "retained_entries",
            "reapplied_entries",
            "discarded_entries",
            "rebuild_kinds",
            "verification_probes",
        ),
        ("status", "plan", "recover", "verify", "rollback"),
    ),
    "retire-source:clearance": (
        (
            "retirement_plan_digest",
            "clearance_digest",
            "retirement_lifecycle_digest",
            "surviving_copy_ledger_digest",
        ),
        ("survivor_rows", "verified_survivor_rows"),
        ("status", "retire-source"),
    ),
    "retire-source:finalize": (
        (
            "retirement_plan_digest",
            "retirement_lifecycle_digest",
            "completion_digest",
            "finalization_digest",
            "surviving_copy_ledger_digest",
        ),
        ("completion_records", "survivor_rows"),
        ("status", "plan"),
    ),
}


class ConsolidationTerminalUnavailable(RuntimeError):
    """Content-free refusal for an invalid consolidation terminal."""

    code = "CONSOLIDATION_TERMINAL_INVALID"

    def __init__(self) -> None:
        super().__init__("consolidation terminal is unavailable")


@dataclass(frozen=True, slots=True)
class TerminalProjectionState:
    """Exact state projected by the process-trusted command coordinator."""

    validated_request: Mapping[str, object]
    terminal: Mapping[str, object]
    trusted_run_mode: str | None = None
    successor_reference: str | None = None
    successor_context: consolidation_successor.CanonicalSuccessorContext | None = None
    expected_successor: consolidation_successor.ExpectedSuccessorContext | None = None
    eligible_plan_kinds: tuple[str, ...] | None = None
    nonterminal_contingency_eligible: bool = False


class _ValidatedTerminal(Mapping[str, object]):
    __slots__ = ("_value",)
    _value: Mapping[str, object]

    def __init__(self, value: Mapping[str, object], *, seal: object) -> None:
        if seal is not _TERMINAL_SEAL:
            _fail()
        object.__setattr__(self, "_value", _freeze(value))

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        _fail()

    def __getitem__(self, key: str) -> object:
        return self._value[key]

    def __iter__(self):
        return iter(self._value)

    def __len__(self) -> int:
        return len(self._value)


_TERMINAL_SEAL = object()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _fail() -> NoReturn:
    raise ConsolidationTerminalUnavailable from None


def _text(value: object, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail()
    if unicodedata.normalize("NFC", value) != value:
        _fail()
    try:
        if len(value.encode("utf-8")) > maximum:
            _fail()
    except UnicodeEncodeError:
        _fail()
    return value


def _digest(value: object) -> str:
    text = _text(value, maximum=64)
    if _DIGEST.fullmatch(text) is None:
        _fail()
    return text


def _uuid(value: object) -> str:
    text = _text(value, maximum=36)
    if _UUID4.fullmatch(text) is None:
        _fail()
    return text


def _integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
        _fail()
    return value


def _object(value: object, fields: Sequence[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        _fail()
    return value


def _next_actions(value: object, allowed: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail()
    actions = tuple(_text(item, maximum=64) for item in value)
    if len(actions) != len(set(actions)):
        _fail()
    positions = [allowed.index(item) if item in allowed else -1 for item in actions]
    if -1 in positions or positions != sorted(positions):
        _fail()
    return actions


def _output_forms(action: str) -> tuple[tuple[str, ...], ...]:
    if action in {"start", "abort"}:
        return ((),)
    if action == "status":
        return (
            (),
            ("next_cursor",),
            _PAIR,
            _PAIR + ("eligible_plan_kinds",),
            ("next_cursor",) + _PAIR,
            ("next_cursor",) + _PAIR + ("eligible_plan_kinds",),
        )
    if action == "reconcile":
        return ((), _PAIR + ("eligible_plan_kinds",))
    if action == "plan:materialize":
        return (_PAIR,)
    if action == "plan:render-begin":
        return (_PAIR + ("render_session_ref",),)
    if action == "plan:render-page":
        return (_PAIR + ("render_session_ref", "render_delivery_ref"),)
    if action == "plan:render-acknowledge":
        return (_PAIR + ("render_session_ref",),)
    if action == "plan:render-complete":
        return (_PAIR + ("rendering_completeness_ref",),)
    if action == "approve":
        return (_PAIR + ("approval_token_ref",),)
    if action == "retire-source:clearance":
        return (("retirement_clearance_ref", "retirement_lifecycle_ref"),)
    if action in {"apply", "verify", "recover"}:
        return ((), _PAIR, _PAIR + ("eligible_plan_kinds",))
    return ((), _PAIR + ("eligible_plan_kinds",))


def _validate_outputs(action: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail()
    try:
        fields = frozenset(value)
    except TypeError:
        _fail()
    if not all(isinstance(field, str) for field in fields) or fields not in {
        frozenset(form) for form in _output_forms(action)
    }:
        _fail()
    for name, item in value.items():
        if name.endswith("_digest"):
            _digest(item)
        elif name.endswith("_ref") or name == "next_cursor":
            _text(item)
        elif name == "eligible_plan_kinds":
            if isinstance(item, (str, bytes)) or not isinstance(item, Sequence):
                _fail()
            kinds = tuple(_text(kind, maximum=16) for kind in item)
            if not kinds or kinds != tuple(kind for kind in _PLAN_KINDS if kind in kinds):
                _fail()
        else:
            _fail()
    return value


def _output_schema(fields: tuple[str, ...]) -> dict[str, object]:
    properties: dict[str, object] = {}
    for field in fields:
        if field.endswith("_digest"):
            properties[field] = {"$ref": "#/$defs/digest"}
        elif field.endswith("_ref") or field == "next_cursor":
            properties[field] = {"$ref": "#/$defs/ref"}
        else:
            properties[field] = {
                "type": "array",
                "items": {"enum": list(_PLAN_KINDS)},
                "uniqueItems": True,
            }
    return {
        "type": "object",
        "properties": properties,
        "required": list(fields),
        "additionalProperties": False,
    }


def _branch_schema(action: str) -> dict[str, object]:
    digest_fields, count_fields, next_actions = _ROWS[action]
    required = [
        "schema",
        "action",
        "outcome",
        "run_id",
        "run_revision",
        "phase",
        "artifact_digests",
        "counts",
        "next_actions",
        "trusted_outputs",
    ]
    properties: dict[str, object] = {
        "schema": {"const": TERMINAL_SCHEMA_NAME},
        "action": {"const": action},
        "outcome": {"const": "observed" if action == "status" else "committed"},
        "run_id": {"$ref": "#/$defs/uuid4"},
        "run_revision": {"$ref": "#/$defs/integer"},
        "phase": {"$ref": "#/$defs/text"},
        "artifact_digests": {
            "type": "object",
            "properties": {name: {"$ref": "#/$defs/digest"} for name in digest_fields},
            "required": list(digest_fields),
            "additionalProperties": False,
        },
        "counts": {
            "type": "object",
            "properties": {name: {"$ref": "#/$defs/integer"} for name in count_fields},
            "required": list(count_fields),
            "additionalProperties": False,
        },
        "next_actions": {
            "type": "array",
            "items": {"enum": list(next_actions)},
            "uniqueItems": True,
        },
        "trusted_outputs": {"oneOf": [_output_schema(form) for form in _output_forms(action)]},
    }
    if action != "status":
        mutation = ("operation_id", "request_digest", "prior_state_digest", "final_state_digest")
        required.extend(mutation)
        properties.update(
            {
                name: {"$ref": "#/$defs/uuid4" if name == "operation_id" else "#/$defs/digest"}
                for name in mutation
            }
        )
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TERMINAL_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": TERMINAL_SCHEMA_NAME,
    "oneOf": [_branch_schema(action) for action in _ROWS],
    "$defs": {
        "uuid4": {
            "type": "string",
            "pattern": (
                "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                "[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\\s\\S])"
            ),
        },
        "digest": {"type": "string", "pattern": "^[0-9a-f]{64}(?![\\s\\S])"},
        "ref": {"type": "string", "minLength": 1, "maxLength": 512, "not": {"pattern": "\\u0000"}},
        "integer": {"type": "integer", "minimum": 0, "maximum": _MAX_SAFE_INTEGER},
        "text": {"type": "string", "minLength": 1, "maxLength": 512, "not": {"pattern": "\\u0000"}},
    },
}


def terminal_schema() -> dict[str, object]:
    """Return a fresh JSON Schema for the terminal tagged union."""

    return copy.deepcopy(TERMINAL_SCHEMA)


def _validate_structure(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail()
    action = value.get("action")
    if not isinstance(action, str) or action not in _ROWS:
        _fail()
    digest_fields, count_fields, allowed_actions = _ROWS[action]
    common = {
        "schema",
        "action",
        "outcome",
        "run_id",
        "run_revision",
        "phase",
        "artifact_digests",
        "counts",
        "next_actions",
        "trusted_outputs",
    }
    mutation = {"operation_id", "request_digest", "prior_state_digest", "final_state_digest"}
    if set(value) != (common if action == "status" else common | mutation):
        _fail()
    if value["schema"] != TERMINAL_SCHEMA_NAME or value["outcome"] != (
        "observed" if action == "status" else "committed"
    ):
        _fail()
    _uuid(value["run_id"])
    _integer(value["run_revision"])
    _text(value["phase"])
    for item in _object(value["artifact_digests"], digest_fields).values():
        _digest(item)
    for item in _object(value["counts"], count_fields).values():
        _integer(item)
    _next_actions(value["next_actions"], allowed_actions)
    _validate_outputs(action, value["trusted_outputs"])
    if action != "status":
        _uuid(value["operation_id"])
        for name in ("request_digest", "prior_state_digest", "final_state_digest"):
            _digest(value[name])
    return copy.deepcopy(dict(value))


def _request_action(value: Mapping[str, object]) -> str:
    action = value.get("action")
    if action == "plan":
        if value.get("operation") == "materialize":
            return "plan:materialize"
        step = value.get("render_step")
        if value.get("operation") == "render" and step in {
            "begin",
            "page",
            "acknowledge",
            "complete",
        }:
            return f"plan:render-{step}"
    if action == "rollback" and value.get("rollback_mode") in {
        "nonterminal-contingency",
        "terminal-plan",
    }:
        return f"rollback:{value['rollback_mode']}"
    if action == "retire-source" and value.get("phase") in {"clearance", "finalize"}:
        return f"retire-source:{value['phase']}"
    if action in _ROWS:
        return action
    _fail()


def _request_digest(value: Mapping[str, object]) -> str:
    try:
        return hashlib.sha256(consolidation_plan.canonical_closed_jcs(value)).hexdigest()
    except (consolidation_plan.ConsolidationPlanUnavailable, RecursionError):
        _fail()


def _eligible_plan_kinds(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail()
    kinds = tuple(_text(kind, maximum=16) for kind in value)
    if not kinds or kinds != tuple(kind for kind in _PLAN_KINDS if kind in kinds):
        _fail()
    return kinds


def _require_facts(
    context: consolidation_successor.CanonicalSuccessorContext,
    kind: str,
    expected: Mapping[str, object],
) -> None:
    if context.preimage["context_kind"] != kind:
        _fail()
    facts = context.preimage["facts"]
    if not isinstance(facts, Mapping):
        _fail()
    for name, item in expected.items():
        if facts.get(name) != item:
            _fail()


def _validate_plan_entry(
    state: TerminalProjectionState,
    request: Mapping[str, object],
    terminal: Mapping[str, object],
    context: consolidation_successor.CanonicalSuccessorContext,
) -> None:
    outputs = terminal["trusted_outputs"]
    assert isinstance(outputs, Mapping)
    facts = context.preimage["facts"]
    if not isinstance(facts, Mapping):
        _fail()
    projected = _eligible_plan_kinds(state.eligible_plan_kinds)
    terminal_kinds = _eligible_plan_kinds(outputs.get("eligible_plan_kinds"))
    context_kinds = _eligible_plan_kinds(facts.get("eligible_plan_kinds"))
    if projected is None or projected != terminal_kinds or projected != context_kinds:
        _fail()
    next_actions = cast(Sequence[str], terminal["next_actions"])
    if "plan" not in next_actions or state.nonterminal_contingency_eligible:
        _fail()

    action = terminal["action"]
    phase = terminal["phase"]
    if action == "status":
        allowed = (
            (projected == ("cutover",) and phase in {"reconcile", "repair-terminal"})
            or (
                projected == ("rollback",)
                and phase
                in {
                    "complete",
                    "rollback-complete",
                    "retirement-pending-forward-only",
                    "retirement-finalize",
                    "repair-terminal",
                }
            )
            or (
                projected == ("rollback", "retirement") and phase in {"complete", "repair-terminal"}
            )
        )
    elif action == "reconcile":
        counts = terminal["counts"]
        assert isinstance(counts, Mapping)
        allowed = phase == "reconcile" and counts["unresolved"] == 0 and projected == ("cutover",)
    elif action == "apply":
        allowed = phase == "complete" and projected in {
            ("rollback",),
            ("rollback", "retirement"),
        }
    elif action == "verify":
        allowed = (
            phase == "complete"
            and request.get("verification_kind") == "transport"
            and projected in {("rollback",), ("rollback", "retirement")}
        )
    elif action == "recover":
        allowed = phase == "repair-terminal" and projected in {
            ("cutover",),
            ("rollback",),
            ("rollback", "retirement"),
        }
    elif action in {"rollback:nonterminal-contingency", "rollback:terminal-plan"}:
        allowed = phase == "rollback-complete" and projected == ("rollback",)
    elif action == "retire-source:finalize":
        allowed = phase == "retirement-finalize" and projected == ("rollback",)
    else:
        allowed = False
    if not allowed or ("retirement" in projected and state.trusted_run_mode != "real-cutover"):
        _fail()


def _validate_successor_branch(
    state: TerminalProjectionState,
    request: Mapping[str, object],
    terminal: Mapping[str, object],
    context: consolidation_successor.CanonicalSuccessorContext,
) -> None:
    if (
        context.preimage["run_id"] != terminal["run_id"]
        or context.preimage["run_revision"] != terminal["run_revision"]
    ):
        _fail()
    action = str(terminal["action"])
    outputs = terminal["trusted_outputs"]
    artifacts = terminal["artifact_digests"]
    counts = terminal["counts"]
    next_actions = cast(Sequence[str], terminal["next_actions"])
    assert isinstance(outputs, Mapping)
    assert isinstance(artifacts, Mapping)
    assert isinstance(counts, Mapping)
    kind = context.preimage["context_kind"]

    if action.startswith("plan:render-") or action == "approve":
        if request["plan_digest"] != artifacts["plan_digest"]:
            _fail()
    if state.nonterminal_contingency_eligible != (kind == "rollback-nonterminal-contingency"):
        _fail()

    if kind == "plan-materialize":
        _validate_plan_entry(state, request, terminal, context)
        return
    if state.eligible_plan_kinds is not None or "eligible_plan_kinds" in outputs:
        _fail()

    if action == "status":
        if "plan" in next_actions and kind not in _STATUS_PLAN_CONTEXTS:
            _fail()
        return
    if action == "plan:materialize":
        if tuple(next_actions) != ("status", "plan"):
            _fail()
        _require_facts(
            context,
            "render-begin",
            {"plan_kind": request["plan_kind"], "plan_digest": artifacts["plan_digest"]},
        )
    elif action == "plan:render-begin":
        if tuple(next_actions) != ("status", "plan"):
            _fail()
        _require_facts(
            context,
            "render-page",
            {
                "plan_kind": request["plan_kind"],
                "plan_digest": artifacts["plan_digest"],
                "render_session_digest": artifacts["render_session_digest"],
                "page_ordinal": 0,
            },
        )
    elif action == "plan:render-page":
        if tuple(next_actions) != ("status", "plan"):
            _fail()
        if (
            outputs["render_session_ref"] != request["render_session_ref"]
            or counts["page_ordinal"] != request["page_ordinal"]
        ):
            _fail()
        _require_facts(
            context,
            "render-acknowledge",
            {
                "plan_kind": request["plan_kind"],
                "plan_digest": artifacts["plan_digest"],
                "page_ordinal": request["page_ordinal"],
                "page_digest": artifacts["render_page_digest"],
            },
        )
    elif action == "plan:render-acknowledge":
        if tuple(next_actions) != ("status", "plan"):
            _fail()
        if (
            outputs["render_session_ref"] != request["render_session_ref"]
            or counts["acknowledged_pages"] != cast(int, request["page_ordinal"]) + 1
            or counts["acknowledged_pages"] > counts["render_pages"]
        ):
            _fail()
        expected_kind = (
            "render-complete"
            if counts["acknowledged_pages"] == counts["render_pages"]
            else "render-page"
        )
        expected_facts = {
            "plan_kind": request["plan_kind"],
            "plan_digest": artifacts["plan_digest"],
        }
        if expected_kind == "render-page":
            expected_facts["page_ordinal"] = counts["acknowledged_pages"]
        _require_facts(context, expected_kind, expected_facts)
    elif action == "plan:render-complete":
        if tuple(next_actions) != ("status", "approve"):
            _fail()
        if counts["render_pages"] == 0 or counts["acknowledged_pages"] != counts["render_pages"]:
            _fail()
        _require_facts(
            context,
            "approve",
            {
                "plan_kind": request["plan_kind"],
                "plan_digest": artifacts["plan_digest"],
                "rendering_completeness_digest": artifacts["rendering_completeness_digest"],
            },
        )
    elif action == "approve":
        kinds = {
            "cutover": ("apply", "cutover_plan_digest", "approval_token_digest", "apply"),
            "rollback": (
                "rollback-terminal-plan",
                "rollback_plan_digest",
                "rollback_token_digest",
                "rollback",
            ),
            "retirement": (
                "retire-source-clearance",
                "retirement_plan_digest",
                "retirement_token_digest",
                "retire-source",
            ),
        }
        expected_kind, plan_field, token_field, next_action = kinds[str(request["plan_kind"])]
        _require_facts(
            context,
            expected_kind,
            {plan_field: request["plan_digest"], token_field: artifacts["approval_token_digest"]},
        )
        if tuple(next_actions) != ("status", next_action):
            _fail()
    elif action in {"apply", "verify", "recover"} and kind == "rollback-nonterminal-contingency":
        if (
            terminal["phase"] == "complete"
            or not state.nonterminal_contingency_eligible
            or "plan" in next_actions
        ):
            _fail()
        if action == "apply":
            _require_facts(
                context,
                kind,
                {
                    "original_apply_operation_id": request["operation_id"],
                    "cutover_plan_digest": request["cutover_plan_digest"],
                },
            )
    else:
        _fail()


def _validate_projection_binding(
    state: TerminalProjectionState,
    request: Mapping[str, object],
    terminal: Mapping[str, object],
) -> None:
    if terminal["action"] == "status":
        expected_revision = request.get("expected_run_revision")
        if expected_revision is not None and expected_revision != terminal["run_revision"]:
            _fail()
    else:
        if (
            request["operation_id"] != terminal["operation_id"]
            or cast(int, request["expected_run_revision"]) + 1 != terminal["run_revision"]
            or _request_digest(request) != terminal["request_digest"]
        ):
            _fail()


def _projection(value: object) -> dict[str, object]:
    if not isinstance(value, TerminalProjectionState):
        _fail()
    if type(value.nonterminal_contingency_eligible) is not bool:
        _fail()
    try:
        request = consolidation_request.validate_request(
            dict(value.validated_request), trusted_run_mode=value.trusted_run_mode
        )
    except (consolidation_request.ConsolidationRequestUnavailable, TypeError, ValueError):
        _fail()
    terminal = _validate_structure(value.terminal)
    if terminal["action"] != _request_action(request):
        _fail()
    if request.get("action") != "start" and request.get("run_id") != terminal["run_id"]:
        _fail()
    _validate_projection_binding(value, request, terminal)
    pair = {"successor_context_ref", "successor_context_digest"}
    outputs = cast(Mapping[str, object], terminal["trusted_outputs"])
    next_actions = cast(Sequence[str], terminal["next_actions"])
    has_pair = pair <= set(outputs)
    context_values = (value.successor_reference, value.successor_context, value.expected_successor)
    if has_pair:
        if any(item is None for item in context_values):
            _fail()
        try:
            context = consolidation_successor.verify_context(
                value.successor_context, expected=value.expected_successor
            )
        except consolidation_successor.SuccessorContextUnavailable:
            _fail()
        if (
            outputs["successor_context_ref"] != _text(value.successor_reference)
            or outputs["successor_context_digest"] != context.digest
        ):
            _fail()
        if terminal["action"] == "status" and request.get("detail", "summary") != "owner-detail":
            _fail()
        if context.preimage["successor_action"] not in next_actions:
            _fail()
        _validate_successor_branch(value, request, terminal, context)
    elif (
        any(item is not None for item in context_values)
        or value.eligible_plan_kinds is not None
        or value.nonterminal_contingency_eligible
    ):
        _fail()
    elif (
        terminal["action"]
        in {
            "status",
            "reconcile",
            "apply",
            "verify",
            "recover",
            "rollback:nonterminal-contingency",
            "rollback:terminal-plan",
            "retire-source:finalize",
        }
        and "plan" in next_actions
    ):
        _fail()
    return terminal


def validate_terminal(
    value: object,
    *,
    request: TerminalProjectionState,
    successor_context: object | None = None,
    plan_entry: object | None = None,
) -> Mapping[str, object]:
    """Validate an exact terminal against the coordinator's current resolved state."""

    if successor_context is not None or plan_entry is not None:
        _fail()
    terminal = _validate_structure(value)
    if terminal != _projection(request):
        _fail()
    return _ValidatedTerminal(terminal, seal=_TERMINAL_SEAL)


def success_envelope(
    terminal: Mapping[str, object], delivery: str = "initial"
) -> dict[str, object]:
    """Return a detached JSON value around a sealed validated terminal."""

    if type(terminal) is not _ValidatedTerminal:
        _fail()
    checked = terminal
    if delivery not in {"initial", "replayed"}:
        _fail()
    if checked["action"] == "status" and delivery != "initial":
        _fail()
    thawed = _thaw(checked)
    if not isinstance(thawed, dict):
        _fail()
    return {"success": True, "data": {"delivery": delivery, "terminal": thawed}}


__all__ = [
    "TERMINAL_SCHEMA",
    "TERMINAL_SCHEMA_NAME",
    "ConsolidationTerminalUnavailable",
    "TerminalProjectionState",
    "success_envelope",
    "terminal_schema",
    "validate_terminal",
]
