"""The short id an agent names one review item by, and how a decision binds it.

A review item carries a `ref` that names it and a `fingerprint` that names the signal as read. The
id is `<head of ref>.<tail of fingerprint>`. Triage binds it before writer admission: the item that
still holds the ref and the fingerprint supplies the guards the decision needs, and a stale id is
refused with the current one. A stored due entry binds its own id (`bind_decision`); the review
queues are read for any other id (`bind`).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from typing import Any

from . import page_ids, relation_queue
from .cli_ops import OpError
from .public_tools import REVIEW_QUEUES

#: The queues a decision scans for an id: every queue that list_review_items lists, so an id that a
#: listing gave is found where it came from. One source: the listing's own queue table.
QUEUE_MODES: tuple[str, ...] = tuple(dict.fromkeys(REVIEW_QUEUES.values()))
RELATION_QUEUE_MODES: tuple[str, ...] = (REVIEW_QUEUES["relation_suggestions"],)
_SCAN_LIMIT = 200


def item_id(item: Mapping[str, Any]) -> str | None:
    ref, fingerprint = item.get("ref"), item.get("fingerprint")
    if isinstance(ref, str) and isinstance(fingerprint, str):
        return page_ids.part_id(ref, fingerprint)
    return None


def mark_items(result: Any) -> Any:
    """`result` with an `id` on every review item in it, at any depth (queues nest items in groups)."""
    if isinstance(result, dict):
        if (found := item_id(result)) is not None:
            result["id"] = found
        for value in result.values():
            mark_items(value)
    elif isinstance(result, list):
        for value in result:
            mark_items(value)
    return result


def marks_items(read: Callable[..., Any]) -> Callable[..., Any]:
    """Decorate a review reader so each item it returns carries its id."""

    @functools.wraps(read)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return mark_items(read(*args, **kwargs))

    return wrapper


def _items(result: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(result, Mapping):
        if item_id(result) is not None:
            found.append(result)
        for value in result.values():
            found += _items(value)
    elif isinstance(result, list):
        for value in result:
            found += _items(value)
    return found


def bind(
    read: Callable[..., Any], *, item: str, modes: tuple[str, ...] = QUEUE_MODES
) -> dict[str, str]:
    """The `ref`, `expected_fingerprint` and `source_path` (a relation suggestion's page) that `item` stands for.

    `read(mode=..., limit=..., [state=...])` is the review reader. The first queue of `modes` that
    holds the id's head decides; a held head with another version is stale.
    """
    head = item.partition(".")[0]
    for mode in modes:
        try:
            # `state="all"` lets a dismissed or snoozed item be found to reopen; a queue without
            # states ignores it.
            reply = read(mode=mode, limit=_SCAN_LIMIT, state="all")
        except (ValueError, OpError):
            # A queue this principal or vault cannot read holds no item to bind; the next one may.
            continue
        held = [
            candidate for candidate in _items(reply)
            if page_ids.part_head(str(candidate["ref"])) == head
        ]
        if not held:
            continue
        current = held[0]
        if item_id(current) != item:
            raise OpError(
                "STALE_ITEM",
                f"review item {item} is stale; it now has id {item_id(current)}. "
                "List the review items again and retry with the current id.",
                details={"current_id": item_id(current)},
            )
        bound = {"ref": str(current["ref"]), "expected_fingerprint": str(current["fingerprint"])}
        if relation_queue.is_relation_ref(bound["ref"]) and isinstance(current.get("from"), str):
            bound["source_path"] = current["from"]
        return bound
    raise OpError(
        "ITEM_NOT_FOUND",
        f"review item {item} is on no review queue; list the review items for the current ids.",
    )


def bind_decision(
    read: Callable[..., Any], *, item: str, stored: list[tuple[str, str]]
) -> dict[str, str]:
    """What a triage decision's `item` stands for: its stored due entry, else what `bind` finds.

    `stored` holds the `(ref, fingerprint)` of each stored due entry with the id's head
    (`due_state.stored_by_head`). A due entry's id is given out by responses that are not review
    listings, for families that no listing holds, so only the stored entry can bind it.

    Only the entry's own id binds here. A listed item can fuse a stored entry with other signals
    of its page under the same reference, and only its queue knows that fingerprint, so an id
    that is not a stored entry's exact id binds from the queues as before.
    """
    for ref, fingerprint in stored:
        if page_ids.part_id(ref, fingerprint) == item:
            return {"ref": ref, "expected_fingerprint": fingerprint}
    return bind(read, item=item)
