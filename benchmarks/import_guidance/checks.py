"""Deterministic checks over the call log, and the verdict that joins them with the judge.

A check never reads the agent's words: those go to the judge. Each returns
`pass`, `fail` or `unknown`; a run error or an unreadable judge answer is
`unknown`, never `pass`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"

#: The scripted server's name, as the agent sees it in `mcp__<server>__<tool>`.
SERVER = "exomem"


@dataclass(frozen=True)
class Outcome:
    check: str
    verdict: str
    detail: str


@dataclass(frozen=True)
class Evidence:
    """What a run left behind. `calls` is the server's JSONL log; `tool_names` is every tool
    the agent tried, from the transcript; `shell_events` maps `start` and `exit` to epoch seconds."""

    calls: tuple[dict, ...]
    tool_names: tuple[str, ...]
    shell_events: dict[str, float]
    # The agent's final message, and the replies recorded from the arm's own code (see `recording.py`).
    answer: str = ""
    shapes: Mapping[str, Any] | None = None


def matches(actual: Any, wanted: Any) -> bool:
    """`wanted` is a subset of `actual`; lists compare as sets (path order carries no meaning)."""
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(
            k in actual and matches(actual[k], v) for k, v in wanted.items()
        )
    if isinstance(wanted, list):
        return isinstance(actual, list) and sorted(map(repr, actual)) == sorted(map(repr, wanted))
    return actual == wanted


def _calls(evidence: Evidence, tool: str, kind: str | None) -> list[dict]:
    return [
        c for c in evidence.calls if c["tool"] == tool and (kind is None or c.get("kind") == kind)
    ]


def never_called(spec: dict, evidence: Evidence, **_: Any) -> Outcome:
    hits = _calls(evidence, spec["tool"], spec.get("kind"))
    return Outcome(
        "never_called",
        FAIL if hits else PASS,
        f"{len(hits)} call(s) to {spec['tool']}.{spec.get('kind')}",
    )


def max_calls(spec: dict, evidence: Evidence, **_: Any) -> Outcome:
    hits = _calls(evidence, spec["tool"], spec.get("kind"))
    return Outcome(
        "max_calls",
        FAIL if len(hits) > spec["n"] else PASS,
        f"{len(hits)} call(s), limit {spec['n']}",
    )


def called_with(spec: dict, evidence: Evidence, **_: Any) -> Outcome:
    hits = [
        c
        for c in _calls(evidence, spec["tool"], spec.get("kind"))
        if matches(c["arguments"], spec["where"])
    ]
    wrong = len(_calls(evidence, spec["tool"], spec.get("kind"))) - len(hits)
    if not hits:
        return Outcome("called_with", FAIL, "no call with the required arguments")
    if spec.get("once") and wrong:
        return Outcome("called_with", FAIL, f"{wrong} further call(s) with other arguments")
    return Outcome("called_with", PASS, f"{len(hits)} matching call(s)")


def only_scripted_tools(
    spec: dict, evidence: Evidence, served: tuple[str, ...], builtin: tuple[str, ...], **_: Any
) -> Outcome:
    allowed = {f"mcp__{SERVER}__{name}" for name in served} | set(builtin)
    invented = sorted(set(evidence.tool_names) - allowed)
    return Outcome(
        "only_scripted_tools",
        FAIL if invented else PASS,
        f"invented or unlisted tools: {invented}" if invented else "none",
    )


def filed_before_shell_exit(spec: dict, evidence: Evidence, **_: Any) -> Outcome:
    """A landed window is filed (`import_memory` start or select) while the import command still runs."""
    filings = [
        c["t"]
        for c in evidence.calls
        if c["tool"] == "import_memory" and c.get("kind") in ("start", "select")
    ]
    if not filings:
        return Outcome("filed_before_shell_exit", FAIL, "no window was filed")
    exit_at = evidence.shell_events.get("exit")
    if exit_at is None:
        return Outcome("filed_before_shell_exit", PASS, "filed; the command had not exited")
    if min(filings) < exit_at:
        return Outcome(
            "filed_before_shell_exit",
            PASS,
            f"filed {exit_at - min(filings):.0f}s before the command exited",
        )
    return Outcome("filed_before_shell_exit", FAIL, "first filing came after the command exited")


#: Result fields that hold an id, a hash or a code; `references/mutation-results.md` documents them. A person
#: never needs their values, so a check can look for the exact strings that a scripted reply carried.
SCRIPTED_ID_KEYS = (
    "request_id",
    "receipt_id",
    "operation_id",
    "draft_id",
    "draft_hash",
    "relation_review_hash",
    "idempotency_key",
    "code",
    "error_code",
    "graph_sync_code",
    "derived_sync_code",
)
#: Shorter values are words ("ok"), not identifiers; a longer one is also looked for by its first characters.
MIN_ID_LENGTH = 8
PREFIX_FROM_LENGTH = 24


def values_under(node: Any, keys: tuple[str, ...]) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys and isinstance(value, str) and len(value) >= MIN_ID_LENGTH:
                yield value
            yield from values_under(value, keys)
    elif isinstance(node, list):
        for item in node:
            yield from values_under(item, keys)


def scripted_strings(shapes: Mapping[str, Any], spec: dict) -> list[str]:
    """The id, hash and code strings the scripted replies carried, and the `literals` the spec adds (field
    names that give away a sync state). `spec["shapes"]` limits the replies; without it every reply counts."""
    names = spec.get("shapes") or list(shapes)
    found: list[str] = []
    for name in names:
        for value in values_under(shapes[name], SCRIPTED_ID_KEYS):
            found.append(value)
            if len(value) >= PREFIX_FROM_LENGTH:
                found.append(value[:MIN_ID_LENGTH])
    found.extend(spec.get("literals", ()))
    return sorted(set(found))


def leaked_strings(text: str, shapes: Mapping[str, Any], spec: dict) -> list[str]:
    """The scripted strings that appear in `text`, ignoring case."""
    lowered = text.lower()
    return [s for s in scripted_strings(shapes, spec) if s.lower() in lowered]


def answer_lacks_scripted_strings(spec: dict, evidence: Evidence, **_: Any) -> Outcome:
    """The answer holds no id, hash, code or sync-state field name that a scripted reply carried."""
    if evidence.shapes is None or not scripted_strings(evidence.shapes, spec):
        return Outcome("answer_lacks_scripted_strings", UNKNOWN, "no scripted string to look for")
    leaked = leaked_strings(evidence.answer, evidence.shapes, spec)
    return Outcome(
        "answer_lacks_scripted_strings",
        FAIL if leaked else PASS,
        f"in the answer: {leaked}" if leaked else "none",
    )


def answer_has_scripted_string(spec: dict, evidence: Evidence, **_: Any) -> Outcome:
    """The answer holds the value of one field of one scripted reply (the request id the person asked for)."""
    reply = (evidence.shapes or {}).get(spec["shape"])
    values = list(values_under(reply, (spec["key"],)))
    if not values:
        return Outcome(
            "answer_has_scripted_string", UNKNOWN, f"{spec['shape']} has no {spec['key']}"
        )
    held = values[0] in evidence.answer
    return Outcome(
        "answer_has_scripted_string", PASS if held else FAIL, f"{spec['key']} in the answer: {held}"
    )


CHECKS = {
    f.__name__: f
    for f in (
        never_called,
        max_calls,
        called_with,
        only_scripted_tools,
        filed_before_shell_exit,
        answer_lacks_scripted_strings,
        answer_has_scripted_string,
    )
}


def run_checks(
    specs: tuple[dict, ...], evidence: Evidence, served: tuple[str, ...], builtin: tuple[str, ...]
) -> list[Outcome]:
    outcomes = []
    for spec in specs:
        check = CHECKS.get(spec["type"])
        if check is None:
            outcomes.append(Outcome(spec["type"], UNKNOWN, "no such check"))
        else:
            outcomes.append(check(spec, evidence, served=served, builtin=builtin))
    return outcomes


def judged_outcomes(criteria: tuple[dict, ...], answer: dict | None) -> list[Outcome]:
    """One outcome per criterion; a missing or non-boolean answer is `unknown`."""
    outcomes = []
    for criterion in criteria:
        held = None if answer is None else answer.get(criterion["id"])
        if not isinstance(held, bool):
            outcomes.append(Outcome(criterion["id"], UNKNOWN, "judge gave no boolean"))
            continue
        reason = str(answer.get(f"{criterion['id']}_reason", ""))
        outcomes.append(
            Outcome(criterion["id"], PASS if held == criterion["expect"] else FAIL, reason)
        )
    return outcomes


def verdict(run_error: str | None, deterministic: list[Outcome], judged: list[Outcome]) -> str:
    """A run error is unknown whatever the partial log says: a cut-off run proves no absence.
    Otherwise a proven failure wins, any gap is unknown, and pass needs every check to pass."""
    if run_error is not None:
        return UNKNOWN
    outcomes = [*deterministic, *judged]
    if any(o.verdict == FAIL for o in outcomes):
        return FAIL
    return PASS if outcomes and all(o.verdict == PASS for o in outcomes) else UNKNOWN
