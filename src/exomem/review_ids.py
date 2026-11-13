"""The short id an agent names one review item by, and how a decision binds it.

A review item carries a `ref` that names it and a `fingerprint` that names the signal as read. The
id is `<head of ref>.<tail of fingerprint>`. Triage binds it before writer admission: the item that
still holds the ref and the fingerprint supplies the guards the decision needs, and a stale id is
refused with the current one. A stored due entry binds its own id (`bind_decision`); a review queue
is read only for an id that stored state cannot settle (`bind`).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from typing import Any

from . import page_ids, relation_queue, review_state
from .cli_ops import OpError
from .public_tools import REVIEW_QUEUES

#: The queues a decision scans for an id: every queue that list_review_items lists, so an id that a
#: listing gave is found where it came from. One source: the listing's own queue table.
QUEUE_MODES: tuple[str, ...] = tuple(dict.fromkeys(REVIEW_QUEUES.values()))
RELATION_QUEUE_MODES: tuple[str, ...] = (REVIEW_QUEUES["relation_suggestions"],)
#: The queues that `attention` and `activation` rebuild from the vault on every read. Each stamps
#: the items it returns on the first-surfaced ledger, so the ledger knows every id they gave out.
#: A closed subset of the listing's own queue table: the modes that `op_review_memory` routes there.
_LISTED_QUEUES = ("due", "activation", "stale", "contradiction", "unprocessed_sources", "unlinked")  # nosemgrep: ep-word-set -- the closed queue names of REVIEW_QUEUES whose modes op_review_memory routes to attention and activation.
LISTED_QUEUE_MODES: tuple[str, ...] = tuple(REVIEW_QUEUES[queue] for queue in _LISTED_QUEUES)
#: The queues that read their own store. Their items are not on the ledger, and one read is cheap.
UNLISTED_QUEUE_MODES: tuple[str, ...] = tuple(
    mode for mode in QUEUE_MODES if mode not in LISTED_QUEUE_MODES
)
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


def _listed(surfaced: Mapping[str, Any], item: str, *, exact: bool) -> bool:
    """Whether a review listing showed an item with this id's head, or (`exact`) this very id.

    `surfaced` is the first-surfaced ledger, keyed `<review id>:<fingerprint>`. Every listing of a
    `LISTED_QUEUE_MODES` queue stamps the items it returns there with the `review` surface.
    """
    head = item.partition(".")[0]
    for key, row in surfaced.items():
        if not isinstance(row, Mapping) or row.get("surface") != "review":
            continue
        review_id, _, fingerprint = key.partition(":")
        try:
            ref = review_state.review_ref(review_id)
        except ValueError:
            continue
        if page_ids.part_head(ref) == head and (
            not exact or page_ids.part_id(ref, fingerprint) == item
        ):
            return True
    return False


def bind_decision(
    read: Callable[..., Any],
    *,
    item: str,
    stored: list[tuple[str, str]],
    surfaced: Callable[[], Mapping[str, Any]],
) -> dict[str, str]:
    """What a triage decision's `item` stands for, read from stored state before any queue.

    `stored` holds the `(ref, fingerprint)` of each stored due entry with the id's head
    (`due_state.stored_by_head`). A due entry's id is given out by responses that are not review
    listings, for families that no listing holds, so only the stored entry can bind it. An entry
    that holds the head with another fingerprint makes the id stale, unless a listing showed this
    very id: a listed item can fuse the entry with other signals of its page.

    A queue is read only for an id that stored state cannot settle. The queues that rebuild their
    items from the vault are read only for an id the ledger says a listing showed, so an id the
    service never gave out is refused without a pass over the vault.
    """
    for ref, fingerprint in stored:
        if page_ids.part_id(ref, fingerprint) == item:
            return {"ref": ref, "expected_fingerprint": fingerprint}
    listed = _listed(surfaced(), item, exact=bool(stored))
    if stored and not listed:
        current = page_ids.part_id(*stored[0])
        raise OpError(
            "STALE_ITEM",
            f"review item {item} is stale; it now has id {current}. Retry with the current id.",
            details={"current_id": current},
        )
    return bind(read, item=item, modes=QUEUE_MODES if listed else UNLISTED_QUEUE_MODES)

