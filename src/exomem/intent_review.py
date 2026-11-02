"""Write-conflict review items (design D5): `review_memory` shows one, `triage_memory` decides it.

A `conflicted` intent waits for an owner decision. Its reference is `exomem://write/<intent_id>`, the one the
accepted answer gave. The reader sees only intents of its own audience, except the owner, who sees all, and only
those whose planned pages the reader's own client binding could write now: the check the synchronous write makes.
A reader with no right to an intent gets the answer for an unknown reference.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from . import (
    intent_accept,
    intent_applier,
    intent_outcomes,
    request_budget,
    state_paths,
    write_intents,
)

log = logging.getLogger(__name__)

REF_PREFIX = "exomem://write/"
ACTION_RETRY = "retry-write"
ACTION_DISCARD = "discard-write"
ACTIONS = (
    ACTION_RETRY,
    ACTION_DISCARD,
)  # nosemgrep: ep-word-set -- the closed decisions this review item offers

#: The longest one poll may wait, whatever the caller asks. A poll with a request budget also ends before the
#: budget less its delivery reserve; this bound covers the in-process caller that has no budget.
MAX_POLL_WAIT_SECONDS = 120.0

# Causes where a retry cannot succeed on its own, with the reason the owner reads.
_BASIS_CAUSES = (
    intent_outcomes.CAUSE_BASIS_CHANGED,
    intent_outcomes.CAUSE_VOCABULARY_CHANGED,
)  # nosemgrep: ep-word-set -- closed causes the applier writes


def is_write_ref(ref: str | None) -> bool:
    return isinstance(ref, str) and ref.strip().lower().startswith(REF_PREFIX)


def _unknown(ref: str) -> ValueError:
    """The one answer for a malformed, unknown or foreign reference, so a reader learns nothing about other audiences."""
    return ValueError(f"REVIEW_ITEM_NOT_FOUND: no write intent for {ref!r}")


def _manager_and_journal(vault_root: Path) -> tuple[Any, write_intents.WriteIntentJournal]:
    from . import writer_lease

    manager = writer_lease.active_manager()
    try:
        journal = manager._intent_journal(vault_root)
    except write_intents.JournalRefusal as error:
        raise ValueError(f"WRITE_JOURNAL_UNAVAILABLE: {error}") from error
    if journal is None:
        raise ValueError("WRITE_JOURNAL_UNAVAILABLE: this path has no write journal")
    return manager, journal


def _reader() -> tuple[str, bool]:
    from .governance.principal import OWNER_AUDIENCE, effective_principal

    audience = effective_principal().audience_id
    return audience, audience == OWNER_AUDIENCE


def _admitted(vault_root: Path, intent: write_intents.Intent) -> bool:
    """Whether this reader's audience and client binding admit every page the intent plans to write.

    It runs the check a synchronous write runs (`connector_boundary.require_write`), for the reader and not the
    caller who made the intent, so a restricted client of the owner's audience learns nothing of a private page.
    """
    from .governance import connector_boundary

    audience, owner = _reader()
    if not (owner or intent.audience == audience):
        return False
    try:
        for path in intent.planned_paths or ():
            connector_boundary.require_write(vault_root, path, content=None)
    except ValueError:
        return False
    return True


def _intent_for(
    vault_root: Path, journal: write_intents.WriteIntentJournal, ref: str
) -> tuple[write_intents.Intent, bool]:
    """The intent this reader may see, and whether the reader is the owner."""
    _, owner = _reader()
    intent_id = ref.strip()[len(REF_PREFIX) :].lower()
    intent = journal.get(intent_id) if is_write_ref(ref) and intent_id.isalnum() else None
    if intent is None or not _admitted(vault_root, intent):
        raise _unknown(ref)
    return intent, owner


def _retry_refusal(intent: write_intents.Intent, *, owner: bool, rebase: bool) -> str | None:
    """Why `retry-write` is refused for this reader and intent, or None when it may go ahead."""
    cause = (intent.conflict or {}).get("cause")
    if intent.state != "conflicted":
        return f"only a conflicted write can be retried; this one is {intent.state}"
    if cause in intent_outcomes.AUTHORIZATION_CAUSES:
        return "the caller's authorization is gone; send the write again under a live login, or discard this item"
    if cause == intent_outcomes.CAUSE_OUTCOME_UNKNOWN and not owner:
        return "only the vault owner may retry a write whose outcome is unknown; a retry can write twice"
    arguments = intent.arguments or {}
    if rebase and cause not in _BASIS_CAUSES:
        return f"rebase applies to a changed basis, not to cause {cause!r}"
    if cause == intent_outcomes.CAUSE_BASIS_CHANGED and not rebase:
        return "the page changed under the caller's basis; retry with rebase=true to apply the write to current content"
    if rebase and any(
        intent_accept.argument_value(arguments, name) is not None
        for name in intent_accept.UNREBASABLE_ARGUMENTS
    ):
        return "a write bound to a draft or transition token cannot rebase; discard it and prepare a new draft"
    return None


def _terminal_for_reader(vault_root: Path, intent: write_intents.Intent) -> Any:
    """The stored terminal as the synchronous path would project it for this reader's current authority.

    None when the filter refuses it: the reader then learns the intent ended, not what it returned.
    """
    from .governance import egress

    try:
        return egress.postfilter(intent.command, intent.terminal, vault_root)
    except Exception:  # noqa: BLE001 - a refusal or filter fault withholds the terminal; it never fails the poll
        log.warning("the terminal of write %s was withheld from this reader", intent.intent_id, exc_info=True)
        return None


def item(vault_root: Path, ref: str) -> dict[str, Any]:
    """The review item of one write intent: cause, evidence, the decisions open to this reader, retained arguments."""
    _, journal = _manager_and_journal(vault_root)
    intent, owner = _intent_for(vault_root, journal, ref)
    conflict = intent.conflict or {}
    now = intent_accept.planned_hashes(vault_root, intent.planned_paths)
    at_accept = intent.target_hashes or {}
    references = (intent.base_guards or {}).get(intent_accept.TARGET_REFERENCES) or {}
    overtaking = [
        {
            "ref": f"{REF_PREFIX}{later.intent_id}",
            "command": later.command,
            "state": later.state,
            "planned_paths": later.planned_paths,
        }
        for later in journal.after(intent.seq)
        if not set(later.planned_paths or ()).isdisjoint(intent.planned_paths or ())
        and _admitted(vault_root, later)
    ]
    view: dict[str, Any] = {
        "ref": f"{REF_PREFIX}{intent.intent_id}",
        "intent_id": intent.intent_id,
        "state": intent.state,
        "command": intent.command,
        "cause": conflict.get("cause"),
        "validation": intent.validation,
        "accepted_at": intent.accepted_at,
        "attempts": intent.attempts,
        "targets": {
            path: {
                "reference": references.get(path),
                "at_accept": at_accept.get(path),
                "now": now.get(path),
            }
            for path in intent.planned_paths or ()
        },
        "evidence": conflict.get("evidence") or {},
        "conflict": {key: value for key, value in conflict.items() if key != "evidence"},
    }
    if intent.wait_cause is not None and intent.active:
        view["wait_cause"] = intent.wait_cause
    if intent.terminal is not None:
        terminal = _terminal_for_reader(vault_root, intent)
        if terminal is None:
            view["terminal_withheld"] = True
        else:
            view["terminal"] = terminal
    if intent.state != "conflicted":
        return view
    view["arguments"] = intent.arguments
    view["later_intents"] = overtaking
    if overtaking:
        view["order_note"] = (
            "later writes already touched these targets; a retry applies this write after them"
        )
    refusal = _retry_refusal(intent, owner=owner, rebase=conflict.get("cause") in _BASIS_CAUSES)
    view["decisions"] = {
        ACTION_DISCARD: {"allowed": True},
        ACTION_RETRY: {
            "allowed": refusal is None,
            "needs_rebase": conflict.get("cause") == intent_outcomes.CAUSE_BASIS_CHANGED,
            **({"refused_because": refusal} if refusal else {}),
        },
    }
    return view


def triage(
    vault_root: Path, ref: str, action: str, *, why: str | None = None, rebase: bool = False
) -> dict[str, Any]:
    """Decide one write conflict: `retry-write` at the end of the line under a new key, or `discard-write`."""
    manager, journal = _manager_and_journal(vault_root)
    intent, owner = _intent_for(vault_root, journal, ref)
    if action not in ACTIONS:
        raise ValueError(f"INVALID_REVIEW_ACTION: a write conflict takes {' or '.join(ACTIONS)}")
    if rebase and action != ACTION_RETRY:
        raise ValueError("INVALID_REVIEW_ACTION: rebase applies to retry-write only")
    reference = f"{REF_PREFIX}{intent.intent_id}"
    if action == ACTION_DISCARD:
        reason = (why or "").strip() or "discarded by decision"
        if not intent_applier.discard(journal, vault_root, intent.intent_id, reason):
            raise ValueError(
                f"WRITE_DECISION_REFUSED: only a conflicted write can be discarded; this one is {intent.state}"
            )
        return {"ref": reference, "action": action, "state": "discarded", "reason": reason}
    refusal = _retry_refusal(intent, owner=owner, rebase=rebase)
    if refusal is not None:
        raise ValueError(f"WRITE_DECISION_REFUSED: {refusal}")
    from . import writer_lease
    from .governance.principal import effective_principal

    arguments = dict(intent.arguments or {})
    guards = dict(intent.base_guards or {})
    if rebase:
        arguments = intent_accept.without_arguments(arguments, intent_accept.REBASE_GUARDS)
        guards = {
            key: value for key, value in guards.items() if key not in intent_accept.REBASE_GUARDS
        }
    command = intent_applier._command(intent.command, intent.surface)
    digest = (
        writer_lease._command_digest(command, arguments) if command is not None else intent.digest
    )
    # The old intent ends `discarded` and releases its vocabulary binding first; the retry never rebinds the old key.
    intent_applier.release_vocabulary_binding(vault_root, intent)
    retried = journal.retry(
        intent.intent_id,
        arguments=arguments,
        digest=digest,
        base_guards=guards,
        reason=f"retried as a new write{' on current content' if rebase else ''}",
        audience=_reader()[0],
        principal=intent_accept.principal_snapshot(effective_principal()),
    )
    if retried is None:
        raise ValueError("WRITE_DECISION_REFUSED: the write is no longer conflicted")
    intent_applier.ensure_running(manager, vault_root)
    return {
        "ref": reference,
        "action": action,
        "state": "discarded",
        "retried_as": {
            "ref": f"{REF_PREFIX}{retried.intent_id}",
            "intent_id": retried.intent_id,
            "state": retried.state,
            "retry_key": retried.retry_key,
            "rebase": rebase,
        },
    }


def conflict_count(vault_root: Path) -> dict[str, Any]:
    """The reader's count of undecided write conflicts, for the attention review (design D5).

    A vault with no journal has no conflicts, and says so; an unreadable journal says it is unavailable, never 0.
    """
    if not (state_paths.vault_state_dir(vault_root) / write_intents.JOURNAL_FILENAME).exists():
        return {"count": 0}
    audience, owner = _reader()
    try:
        _, journal = _manager_and_journal(vault_root)
        count = sum(
            1
            for row in journal.conflicted(audience=None if owner else audience)
            if _admitted(vault_root, row)
        )
    except (ValueError, write_intents.JournalRefusal):
        log.warning(
            "the write journal could not be counted for the attention review", exc_info=True
        )
        return {"count": None, "status": "unavailable"}
    return {"count": count}


def _wait_budget(wait_seconds: float) -> float:
    """How long a poll may wait: the ask, bounded, and within the request budget less its delivery reserve."""
    wait = max(0.0, min(float(wait_seconds), MAX_POLL_WAIT_SECONDS))
    budget = request_budget.current()
    if budget is not None:
        left = budget.deadline - request_budget.DELIVERY_RESERVE_SECONDS - time.monotonic()
        wait = min(wait, max(0.0, left))
    return wait


def poll(vault_root: Path, ref: str, wait_seconds: float = 0.0) -> dict[str, Any]:
    """The state of one write intent, waiting up to `wait_seconds` for it to end (design D6).

    The wait takes a waiter slot from the cap other request waits use. With no slot free, the poll returns the
    current state at once and says so.
    """
    _, journal = _manager_and_journal(vault_root)
    intent, _ = _intent_for(vault_root, journal, ref)
    from . import writer_lease

    wait = _wait_budget(wait_seconds)
    started = time.monotonic()
    refused = False
    if intent.active and wait > 0:
        with writer_lease.request_waiter_slot() as admitted:
            refused = not admitted
            end = started + wait
            while admitted and intent.active and end - time.monotonic() > 0:
                intent_accept.wait_for_applier_progress(
                    min(intent_accept.POST_ROW_POLL_SECONDS, end - time.monotonic())
                )
                try:
                    latest = journal.get(intent.intent_id)
                except write_intents.JournalRefusal:
                    break
                if latest is None:
                    break
                intent = latest
    view = item(vault_root, ref)
    view["waited_seconds"] = round(time.monotonic() - started, 3)
    if refused:
        view["wait_refused"] = "no_waiter_slot"
    if view["state"] in write_intents.ACTIVE_STATES:
        view["poll_after_ms"] = intent_accept.POLL_AFTER_MS
    return view


def listing(vault_root: Path, limit: int) -> dict[str, Any]:
    """The reader's own non-terminal and conflicted writes, newest first, at most `limit` (design D6).

    The owner lists every audience's. A vault with no journal has none, and creating one is not a read's job.
    """
    if not (state_paths.vault_state_dir(vault_root) / write_intents.JOURNAL_FILENAME).exists():
        return {"writes": [], "returned": 0, "truncated": False}
    audience, owner = _reader()
    _, journal = _manager_and_journal(vault_root)
    try:
        # Admission filters after the read, so the read takes every open row (the journal bound keeps them few).
        rows = [
            row
            for row in journal.open_intents(audience=None if owner else audience)
            if _admitted(vault_root, row)
        ]
    except write_intents.JournalRefusal as error:
        raise ValueError(f"WRITE_JOURNAL_UNAVAILABLE: {error}") from error
    writes = [
        {
            "ref": f"{REF_PREFIX}{row.intent_id}",
            "state": row.state,
            "command": row.command,
            "planned_paths": row.planned_paths,
            "accepted_at": row.accepted_at,
            "cause": (row.conflict or {}).get("cause") or (row.wait_cause if row.active else None),
        }
        for row in rows[:limit]
    ]
    return {"writes": writes, "returned": len(writes), "truncated": len(rows) > limit}


def await_end(vault_root: Path, ref: str) -> dict[str, Any]:
    """Wait with no bound until the write ends, for an in-process caller that loses no reply (design D1, D8).

    The caller interrupts it (Ctrl-C), which leaves the intent queued for the next applier.
    """
    while True:
        view = poll(vault_root, ref, MAX_POLL_WAIT_SECONDS)
        if view["state"] not in write_intents.ACTIVE_STATES:
            return view
        if view.get("wait_refused"):
            # No slot to park on: look again after the interval a poll advises, never in a tight loop.
            time.sleep(intent_accept.POLL_AFTER_MS / 1000)
