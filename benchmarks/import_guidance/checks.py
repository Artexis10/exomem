"""Deterministic checks over the call log, and the verdict that joins them with the judge.

A check never reads the agent's words: those go to the judge. Each returns
`pass`, `fail` or `unknown`; a run error or an unreadable judge answer is
`unknown`, never `pass`.
"""

from __future__ import annotations

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


def _matches(actual: Any, wanted: Any) -> bool:
    """`wanted` is a subset of `actual`; lists compare as sets (path order carries no meaning)."""
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(
            k in actual and _matches(actual[k], v) for k, v in wanted.items()
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
        if _matches(c["arguments"], spec["where"])
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


CHECKS = {
    f.__name__: f
    for f in (never_called, max_calls, called_with, only_scripted_tools, filed_before_shell_exit)
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
