"""How an agent names a collection and one of its rows: by title, and by a short id.

A collection is named by its title. A row is named by `<head>.<version>`: the head names the row
and survives edits, the version names the row as the caller read it. The row's key, version and the
container hash that the leaf guards need never reach the agent. The leaf binds the id here, after
admission and under the writer lease, by reading the collection once. A stale id is refused with
the current one, so the caller rereads nothing it does not need. The row's key (its record id) is
no row id: it names no version, so it guards no edit, and the leaf refuses it and says so.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import collection_profiles, memory_refs, page_ids, record_governance, vault
from .cli_ops import OpError

#: Pages of rows read to find one id. A collection larger than this refuses rather than guess.
_MAX_SCAN_PAGES = 20
_SCAN_LIMIT = 500
#: The reply fields that can carry a row's id, in order: the first one the collection does not declare.
#: They are this tool's own reply names, not data, so code fixes them.
_ID_SLOTS = ("id", "row_id")
#: Where a reply puts a row's id, said once for each message that sends a caller to it.
_ID_SLOT_RULE = f"`{_ID_SLOTS[0]}` (`{_ID_SLOTS[1]}` where the collection declares its own `{_ID_SLOTS[0]}` field)"


def names_a_manifest(selector: str) -> bool:
    """Whether `selector` names a collection by reference, path or UUID. Any other spelling is a title."""
    return (
        selector.lower().startswith("exomem://")
        or selector.endswith(".md")
        or "/" in selector
        or memory_refs.normalize_id(selector) is not None
    )


def resolve_collection(
    vault_root: Path, selector: str | None, *, profile: str, unserved: Callable[[], str | None] | None = None,
    certain: bool = False,
) -> str | None:
    """The manifest path of the collection `selector` names by title; `selector` otherwise.

    A path, a reference or a UUID passes unchanged. A title that names no visible collection of this
    profile also passes unchanged, so the leaf's own refusal for an unknown collection still answers:
    a collection that the caller may not see answers as one that does not exist. Titles compare as
    the vault compares every title (`vault.title_key`: trimmed, normalized and case-insensitive).
    `unserved` answers instead where no readable collection carries the title but a visible one lives
    in a store this thread does not serve, so whether it carries the title is unknown here. With
    `certain`, it answers there even when one readable collection carries the title, because an
    unread one may carry it too.
    """
    if selector is None or names_a_manifest(selector.strip()):
        return selector
    key = vault.title_key(selector)
    titles, unread = record_governance.visible_collection_titles(vault_root, semantic_profile=profile)
    matches = sorted(path for title, path in titles if vault.title_key(title) == key)
    if len(matches) > 1:
        # The refusal names these manifests, so each is recorded as released, as the inventory records it.
        candidates = list(record_governance.released_manifest_paths(vault_root, matches))
        raise OpError(
            "AMBIGUOUS_COLLECTION",
            f"{len(candidates)} collections are titled {selector!r}; name one by its manifest path.",
            details={"candidates": candidates},
        )
    if unread and unserved is not None and (certain or not matches):
        return unserved()
    return matches[0] if matches else selector


def row_id(profile: str, row: Mapping[str, Any]) -> str | None:
    """The id of a query row that carries its key and version, else None."""
    key = row.get(collection_profiles.profile_for(profile).item_id_property)
    version = row.get("item_version")
    return page_ids.part_id(key, version) if isinstance(key, str) and isinstance(version, str) else None


def mark_rows(result: Any, *, profile: str) -> Any:
    """`result` with an `id` on each row that carries its key and version.

    A field the collection declares is stored data and stays. The id goes under the first of
    `_ID_SLOTS` that the collection does not declare, and a row that declares both has no short id.
    """
    rows = result.get("rows") if isinstance(result, Mapping) else None
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        slot = next((name for name in _ID_SLOTS if name not in row), None)
        if slot is not None and (found := row_id(profile, row)) is not None:
            row[slot] = found
    return result


def id_head(item_id: str) -> str:
    """The head that `item_id` asks for: a record id is a row's key, whose head is the key's own."""
    key = memory_refs.normalize_id(item_id)
    return page_ids.part_head(key) if key is not None else item_id.partition(".")[0]


def bound_row(item_id: str, matches: Sequence[tuple[str, str]], snapshot: str | None, *, profile: str) -> dict[str, str]:
    """The binding `item_id` stands for among the readable rows that carry its head (`id_head`).

    `matches` are `(item_key, item_version)` of the rows the caller may read, never of withheld
    ones, so a withheld row answers as an absent one. `snapshot` is the container hash the
    query of that read returned. A row's key is no row id: it is refused for a row the caller may
    read, and answers as no row otherwise.
    """
    key_named = memory_refs.normalize_id(item_id)
    if key_named is not None:
        if any(key == key_named for key, _ in matches):
            label = collection_profiles.profile_for(profile).item_id_property
            raise OpError(
                "ITEM_ID_IS_KEY",
                f"{item_id} is the row's {label}. It names the row but no version of it, so it cannot guard an "
                f"edit. Edit by the row's {_ID_SLOT_RULE}. A query returns it beside {label}, and it changes "
                "with each edit.",
            )
        matches = []
    if not matches:
        raise OpError(
            "ITEM_NOT_FOUND",
            f"id {item_id} names no row of this collection; query it again for the current ids.",
        )
    if len(matches) > 1 or snapshot is None:
        raise OpError("AMBIGUOUS_ITEM", f"id {item_id} names more than one row of this collection.")
    key, version = matches[0]
    current = page_ids.part_id(key, version)
    if current != item_id:
        raise OpError(
            "STALE_ITEM",
            f"id {item_id} is stale: the row has changed and now has id {current}. "
            "Query it again and retry with the current id.",
            details={"current_id": current},
        )
    return {"item_key": key, "expected_item_version": version, "expected_container_hash": snapshot}


def bind_item(
    query: Callable[..., Mapping[str, Any]], *, collection: str, item_id: str, profile: str
) -> dict[str, str]:
    """The `item_key`, `expected_item_version` and `expected_container_hash` that `item_id` stands for.

    `query` is the leaf's own query, so a file collection and a store collection answer alike.
    A store collection binds by `collection_store.preview.bind_row` first and reaches this scan
    only where that cannot answer.
    """
    column = collection_profiles.profile_for(profile).item_id_property
    head = id_head(item_id)
    found: list[Mapping[str, Any]] = []
    snapshot: str | None = None
    continuation: str | None = None
    for _ in range(_MAX_SCAN_PAGES):
        reply = query(
            collection=collection, columns=[column, "item_version"], limit=_SCAN_LIMIT,
            continuation=continuation,
        )
        snapshot = snapshot or reply.get("snapshot")
        found += [
            row for row in reply.get("rows", ())
            if isinstance(row.get(column), str) and isinstance(row.get("item_version"), str)
            and page_ids.part_head(row[column]) == head
        ]
        continuation = reply.get("continuation")
        if not continuation:
            break
    else:
        raise OpError(
            "ITEM_SCAN_INCOMPLETE",
            "this collection is too large to find a row by its id; filter the query to the row and use its key.",
        )
    return bound_row(item_id, [(row[column], row["item_version"]) for row in found], snapshot, profile=profile)
