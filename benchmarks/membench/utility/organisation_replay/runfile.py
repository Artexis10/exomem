"""The run file of SCHEMA.md section 2: session objects, call records and a shape check.

A run file can show an arm's role: ``edge_review_reasons`` follows the bound arm's
reason capability. The blind judge sees only the five-field view, never ``run.json``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from membench.utility.switch_eval.arms import canonical_json

#: The tap's own refusals, which carry no product refusal code: its log outcome is the code.
_TAP_REFUSALS = ("refused_budget", "refused_filtered")
#: The product's compare-and-retry guards a write call sends and a refusal or a
#: preview offers back: documented argument names of the governed write doors.
RETRY_GUARD_FIELDS = ("draft_hash", "expected_hash")
#: The documented argument that turns a write door into a preview that writes nothing.
PREVIEW_FIELD = "validate_only"
#: The documented arguments that name a written page: a note's title, an entity's name.
TITLE_FIELDS = ("title", "name")
#: The documented answer fields for a written page: where a write landed, where a preview would.
PATH_FIELDS = ("path", "destination")


def structured_answer(structured: object, meta: object) -> object:
    """A tool's answer from its structured content, as the tool returned it.

    FastMCP sends the answer of a tool whose output schema is not an object as
    ``{"result": answer}`` and marks the call's ``_meta`` with
    ``fastmcp.wrap_result``. The marker is the framework's own protocol field,
    the one its client unwraps by, so no answer is unwrapped by its shape.
    """

    framework = meta.get("fastmcp") if isinstance(meta, dict) else None
    if isinstance(framework, dict) and framework.get("wrap_result") is True:
        return structured.get("result") if isinstance(structured, dict) else None
    return structured


def refusal(answer: object) -> str | None:
    """The structured refusal code of an answer, or ``None`` when it succeeded."""

    if not isinstance(answer, dict):
        return None
    error = answer.get("error")
    if answer.get("success") is False or isinstance(error, dict):
        code = error.get("code") if isinstance(error, dict) else None
        return str(code or "refused")
    return None


def _answer(result: object) -> object:
    if not isinstance(result, dict):
        return None
    if result.get("structuredContent") is not None:
        return structured_answer(result["structuredContent"], result.get("_meta"))
    text = "".join(
        item.get("text", "") for item in result.get("content") or [] if isinstance(item, dict)
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _result_text(result: object) -> str:
    """The text a client receives: the content text, else the structured content an empty one stands for."""

    if not isinstance(result, dict):
        return ""
    text = "".join(
        item.get("text", "") for item in result.get("content") or [] if isinstance(item, dict)
    )
    if not text and result.get("structuredContent") is not None:
        return canonical_json(result["structuredContent"])
    return text


def _layers(value: object) -> list[dict]:
    """A mapping and the ``operation`` or ``result`` mapping nested in it, where doors put fields."""

    if not isinstance(value, dict):
        return []
    nested = [value[name] for name in ("operation", "result") if isinstance(value.get(name), dict)]
    return [value, *nested]


def _first(layers: Sequence[dict], names: Sequence[str]) -> object:
    for layer in layers:
        for name in names:
            if layer.get(name) not in (None, ""):
                return layer[name]
    return None


def call_records(tap_events: Sequence[Mapping]) -> list[dict]:
    """One record per Exomem call the tap saw, in the tap log's order."""

    listing = next((e for e in tap_events if e.get("event") == "tools_list"), {})
    read_only = {
        tool.get("name"): bool((tool.get("annotations") or {}).get("readOnlyHint"))
        for tool in listing.get("tools") or []
    }
    records = []
    calls = [e for e in tap_events if e.get("event") == "call"]
    for seq, call in enumerate(calls, start=1):
        arguments = call.get("arguments") or {}
        result = call.get("result")
        answer = _answer(result)
        refusal_code = call["outcome"] if call.get("outcome") in _TAP_REFUSALS else refusal(answer)
        sent = _layers(arguments)
        preview = bool(_first(sent, (PREVIEW_FIELD,)))
        path = _first(_layers(answer), PATH_FIELDS)
        title = _first(sent, TITLE_FIELDS)
        records.append(
            {
                "turn": call.get("turn"),
                "seq": seq,
                "ordinal_in_turn": call.get("ordinal"),
                "tool": call.get("name"),
                "outcome": call.get("outcome"),
                "arguments": arguments,
                "request_bytes": len(canonical_json(arguments).encode("utf-8")),
                "result": result,
                "error": call.get("error"),
                "response_bytes": len(_result_text(result).encode("utf-8")),
                "is_error": bool(call.get("error")) or bool((result or {}).get("isError")),
                "refusal_code": refusal_code,
                "mutating": read_only.get(call.get("name")) is False and not preview,
                "target": {"path": path, "title": title} if path or title else None,
                "retry_hash": _first(sent, RETRY_GUARD_FIELDS)
                or (_first(_layers(answer), RETRY_GUARD_FIELDS) if refusal_code else None),
                "elapsed_ms": call.get("elapsed_ms"),
            }
        )
    return records


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def session_object(role: str, final: Mapping, session_dir: Path, run_dir: Path) -> dict:
    attempt_dir = session_dir / f"attempt-{final['attempts']}"
    transcript = attempt_dir / "transcript.jsonl"
    tap_log = attempt_dir / "runtime" / "tap.jsonl"
    init = next(
        (
            e
            for e in read_jsonl(transcript)
            if e.get("type") == "system" and e.get("subtype") == "init"
        ),
        {},
    )
    tap_events = read_jsonl(tap_log)
    turns = final.get("turns") or []
    return {
        "role": role,
        "session_id": init.get("session_id"),
        "attempt": final["attempts"],
        "outcome": final["outcome"],
        "kind": final["kind"],
        "init": (final.get("checks") or {}).get("init") or {},
        "checks": {k: v for k, v in (final.get("checks") or {}).items() if k != "init"},
        "transcript_path": transcript.relative_to(run_dir).as_posix(),
        "tap_log_path": tap_log.relative_to(run_dir).as_posix(),
        "calls": call_records(tap_events),
        "final_answer": turns[-1].get("answer") if turns else None,
        "turn_results": [
            {
                "index": turn["index"],
                "subtype": turn.get("result_subtype"),
                "is_error": turn.get("is_error"),
                "duration_seconds": turn.get("wall_seconds"),
                "usage": turn.get("usage"),
                "hook_responses": turn.get("hook_responses"),
            }
            for turn in turns
        ],
        "guard_refusals": sum(
            1
            for e in tap_events
            if e.get("event") == "call" and e.get("outcome") == "refused_budget"
        ),
    }


