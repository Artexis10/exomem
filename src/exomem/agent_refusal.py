"""The error result an agent reads when a tool call is refused.

`cli_ops.public_refusal` builds one public error block for every facade: a stable code, a message,
remediation prose, and whatever operational details the refusing layer attached. The CLI and REST
keep that block. An MCP agent needs less: what happened to its data, and the exact call that can
succeed. This module turns the public block into that, once, for every tool.

Compact result: `{"error": {"code", "message", "retry"?, "candidates"?}}`.
`message` says what was refused. When there is no `retry`, it carries the whole remediation next,
because that text is the only fix the agent gets. A refused write ends it with what happened to the
data.
`retry` is the call that can succeed, spelled for the serving surface. For the tool that was called,
`retry.arguments` holds only the arguments to add or change, and `null` clears one; for another
tool it holds the whole call. `retry` is absent when no retry can succeed. With
`response_detail="full"` the old envelope (umbrella code, remediation prose, ids, `committed`,
waits) rides beside it under `diagnostics`.

A producer states its next call and its outcome in its own details, in command terms, so that this
module keeps no knowledge of any producer: `retry_with` (arguments to change on the call that was
refused), `retry_call` (the whole of a different call) and `fate` (what happened to the data).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from typing import Any

from . import capabilities

MAX_CANDIDATES = 5
MAX_FIELDS = 3

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


def ranked_with_link(options: Collection[Mapping[str, Any]], link: str) -> list[str]:
    """The ranked option keys, then the plain link; the agent sees at most `MAX_CANDIDATES` names.

    The link keeps the last slot, and the ranking may list it too, so it is named once.
    """
    keys = [str(option["key"]) for option in options if option["key"] != link]
    return [*keys[: MAX_CANDIDATES - 1], link]


def _candidates(refusal: Mapping[str, Any]) -> list[Any]:
    """The values the agent can choose from, from the producer that knows them.

    A producer that has `candidates` of its own names them first; a candidate that is a mapping (a
    page named by title and id) reaches the agent whole. `allowed` is the closed set of values a
    refused argument accepts. A producer that only ranks `options` by `key` (the generic-relation
    barrier) is read last.
    """
    for name in ("candidates", "allowed"):
        value = refusal.get(name)
        if isinstance(value, (list, tuple)) and value:
            return [item if isinstance(item, Mapping) else str(item) for item in value]
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


def _busy_diagnostics(refusal: Mapping[str, Any]) -> dict[str, Any]:
    holder = _mapping(refusal.get("holder")) or {}
    diagnostics = {}
    if operation := holder.get("operation"):
        diagnostics["holder_operation"] = operation
    # These fields and units belong to the mutation-lock diagnostics contract.
    for name, value, scale in (
        ("holder_age_seconds", holder.get("age_seconds"), 1),
        ("caller_wait_seconds", refusal.get("waited_ms", refusal.get("wait_ms")), 1000),
    ):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            diagnostics[name] = round(value / scale, 1)
    return diagnostics


def _relation_decision(parameters: Collection[str], called: Mapping[str, Any]) -> dict[str, Any] | None:
    """The arguments that record a reviewed-none decision, in the terms of the command that was called.

    The command's own signature says where it takes the decision: a flat argument, or inside the
    `operation` object of a call that carries one. A command that takes neither has no such retry.
    """
    decision = {"relation_disposition": "reviewed_none", "relation_review_reason": _UNLINKED_REASON}
    if "relation_disposition" in parameters:
        return decision
    if isinstance(called.get("operation"), Mapping):
        return {"operation": decision}
    return None


def _merged(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """`patch` laid over `base`, key by key and through nested objects; a `null` stays a `null`."""
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        merged[key] = (
            _merged(current, value)
            if isinstance(value, Mapping) and isinstance(current, Mapping)
            else value
        )
    return merged


def _changes(base: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """What turns `base` into `new`: changed and added values, and `null` for each key `new` drops."""
    changes: dict[str, Any] = {}
    for key, value in new.items():
        if key not in base:
            if value is not None:
                changes[key] = value
        elif value != base[key]:
            current = base[key]
            both_objects = isinstance(value, Mapping) and isinstance(current, Mapping)
            changes[key] = _changes(current, value) if both_objects else value
    for key in sorted(set(base) - set(new)):
        changes[key] = None
    return changes


def _without_nulls(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    return value


def _retry_call(
    command: str,
    called: Mapping[str, Any],
    *,
    patch: Mapping[str, Any] | None,
    call: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The retry for a producer's next call, spelled for the serving surface.

    `patch` changes the arguments of the call that was refused; `call` is the whole of another
    call. Both are in command terms and go through `active_spell_call`, so the tool and the
    placement of every argument are the ones this surface takes. A call that no rendering carries
    has no retry: guidance for a tool that this surface cannot call is worse than none.
    """
    new = dict(call) if call is not None else _merged(called, patch or {})
    try:
        base_tool, base = capabilities.active_spell_call(command, called)
        tool, spelled = capabilities.active_spell_call(command, new)
    except ValueError:
        return None
    if tool != base_tool:
        return {"tool": tool, "arguments": _without_nulls(spelled)}
    changes = _changes(base, spelled)
    # A call with nothing to change is the call that was refused, and it cannot succeed.
    return {"tool": tool, "arguments": changes} if changes else None


