"""Once-per-conversation vocabulary notices, subordinate to durable review state.

Only internal evidence providers call this owner. Explicit review remains
available when a family is quiet. Notification suppression never closes an
integrity finding or grants permission to mutate canonical content.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import review_state, vocabulary_notice_index
from .governance.principal import effective_principal
from .vocabulary_signals import compact_advisory
from .vocabulary_workflow import WorkItem, _hash

REVIEW_FAMILIES = MappingProxyType(
    {
        "entity-instance/v1": "vocabulary_entity_instances",
        "entity-type/v1": "vocabulary_entity_types",
        "relation-type/v1": "vocabulary_relation_types",
    }
)

# Only adapted signals inherit an existing review owner's family. Integrity
# findings remain independent from vocabulary consideration.
ORIGINATING_REVIEW_FAMILIES = MappingProxyType({"entity-lifecycle": "entity_recurrence"})
PROJECTION_FAMILIES = frozenset(
    (*REVIEW_FAMILIES.values(), *ORIGINATING_REVIEW_FAMILIES.values())
)


def review_families(family: str, signal: str) -> tuple[str, ...]:
    """The bounded set of dispositions that compose for one consideration."""
    mapped = REVIEW_FAMILIES.get(family, family)
    origin = ORIGINATING_REVIEW_FAMILIES.get(signal)
    return (mapped, origin) if origin is not None and origin != mapped else (mapped,)


def origin_review_binding(item: Mapping[str, Any]) -> tuple[str, str] | None:
    """Read the lifecycle owner's exact material binding, never reconstruct it."""
    if item.get("signal") != "entity-lifecycle":
        return None
    currency = item.get("projection", {}).get("currency", {})
    fingerprint = currency.get("review_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 24 or any(
        char not in "0123456789abcdef" for char in fingerprint
    ):
        return None
    try:
        return review_state.parse_review_ref(currency.get("review_ref")), fingerprint
    except ValueError:
        return None


def origin_review_status(
    vault_root: Path, item: Mapping[str, Any], *, payload: dict[str, Any]
) -> dict[str, Any] | None:
    if item.get("signal") != "entity-lifecycle":
        return None
    binding = origin_review_binding(item)
    if binding is None:
        return {
            "state": "refresh_required",
            "context_route": {"tool": "review_item_context", "args": {"ref": item["ref"]}},
        }
    review_id, fingerprint = binding
    state, decision = review_state.ReviewStateStore(vault_root).effective_state(
        review_id, fingerprint, payload=payload
    )
    return {
        "ref": review_state.review_ref(review_id), "fingerprint": fingerprint,
        "state": state, "decision": decision.as_dict() if decision else None,
    }


# Legacy JSON notices and the machine-local reservation index retain the same
# bounded history as the surfaced ledger.
RETENTION_DAYS = review_state._LEDGER_RETENTION_DAYS


def reconcile_legacy(vault_root: Path) -> dict[str, int]:
    """Move retained legacy JSON reservations into the bounded SQLite index."""
    owner = review_state.ReviewStateStore(vault_root)
    now = dt.datetime.now(dt.UTC)
    imported = 0
    dropped = 0
    with review_state._LOCK:
        payload = owner.load()
        section = payload["vocabulary"]
        notifications = section["notifications"]
        for row in notifications.values():
            if not isinstance(row, dict):
                dropped += 1
                continue
            ref = row.get("item_ref")
            fingerprint = row.get("fingerprint")
            item = section["items"].get(ref) if isinstance(ref, str) else None
            if (
                not isinstance(ref, str)
                or not isinstance(fingerprint, str)
                or not isinstance(item, dict)
                or item.get("fingerprint") != fingerprint
                or f"{ref}:{fingerprint}" in section["decisions"]
            ):
                dropped += 1
                continue
            stamped = review_state._parse_stamp(row.get("reserved_at")) or now
            expires = stamped + dt.timedelta(days=RETENTION_DAYS)
            if expires < now:
                dropped += 1
                continue
            context = row.get("context")
            if not isinstance(context, str) or not context:
                dropped += 1
                continue
            vocabulary_notice_index.reserve(
                vault_root,
                context=context,
                item_ref=ref,
                fingerprint=fingerprint,
                expires_at=expires.timestamp(),
                now=now.timestamp(),
                reserved_at=stamped.timestamp(),
            )
            imported += 1
        if notifications:
            notifications.clear()
            owner._write(payload, vocabulary_refs=(), vocabulary_families=())
    return {"imported": imported, "dropped": dropped}


def take_advisory(vault_root: Path, items: Sequence[WorkItem]) -> dict[str, Any] | None:
    """Observe one bounded provider page and durably reserve at most one notice.

    A sessionless client gets conservative audience-level deduplication, not a
    fabricated conversation identity. Only a hash of context is retained.
    """
    if len(items) > 4:
        raise ValueError("VOCABULARY_LIMIT_INVALID: publish at most one provider page")
    actor = effective_principal()
    if not actor.resolved:
        return None
    context = _hash([actor.audience_id, actor.authorization_session_id or actor.session_id])
    owner = review_state.ReviewStateStore(vault_root)
    with review_state._LOCK:
        payload = owner.load()
        section = payload["vocabulary"]
        observed = tuple(item.ref for item in items)
        before = {ref: section["items"].get(ref) for ref in observed}
        now = dt.datetime.now(dt.UTC)
        selected = None
        for item in items:
            section["items"][item.ref] = item.to_dict()
        changed = any(section["items"].get(ref) != before[ref] for ref in observed)
        if section["notifications"]:
            if changed:
                owner._write(
                    payload,
                    vocabulary_refs=tuple(item.ref for item in items),
                    vocabulary_families=(),
                )
            return None
        if changed:
            owner._write(
                payload,
                vocabulary_refs=tuple(item.ref for item in items),
                vocabulary_families=(),
            )
        expires_at = (now + dt.timedelta(days=RETENTION_DAYS)).timestamp()
        for item in items:
            decision_key = f"{item.ref}:{item.fingerprint}"
            origin = origin_review_status(vault_root, item.to_dict(), payload=payload)
            if (
                selected is not None
                or item.projection_status != "current"
                or decision_key in section["decisions"]
                or (origin is not None and origin["state"] != "open")
                or any(
                    review_state.disposition_for(family, payload=payload) != "normal"
                    for family in review_families(item.family, item.signal)
                )
            ):
                continue
            try:
                reserved = vocabulary_notice_index.reserve(
                    vault_root,
                    context=context,
                    item_ref=item.ref,
                    fingerprint=item.fingerprint,
                    expires_at=expires_at,
                    now=now.timestamp(),
                )
            except (OSError, sqlite3.Error):
                return None
            if not reserved:
                continue
            selected = compact_advisory(
                ref=item.ref, family=item.family, fingerprint=item.fingerprint
            )
        return selected