#: SCHEMA.md section 2's fields and their JSON types, for the shape check.
_RUN_FIELDS = {
    "situation_id": str,
    "situation_sha256": str,
    "arm": str,
    "client_shape": str,
    "binding_ref": str,
    "seed": dict,
    "sessions": list,
    "end_state": dict,
    "faults": list,
    "status": str,
}
_SEED_FIELDS = {
    "tree_digest": str,
    "projection_digest": str,
    "registry_overlay_digests": dict,
    "check": str,
    "settled_promotion_ids": list,
}
_SESSION_FIELDS = {
    "role": str,
    "session_id": str,
    "attempt": int,
    "init": dict,
    "transcript_path": str,
    "tap_log_path": str,
    "calls": list,
    "final_answer": str,
    "turn_results": list,
    "guard_refusals": int,
}
_CALL_FIELDS = {
    "turn": int,
    "seq": int,
    "tool": str,
    "arguments": dict,
    "request_bytes": int,
    "result": (dict, type(None)),
    "response_bytes": int,
    "is_error": bool,
    "refusal_code": (str, type(None)),
    "mutating": bool,
    "target": (dict, type(None)),
    "retry_hash": (str, type(None)),
}
_END_STATE_FIELDS = {
    "pages": list,
    "pages_changed": list,
    "edges": list,
    "entity_types": dict,
    "relations": dict,
    "registry_history": list,
    "homes": dict,
    "operation_log": dict,
    "promotions": list,
}
#: SCHEMA.md's closed values for the run file's own fields.
_STATUSES = ("ok", "struck", "actor_failure")
#: The readback's own closed values for whether an arm's log carries edge-review reasons.
_REASON_STATUSES = ("available", "unavailable")
_FAULT_KINDS = ("harness_fault", "actor_failure", "quota", "abort")


def _fields(where: str, value: object, fields: Mapping[str, object]) -> list[str]:
    if not isinstance(value, dict):
        return [f"{where} is not an object"]
    return [
        f"{where}.{name}: expected {getattr(kind, '__name__', kind)}, got {type(value.get(name)).__name__}"
        for name, kind in fields.items()
        if name not in value
        or not isinstance(value[name], kind)
        or (kind is int and isinstance(value[name], bool))
    ]


def check_run(run: Mapping, run_dir: Path) -> list[str]:
    """Each way a run file departs from SCHEMA.md section 2; empty when it conforms."""

    problems = _fields("run", run, _RUN_FIELDS)
    if problems:
        return problems
    if run["status"] not in _STATUSES:
        problems.append(f"run.status {run['status']!r}")
    problems += _fields("seed", run["seed"], _SEED_FIELDS)
    for index, fault in enumerate(run["faults"]):
        if not isinstance(fault, dict) or fault.get("kind") not in _FAULT_KINDS:
            problems.append(f"faults[{index}] {fault!r}")
    if run["status"] == "struck":
        return problems
    if run["seed"].get("check") != "pass":
        problems.append("a run with sessions has a failed seed check")
    if [s.get("role") for s in run["sessions"]] != ["capture", "fresh"]:
        problems.append(f"sessions are {[s.get('role') for s in run['sessions']]}")
    for session in run["sessions"]:
        where = f"sessions[{session.get('role')}]"
        problems += _fields(where, session, _SESSION_FIELDS)
        for name in ("transcript_path", "tap_log_path"):
            if isinstance(session.get(name), str) and not (run_dir / session[name]).is_file():
                problems.append(f"{where}.{name} does not exist")
        for call in session.get("calls") or []:
            problems += _fields(f"{where}.calls[{call.get('seq')}]", call, _CALL_FIELDS)
    problems += _fields("end_state", run["end_state"], _END_STATE_FIELDS)
    log = run["end_state"].get("operation_log")
    reasons = log.get("edge_review_reasons") if isinstance(log, dict) else None
    if not isinstance(reasons, dict) or reasons.get("status") not in _REASON_STATUSES:
        # A scorer that finds no availability record would read absent reasons as zero.
        problems.append(f"end_state.operation_log.edge_review_reasons {reasons!r}")
    return problems
