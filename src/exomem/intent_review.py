"""Write-conflict review items (design D5): `review_memory` shows one, `triage_memory` decides it.

A `conflicted` intent waits for an owner decision. Its reference is `exomem://write/<intent_id>`, the one the
accepted answer gave. The reader sees only intents of its own audience, except the owner, who sees all. A reader
with no right to an intent gets the answer for an unknown reference.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import intent_accept, intent_applier, intent_outcomes, state_paths, write_intents

log = logging.getLogger(__name__)

REF_PREFIX = "exomem://write/"
ACTION_RETRY = "retry-write"
ACTION_DISCARD = "discard-write"
ACTIONS = (
    ACTION_RETRY,
    ACTION_DISCARD,
)  # nosemgrep: ep-word-set -- the closed decisions this review item offers

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


def _intent_for(
    journal: write_intents.WriteIntentJournal, ref: str
) -> tuple[write_intents.Intent, bool]:
    """The intent this reader may see, and whether the reader is the owner."""
    audience, owner = _reader()
    intent_id = ref.strip()[len(REF_PREFIX) :].lower()
    intent = journal.get(intent_id) if is_write_ref(ref) and intent_id.isalnum() else None
    if intent is None or not (owner or intent.audience == audience):
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


def item(vault_root: Path, ref: str) -> dict[str, Any]:
    """The review item of one write intent: cause, evidence, the decisions open to this reader, retained arguments."""
    _, journal = _manager_and_journal(vault_root)
    intent, owner = _intent_for(journal, ref)
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
    if intent.terminal is not None:
        view["terminal"] = intent.terminal
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
    intent, owner = _intent_for(journal, ref)
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
        count = journal.count_conflicted() if owner else len(journal.conflicted(audience=audience))
    except (ValueError, write_intents.JournalRefusal):
        log.warning(
            "the write journal could not be counted for the attention review", exc_info=True
        )
        return {"count": None, "status": "unavailable"}
    return {"count": count}
