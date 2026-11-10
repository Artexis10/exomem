"""How an agent names a collection and one of its rows: by title, and by a short id.

A collection is named by its title. A row is named by `<head>.<version>`: the head names the row
and survives edits, the version names the row as the caller read it. The row's key, version and the
container hash that the leaf guards need never reach the agent. The leaf binds the id here, after
admission and under the writer lease, by reading the collection once. A stale id is refused with
the current one, so the caller rereads nothing it does not need.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import collection_profiles, memory_refs, page_ids, record_governance, vault
from .cli_ops import OpError

#: Pages of rows read to find one id. A collection larger than this refuses rather than guess.
_MAX_SCAN_PAGES = 20
_SCAN_LIMIT = 500


def _names_a_manifest(selector: str) -> bool:
    return (
        selector.lower().startswith("exomem://")
        or selector.endswith(".md")
        or "/" in selector
        or memory_refs.normalize_id(selector) is not None
    )


def resolve_collection(vault_root: Path, selector: str | None, *, profile: str) -> str | None:
    """The manifest path of the collection `selector` names by title; `selector` otherwise.

    A path, a reference or a UUID passes unchanged. A title that names no collection also passes
    unchanged, so the leaf's own refusal for an unknown collection still answers.
    """
    if selector is None or _names_a_manifest(selector.strip()):
        return selector
    key = vault.title_key(selector)
    inventory = record_governance.inventory_collections(vault_root, semantic_profile=profile)
    matches = [
        row["manifest_path"] for row in inventory.get("collections", ())
        if vault.title_key(str(row.get("title") or "")) == key
    ]
    if len(matches) > 1:
        raise OpError(
            "AMBIGUOUS_COLLECTION",
            f"{len(matches)} collections are titled {selector!r}; name one by its manifest path.",
            details={"candidates": sorted(matches)},
        )
    return matches[0] if matches else selector


def row_id(profile: str, row: Mapping[str, Any]) -> str | None:
    """The id of a query row that carries its key and version, else None."""
    key = row.get(collection_profiles.profile_for(profile).item_id_property)
    version = row.get("item_version")
    return page_ids.part_id(key, version) if isinstance(key, str) and isinstance(version, str) else None


def mark_rows(result: Any, *, profile: str) -> Any:
    """`result` with an `id` on each row that carries its key and version.

    A field the collection declares is stored data and stays. The id goes under `id`, or under
    `row_id` where the collection declares an `id`, and a row that declares both has no short id.
    """
    rows = result.get("rows") if isinstance(result, Mapping) else None
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        slot = next((name for name in ("id", "row_id") if name not in row), None)
        if slot is not None and (found := row_id(profile, row)) is not None:
            row[slot] = found
    return result


def bind_item(
    query: Callable[..., Mapping[str, Any]], *, collection: str, item_id: str, profile: str
) -> dict[str, str]:
    """The `item_key`, `expected_item_version` and `expected_container_hash` that `item_id` stands for.

    `query` is the leaf's own query, so a file collection and a store collection answer alike.
    """
    column = collection_profiles.profile_for(profile).item_id_property
    head = item_id.partition(".")[0]
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
            if isinstance(row.get(column), str) and page_ids.part_head(row[column]) == head
        ]
        continuation = reply.get("continuation")
        if not continuation:
            break
    else:
        raise OpError(
            "ITEM_SCAN_INCOMPLETE",
            "this collection is too large to find a row by its id; filter the query to the row and use its key.",
        )
    if not found:
        raise OpError(
            "ITEM_NOT_FOUND",
            f"id {item_id} names no row of this collection; query it again for the current ids.",
        )
    if len(found) > 1 or snapshot is None:
        raise OpError("AMBIGUOUS_ITEM", f"id {item_id} names more than one row of this collection.")
    row = found[0]
    current = row_id(profile, row)
    if current != item_id:
        raise OpError(
            "STALE_ITEM",
            f"id {item_id} is stale: the row has changed and now has id {current}. "
            "Query it again and retry with the current id.",
            details={"current_id": current},
        )
    return {
        "item_key": row[column], "expected_item_version": row["item_version"],
        "expected_container_hash": snapshot,
    }
