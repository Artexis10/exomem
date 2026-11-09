"""The error result an agent reads when a tool call is refused.

`cli_ops.public_refusal` builds one public error block for every facade: a stable code, a message,
remediation prose, and whatever operational details the refusing layer attached. The CLI and REST
keep that block. An MCP agent needs less: what happened to its data, and the exact call that can
succeed. This module turns the public block into that, once, for every tool.

Compact result: `{"error": {"code", "message", "retry"?, "candidates"?}}`.
`message` says what was refused. When there is no `retry`, it carries the first sentence of the
remediation next, because that sentence is the only fix the agent gets. A refused write ends it
with what happened to the data.
`retry.arguments` holds only the arguments to add or change; the rest of the call is unchanged.
`retry` is absent when no retry can succeed. With `response_detail="full"` the old envelope
(umbrella code, remediation prose, ids, `committed`, waits) rides beside it under `diagnostics`.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from typing import Any

MAX_CANDIDATES = 5

#: The code the semantic contract uses when it blocks a write; the finding it blocked on is the
#: specific code. Both names belong to the contract, not to a vault.
_UMBRELLA_CODE = "SEMANTIC_CONTRACT_BLOCKED"
_BUSY_CODE = "MUTATION_BUSY"

_NOTHING_SAVED = "Nothing was saved."
_SAVED = "The change was saved."
_SAVE_UNKNOWN = "Whether it was saved is not known."

#: The two findings the contract raises for a page without a link or a current decision to stand alone.
#: The same reviewed-none call resolves both, so both name it as the retry.
_RELATION_DECISION_MESSAGES = {
    "RELATION_DISPOSITION_MISSING": (
        "This page links to no other page, so it needs a link or a reason it stands alone."
    ),
    "RELATION_DISPOSITION_STALE": (
        "The reason this page stands alone no longer holds, so it needs a link or a new reason."
    ),
}
_UNLINKED_REASON = "<why this page stands alone>"


def _sentence(text: object) -> str:
    """One trimmed sentence that ends in a full stop."""
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    if value.split(" ", 1)[0].isalpha():
        # An identifier such as a tool name keeps its spelling; only a plain word is capitalised.
        value = value[0].upper() + value[1:]
    return value if value[-1] in ".!?" else f"{value}."


def _fix(refusal: Mapping[str, Any]) -> str:
    """The first sentence of the remediation: the step that clears the refusal.

    Two sentences meet at an end mark, a space and a capital letter. "e.g." is followed by a
    lower-case word, so it does not split.
    """
    text = " ".join(str(refusal.get("remediation") or "").split())
    for index in range(len(text) - 2):
        if text[index] in ".!?" and text[index + 1] == " " and text[index + 2].isupper():
            return text[: index + 1]
    return _sentence(text)


def _candidates(refusal: Mapping[str, Any]) -> list[str]:
    """The values the agent can choose from, from the producer that knows them.

    A producer that has `candidates` of its own names them first. `allowed` is the closed set of
    values a refused argument accepts. A producer that only ranks `options` by `key` (the
    generic-relation barrier) is read last.
    """
    for name in ("candidates", "allowed"):
        value = refusal.get(name)
        if isinstance(value, (list, tuple)) and value:
            return [str(item) for item in value]
    options = refusal.get("options")
    if isinstance(options, (list, tuple)):
        return [str(option["key"]) for option in options if isinstance(option, Mapping) and "key" in option]
    return []


def _located(message: str, refusal: Mapping[str, Any]) -> str:
    """A refusal that names the field it failed on, and how to fix it, keeps both in its message.

    The query engine publishes this shape (`at`, `repair`); the agent cannot correct the call
    without them, and `expected` alone ("closed enumerated value") does not say where.
    """
    at = refusal.get("at")
    if not isinstance(at, str) or not at:
        return message
    located = f"{message[:-1] if message else 'Refused'} at {at}."
    return " ".join(filter(None, (located, _sentence(refusal.get("repair")))))


def _blocking_finding(error: BaseException) -> Any | None:
    """The first finding the semantic contract blocked on, found on the exception chain."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        findings = getattr(current, "validation_findings", None)
        if findings:
            return findings[0]
        current = current.__cause__ or current.__context__
    return None


