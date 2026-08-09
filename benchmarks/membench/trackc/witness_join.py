"""Two-witness activation join: server call-trace × client transcript.

Activation is only credited when BOTH witnesses agree; a one-sided story is a
``WITNESS_MISMATCH`` — a HARNESS fault that invalidates the case, never a
product score (mirrors the invalid-run semantics of the main runner).

Witness 1 — server call-trace JSONL. The exomem repo has no single canonical
"MCP tool call" trace file (``logs/queries.jsonl`` records ``find()`` calls,
``logs/writes.jsonl`` records writes; neither is a complete tool-call ledger),
so this module defines the documented MINIMAL WITNESS SCHEMA the driver's
wrapper emits, one JSON object per line::

    {"ts": "<iso8601 or epoch>", "tool": "<tool name>", "args_digest": "<sha256 prefix>"}

``tool`` is required; ``ts``/``args_digest`` are optional (``args_digest`` is
derived from an ``args`` object when present). Lines with an unknown shape
(undecodable JSON, non-object payloads, missing/empty ``tool``) are counted in
:attr:`ServerTrace.malformed_lines`, never silently dropped — a damaged
witness on EITHER side (malformed trace lines, malformed transcript lines, or
an error-subtype result) makes the whole case a ``WITNESS_MISMATCH`` harness
fault before any activation verdict is considered.

Witness 2 — a ``claude -p --output-format stream-json`` transcript: assistant
``tool_use`` blocks carry ``name``/``input``; the terminal ``result`` line
carries the final text plus token/latency accounting.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

VERDICT_ACTIVATED = "activated"
VERDICT_NOT_ACTIVATED = "not_activated"
VERDICT_WITNESS_MISMATCH = "WITNESS_MISMATCH"

DEFAULT_SERVER_NAME = "exomem"


@dataclass(frozen=True)
class WitnessEvent:
    """One server-side tool-call witness line (minimal schema above)."""

    tool: str
    ts: str | None = None
    args_digest: str | None = None


@dataclass(frozen=True)
class ServerTrace:
    """Parsed server witness: events plus the malformed-line count.

    Mirrors :attr:`Transcript.malformed_lines`: damage is measured, never
    hidden, so a schema-drifted or crash-truncated trace can never masquerade
    as an honestly quiet one.
    """

    events: tuple[WitnessEvent, ...] = ()
    malformed_lines: int = 0


@dataclass(frozen=True)
class ToolUse:
    name: str
    input: dict


@dataclass(frozen=True)
class Transcript:
    """Parsed view of one stream-json transcript."""

    tool_uses: tuple[ToolUse, ...] = ()
    result_text: str = ""
    duration_ms: float | None = None
    usage: dict = field(default_factory=dict)
    total_cost_usd: float | None = None
    session_id: str | None = None
    num_turns: int | None = None
    is_error: bool = False
    malformed_lines: int = 0

    def memory_tool_uses(self, server_name: str = DEFAULT_SERVER_NAME) -> tuple[ToolUse, ...]:
        prefix = f"mcp__{server_name}__"
        return tuple(t for t in self.tool_uses if t.name.startswith(prefix))


@dataclass(frozen=True)
class WitnessVerdict:
    case_id: str
    client_claims: bool
    server_observed: bool
    verdict: str
    harness_fault: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "client_claims": self.client_claims,
            "server_observed": self.server_observed,
            "verdict": self.verdict,
            "harness_fault": self.harness_fault,
            "detail": self.detail,
        }


def _digest_args(args: object) -> str:
    encoded = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def parse_server_trace(source: Path | Iterable[str]) -> ServerTrace:
    """Parse a server call-trace JSONL file (or pre-split lines).

    Returns a :class:`ServerTrace`. Undecodable JSON, non-object payloads, and
    objects without a non-empty ``tool`` string are COUNTED as malformed —
    never silently dropped (blank lines are skipped, not malformed).
    """

    lines = (
        Path(source).read_text(encoding="utf-8").splitlines()
        if isinstance(source, (str, Path))
        else list(source)
    )
    events: list[WitnessEvent] = []
    malformed = 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(payload, dict):
            malformed += 1
            continue
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool:
            malformed += 1
            continue
        digest = payload.get("args_digest")
        if not isinstance(digest, str):
            digest = _digest_args(payload["args"]) if "args" in payload else None
        ts = payload.get("ts")
        events.append(
            WitnessEvent(
                tool=tool,
                ts=str(ts) if ts is not None else None,
                args_digest=digest,
            )
        )
    return ServerTrace(events=tuple(events), malformed_lines=malformed)


def parse_stream_json_transcript(source: Path | Iterable[str]) -> Transcript:
    """Parse ``claude -p --output-format stream-json`` output lines."""

    lines = (
        Path(source).read_text(encoding="utf-8").splitlines()
        if isinstance(source, (str, Path))
        else list(source)
    )
    tool_uses: list[ToolUse] = []
    result_text = ""
    duration_ms: float | None = None
    usage: dict = {}
    total_cost: float | None = None
    session_id: str | None = None
    num_turns: int | None = None
    is_error = False
    malformed = 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(payload, dict):
            malformed += 1
            continue
        kind = payload.get("type")
        if kind == "assistant":
            message = payload.get("message") or {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name")
                    if isinstance(name, str):
                        tool_input = block.get("input")
                        tool_uses.append(
                            ToolUse(
                                name=name,
                                input=tool_input if isinstance(tool_input, dict) else {},
                            )
                        )
        elif kind == "result":
            result = payload.get("result")
            if isinstance(result, str):
                result_text = result
            raw_duration = payload.get("duration_ms")
            if isinstance(raw_duration, (int, float)):
                duration_ms = float(raw_duration)
            if isinstance(payload.get("usage"), dict):
                usage = payload["usage"]
            raw_cost = payload.get("total_cost_usd")
            if isinstance(raw_cost, (int, float)):
                total_cost = float(raw_cost)
            raw_session = payload.get("session_id")
            if isinstance(raw_session, str):
                session_id = raw_session
            raw_turns = payload.get("num_turns")
            if isinstance(raw_turns, int):
                num_turns = raw_turns
            if payload.get("subtype") not in (None, "success"):
                is_error = True
            if payload.get("is_error") is True:
                is_error = True
    return Transcript(
        tool_uses=tuple(tool_uses),
        result_text=result_text,
        duration_ms=duration_ms,
        usage=usage,
        total_cost_usd=total_cost,
        session_id=session_id,
        num_turns=num_turns,
        is_error=is_error,
        malformed_lines=malformed,
    )


def join_case(
    case_id: str,
    *,
    transcript: Transcript,
    server_events: ServerTrace | Sequence[WitnessEvent],
    server_name: str = DEFAULT_SERVER_NAME,
) -> WitnessVerdict:
    """Join both witnesses for one prompt case.

    Witness INTEGRITY is checked before any activation verdict: malformed
    server-trace lines, malformed transcript lines, or an error-subtype
    result on either side make the case a ``WITNESS_MISMATCH`` harness fault
    — two damaged witnesses must never both-quiet their way into a
    ``not_activated`` product score.
    """

    trace = (
        server_events
        if isinstance(server_events, ServerTrace)
        else ServerTrace(events=tuple(server_events))
    )
    claimed = transcript.memory_tool_uses(server_name)
    client_claims = bool(claimed)
    server_observed = bool(trace.events)

    damage: list[str] = []
    if trace.malformed_lines:
        damage.append(f"server trace has {trace.malformed_lines} malformed line(s)")
    if transcript.malformed_lines:
        damage.append(f"transcript has {transcript.malformed_lines} malformed line(s)")
    if transcript.is_error:
        damage.append("transcript reports an error result")
    if damage:
        return WitnessVerdict(
            case_id=case_id,
            client_claims=client_claims,
            server_observed=server_observed,
            verdict=VERDICT_WITNESS_MISMATCH,
            harness_fault=True,
            detail=(
                "damaged witness: " + "; ".join(damage)
                + " — harness fault, never a product score"
            ),
        )

    if client_claims and server_observed:
        verdict, fault = VERDICT_ACTIVATED, False
        detail = (
            f"client used {sorted({t.name for t in claimed})}; "
            f"server observed {sorted({e.tool for e in trace.events})}"
        )
    elif not client_claims and not server_observed:
        verdict, fault = VERDICT_NOT_ACTIVATED, False
        detail = "both witnesses quiet"
    elif client_claims:
        verdict, fault = VERDICT_WITNESS_MISMATCH, True
        detail = (
            "transcript claims memory tool use but the server trace is empty — "
            "harness fault (trace capture broken?), never a product score"
        )
    else:
        verdict, fault = VERDICT_WITNESS_MISMATCH, True
        detail = (
            "server observed tool calls the transcript never claimed — "
            "harness fault (transcript capture broken?), never a product score"
        )
    return WitnessVerdict(
        case_id=case_id,
        client_claims=client_claims,
        server_observed=server_observed,
        verdict=verdict,
        harness_fault=fault,
        detail=detail,
    )


def join_cases(
    cases: dict[str, tuple[Transcript, ServerTrace | Sequence[WitnessEvent]]],
    *,
    server_name: str = DEFAULT_SERVER_NAME,
) -> list[WitnessVerdict]:
    return [
        join_case(case_id, transcript=transcript, server_events=events, server_name=server_name)
        for case_id, (transcript, events) in sorted(cases.items())
    ]
