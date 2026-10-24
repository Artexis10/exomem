"""Scoring one session: task success from the end state, and the secondary readouts.

Primary: a write task succeeds when the vault's collections end exactly as the
case's key says (rows matched on the collection's natural key, every asserted
field equal, nothing else changed); a read task succeeds when its JSON answer
equals the answer computed from the seeded facts and nothing was written. Both
are exact checks on structured values. Nothing here reads the session's prose.

Secondary: the first call that resolved to a key-accepted operation, calls per
task, argument refusals, schema bytes, input tokens, and writes during read
tasks. Calls reach those readouts through the injected resolver (see
``resolve``), so the same code scores every tool shape.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from membench.utility.switch_eval.tap import tool_payloads

from .cases import Case
from .resolve import Resolver, resolve_call
from .templates import READ, WRITE, CollectionKey, Key, ManifestKey

#: Refusal codes of the Records and Planning argument envelope: the product's
#: closed set for a call whose arguments the selected action does not accept.
ARGUMENT_REFUSALS = frozenset({"INVALID_RECORD_ARGUMENTS", "INVALID_PLAN_ARGUMENTS"})

#: Outcomes whose end state is scorable: the session ran and the agent acted
#: (or failed to). A harness fault is retried and a struck session is not run.
SCORABLE_OUTCOMES = frozenset({"ok", "actor_failure"})


# --------------------------------------------------------------------------
# Answers.
# --------------------------------------------------------------------------


def parse_answer(text: str | None) -> Any:
    """The ``answer`` member of the last JSON object in a reply, or ``None``.

    The prompt asks for only that object, and models still add a sentence or a
    code fence around it. The JSON parser finds the object, so the wrapper
    never decides the score; only the parsed value does.
    """

    if not text:
        return None
    decoder = json.JSONDecoder()
    found: Any = None
    position = text.find("{")
    while position != -1:
        try:
            value, end = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            position = text.find("{", position + 1)
            continue
        if isinstance(value, dict) and "answer" in value:
            found = value["answer"]
        position = text.find("{", end)
    return found


def answer_matches(answer: Any, key: Key) -> bool:
    expected = key.answer
    if key.answer_mode == "number":
        return (
            isinstance(answer, (int, float))
            and not isinstance(answer, bool)
            and abs(float(answer) - float(expected)) < 1e-9
        )
    if key.answer_mode == "set":
        return (
            isinstance(answer, list)
            and all(isinstance(entry, str) for entry in answer)
            and len(answer) == len(expected)
            and {entry.strip() for entry in answer} == set(expected)
        )
    if key.answer_mode == "text":
        return isinstance(answer, str) and answer.strip().casefold() == str(expected).strip().casefold()
    raise ValueError(f"unknown answer mode: {key.answer_mode!r}")


# --------------------------------------------------------------------------
# End state.
# --------------------------------------------------------------------------


def _same(actual: Any, expected: Any) -> bool:
    numeric = (int, float)
    if (
        isinstance(actual, numeric)
        and isinstance(expected, numeric)
        and not isinstance(actual, bool)
        and not isinstance(expected, bool)
    ):
        return abs(float(actual) - float(expected)) < 1e-9
    return str(actual) == str(expected)


def _identity(row: Mapping[str, Any], natural_key: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(row.get(name)) for name in natural_key)


def check_collection(key: CollectionKey, state: Mapping) -> list[str]:
    collection = state["collections"].get(key.manifest)
    if collection is None:
        return [f"collection {key.manifest} is missing"]
    actual: dict[tuple[str, ...], list[Mapping]] = {}
    for row in collection["rows"]:
        actual.setdefault(_identity(row, key.natural_key), []).append(row)
    reasons: list[str] = []
    expected_ids = set()
    for expected in key.rows:
        identity = _identity(expected, key.natural_key)
        expected_ids.add(identity)
        found = actual.get(identity, [])
        if len(found) != 1:
            reasons.append(f"{len(found)} rows for {identity}, expected 1")
            continue
        reasons.extend(
            f"{identity}.{name}: {found[0].get(name)!r} != {value!r}"
            for name, value in expected.items()
            if not _same(found[0].get(name), value)
        )
    reasons.extend(f"unexpected row {identity}" for identity in actual if identity not in expected_ids)
    return reasons


def check_manifest(key: ManifestKey, state: Mapping) -> list[str]:
    collection = state["collections"].get(key.manifest)
    if collection is None:
        return [f"collection {key.manifest} is missing"]
    reasons = [
        f"{name}: {collection[name]!r} != {expected!r}"
        for name, expected in (
            ("profile", key.profile),
            ("title", key.title),
            ("natural_key", list(key.natural_key)),
        )
        if collection[name] != expected
    ]
    for name, types in key.fields.items():
        declared = collection["fields"].get(name)
        if declared is None:
            reasons.append(f"field {name} is not declared")
        elif declared["type"] not in types:
            reasons.append(f"field {name} has type {declared['type']!r}, not one of {list(types)}")
        elif name in key.required and not declared["required"]:
            reasons.append(f"field {name} is not required")
    return reasons


def check_end_state(key: Key, before: Mapping, after: Mapping) -> list[str]:
    """Why the end state differs from the key; an empty list means it matches.

    A collection the key does not name must end as it began, so a write to the
    wrong collection fails the task.
    """

    named = {collection.manifest for collection in key.collections}
    named |= {manifest.manifest for manifest in key.manifests}
    reasons: list[str] = []
    for collection in key.collections:
        reasons.extend(check_collection(collection, after))
    for manifest in key.manifests:
        reasons.extend(check_manifest(manifest, after))
    for path in sorted((set(before["collections"]) | set(after["collections"])) - named):
        if before["collections"].get(path) != after["collections"].get(path):
            reasons.append(f"collection {path} changed but the task did not name it")
    return reasons


def state_changed(before: Mapping, after: Mapping) -> bool:
    return before["collections"] != after["collections"]


# --------------------------------------------------------------------------
# Calls.
# --------------------------------------------------------------------------


def compact_calls(tap_events: Sequence[Mapping]) -> list[dict]:
    """Each call the session made, as the tap saw it, without the result payloads.

    ``errored`` is the tool's own error flag; ``codes`` are the structured error
    codes in the result envelope (read as JSON, never from prose).
    """

    calls = []
    for event in tap_events:
        if event.get("event") != "call":
            continue
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        envelope = {"result": event.get("result"), "error": event.get("error")}
        codes = sorted(
            {
                str(value[name])
                for value in tool_payloads(envelope)
                if isinstance(value, dict)
                for name in ("code", "error_code")
                if isinstance(value.get(name), str)
            }
        )
        calls.append(
            {
                "ordinal": event.get("ordinal"),
                "name": event.get("name"),
                "arguments": event.get("arguments"),
                "outcome": event.get("outcome"),
                "errored": bool(event.get("error") or result.get("isError")),
                "codes": codes,
            }
        )
    return calls


@dataclass
class CaseResult:
    case_id: str
    template: str
    kind: str
    arm: str
    repeat: int
    outcome: str
    scored: bool
    success: bool = False
    reasons: list[str] = field(default_factory=list)
    #: Read tasks only: the vault changed, or a write-capable call was served.
    wrong_write: bool = False
    state_changed: bool = False
    write_calls_served: int = 0
    calls: int = 0
    unresolved_calls: int = 0
    argument_refusals: int = 0
    #: 1-based ordinal of the first call that resolved to an operation the key accepts.
    first_correct_ordinal: int | None = None
    schema_bytes: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    wall_seconds: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def score_session(
    case: Case, session: Mapping, calls: Sequence[Mapping], resolver: Resolver
) -> CaseResult:
    """Score one finished session from its record, its calls and the injected resolver.

    ``session`` is the session record the run loop writes: the outcome, the
    answer, the turn records, and the facts with the end states.
    """

    key = case.key
    outcome = session["outcome"]
    result = CaseResult(
        case.case_id, case.template_id, case.kind, session["arm"], session["repeat"], outcome,
        scored=outcome in SCORABLE_OUTCOMES,
    )
    facts = session.get("facts") or {}
    turn = (session.get("turns") or [{}])[0]
    result.schema_bytes = (facts.get("surface") or {}).get("schema_bytes")
    result.input_tokens = turn.get("input_tokens")
    result.output_tokens = turn.get("output_tokens")
    result.wall_seconds = turn.get("wall_seconds")
    result.calls = len(calls)
    targets = {(op.command, op.action, op.mode) for op in key.target_ops}
    for call in calls:
        resolved = resolve_call(resolver, str(call["name"]), call.get("arguments"))
        served = call["outcome"] == "served" and not call["errored"]
        if not resolved.resolved:
            result.unresolved_calls += 1
        if resolved.resolved and result.first_correct_ordinal is None and (
            (resolved.command, resolved.action, resolved.mode) in targets
        ):
            result.first_correct_ordinal = int(call["ordinal"])
        # A call the product refused with an error code wrote nothing; only the
        # state check can count a write from a call that carried no code.
        if served and not call["codes"] and resolved.resolved and resolved.read_only is False:
            result.write_calls_served += 1
        if ARGUMENT_REFUSALS.intersection(call["codes"]):
            result.argument_refusals += 1
    if not result.scored:
        result.reasons.append(f"session outcome {outcome}")
        return result

    before, after = facts["end_state_before"], facts["end_state_after"]
    result.state_changed = state_changed(before, after)
    if key.kind == READ:
        result.wrong_write = result.state_changed or result.write_calls_served > 0
        if not answer_matches(parse_answer(session.get("answer")), key):
            result.reasons.append("answer does not match the seeded facts")
        if result.wrong_write:
            result.reasons.append("a read task wrote")
    elif key.kind == WRITE:
        result.reasons.extend(check_end_state(key, before, after))
    result.success = not result.reasons
    return result