def _fate(refusal: Mapping[str, Any], *, write: bool) -> str | None:
    """What happened to the data, for a refused write; None when the refusal states it itself."""
    if not write:
        return None
    outcome = refusal.get("outcome")
    if isinstance(outcome, Mapping):
        # A batch refusal states its own outcome. Its message names a failed step, so say when the
        # change was nevertheless saved; an unsaved or partial one is named by the message itself.
        return _SAVED if outcome.get("committed") is True else None
    if "committed" not in refusal:
        # A coded refusal a write leaf raises comes before its commit, so nothing changed.
        return _NOTHING_SAVED
    committed = refusal["committed"]
    if committed is True:
        return _SAVED
    return _NOTHING_SAVED if committed is False else _SAVE_UNKNOWN


def _after_seconds(refusal: Mapping[str, Any]) -> int | None:
    wait = refusal.get("retry_after_ms")
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait <= 0:
        return None
    return max(1, math.ceil(wait / 1000))


def _busy_message(refusal: Mapping[str, Any]) -> str:
    waited = refusal.get("waited_ms")
    seconds = round(waited / 1000) if isinstance(waited, (int, float)) and waited >= 0 else 0
    held = (
        f"Another write held memory for {seconds} seconds"
        if seconds >= 2
        else "Memory was busy with another write"
    )
    return f"{held}, so this was not saved. Retry the same call."


def _relation_retry(tool: str, parameters: Collection[str]) -> dict[str, Any] | None:
    """The call that records a reviewed-none decision, placed where this tool takes it."""
    decision = {"relation_disposition": "reviewed_none", "relation_review_reason": _UNLINKED_REASON}
    if "relation_disposition" in parameters:
        return {"tool": tool, "arguments": decision}
    if "operation" in parameters:
        return {"tool": tool, "arguments": {"operation": decision}}
    return None


def reply(
    refusal: Mapping[str, Any],
    error: BaseException,
    *,
    tool: str,
    parameters: Collection[str],
    write: bool,
    full: bool,
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """Shape the public refusal block for an MCP agent; `envelope` is the old reply, for `full`."""
    code = str(refusal.get("code") or "OP_ERROR")
    message = _located(_sentence(refusal.get("message")), refusal)
    retry: dict[str, Any] | None = None
    fate = _fate(refusal, write=write)
    fix = _fix(refusal)
    if code == _UMBRELLA_CODE and (finding := _blocking_finding(error)) is not None:
        code = str(finding.code)
        message = _sentence(finding.detail) or message
        if code in _RELATION_DECISION_MESSAGES:
            message = _RELATION_DECISION_MESSAGES[code]
            retry = _relation_retry(tool, parameters)
    if code == _BUSY_CODE:
        message, fate, fix = _busy_message(refusal), None, ""
    after = _after_seconds(refusal)
    if retry is None and (
        refusal.get("status") == "retryable" or refusal.get("retryable") is True or after is not None
    ):
        # A refusal that advises a wait (`retry_after_ms`) is retryable by definition: warming
        # recall carries one without the `retryable` status. `retryable` is the query engine's
        # own flag for the same fact.
        retry = {"tool": tool}
        if after is not None:
            retry["after_s"] = after
    if retry is not None or fix.casefold() in message.casefold():
        # A structured retry is the fix; a repair that the message already carries is not said twice.
        fix = ""
    body: dict[str, Any] = {"code": code, "message": " ".join(filter(None, (message, fix, fate)))}
    if retry is not None:
        body["retry"] = retry
    if candidates := _candidates(refusal):
        body["candidates"] = candidates[:MAX_CANDIDATES]
    result: dict[str, Any] = {"error": body}
    if full:
        result["diagnostics"] = dict(envelope)
    return result