def _called_tool(command: str, called: Mapping[str, Any]) -> str:
    try:
        return capabilities.active_spell_call(command, called)[0]
    except ValueError:
        # A call that no rendering carries was refused before it ran; the command's own name still names a real route.
        return command


def _plain(value: Any) -> Any:
    """A call's arguments as plain data: the models a command validated, dumped without their unset fields."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _at_fault(message: str, refusal: Mapping[str, Any]) -> str:
    """The fields a producer found at fault, up to `MAX_FIELDS`, when the message does not name them.

    A refusal that fails several fields reports them in `issues`, and its message says only that
    the item failed; the agent cannot correct a field it is not told.
    """
    issues = refusal.get("issues")
    if not isinstance(issues, (list, tuple)):
        return ""
    names = list(
        dict.fromkeys(
            str(issue["field"]) for issue in issues if isinstance(issue, Mapping) and issue.get("field")
        )
    )
    shown = names[:MAX_FIELDS]
    if not shown or all(name in message for name in shown):
        return ""
    more = len(names) - len(shown)
    listed = ", ".join(shown) + (f" and {more} more" if more else "")
    return f"{'Field' if len(names) == 1 else 'Fields'} at fault: {listed}."


def reply(
    refusal: Mapping[str, Any],
    error: BaseException,
    *,
    command: str,
    called: Mapping[str, Any],
    parameters: Collection[str],
    write: bool,
    full: bool,
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """Shape the public refusal block for an MCP agent; `envelope` is the old reply, for `full`.

    `command` and `called` are the refused command and the arguments it was called with, in
    command terms: every call this reply names is spelled from them for the serving surface.
    """
    called = _plain(called)
    code = str(refusal.get("code") or "OP_ERROR")
    message = _located(_sentence(refusal.get("message")), refusal)
    patch, call = _mapping(refusal.get("retry_with")), _mapping(refusal.get("retry_call"))
    if code == _UMBRELLA_CODE and (finding := _blocking_finding(error)) is not None:
        code = str(finding.code)
        message = _sentence(finding.detail) or message
        if code in _RELATION_DECISION_MESSAGES:
            message = _RELATION_DECISION_MESSAGES[code]
            patch = _relation_decision(parameters, called)
    fate = _sentence(refusal.get("fate")) or _fate(refusal, write=write)
    fix = _sentence(refusal.get("remediation"))
    if code == _BUSY_CODE:
        message, fate, fix = (
            "Memory was busy with another write, so this was not saved. Retry the same call.", None, ""
        )
    retry = (
        _retry_call(command, called, patch=patch, call=call)
        if patch is not None or call is not None
        else None
    )
    after = _after_seconds(refusal)
    if retry is None and (
        refusal.get("status") == "retryable" or refusal.get("retryable") is True or after is not None
    ):
        # A refusal that advises a wait (`retry_after_ms`) is retryable by definition: warming
        # recall carries one without the `retryable` status. `retryable` is the query engine's
        # own flag for the same fact.
        retry = {"tool": _called_tool(command, called)}
        if after is not None:
            retry["after_s"] = after
    if retry is not None or fix.casefold() in message.casefold():
        # A structured retry is the fix; a remediation that the message already carries is not said twice.
        fix = ""
    sentences = (message, _at_fault(message, refusal), fix, fate)
    body: dict[str, Any] = {"code": code, "message": " ".join(filter(None, sentences))}
    if code == _BUSY_CODE and (diagnostics := _busy_diagnostics(refusal)):
        body["diagnostics"] = diagnostics
    if retry is not None:
        body["retry"] = retry
    if candidates := _candidates(refusal):
        body["candidates"] = candidates[:MAX_CANDIDATES]
    result: dict[str, Any] = {"error": body}
    if full:
        result["diagnostics"] = dict(envelope)
    return result
