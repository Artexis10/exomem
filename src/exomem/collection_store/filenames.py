"""Filename recipes for store collections: structured-files maintenance as view moves.

A store row never moves; only its rendered view does (design.md §13, "Representation-migration
receipts"). Preview plans each row's recipe path and the links that must follow it. Apply commits
the plan in one mutation, each moved view as an audited ``view_move``, and rewrites a link only
where the store owns its text: an authored body of the same collection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .. import semantic_writes
from .. import structured_collections as collections

#: Rows one plan may cover, as for file-mode migration.
MAX_PLAN_ITEMS = 512
_MAX_BLOCKERS = 64
_REQUEST_PREFIX = b"exomem-structured-files:v1\0"


@dataclass(frozen=True, slots=True)
class Row:
    key: str
    path: str
    values: Mapping[str, Any]
    body: str


@dataclass(frozen=True, slots=True)
class Plan:
    public: dict[str, Any]
    #: Item key, current view path and recipe path, in item-key order.
    moves: tuple[tuple[str, str, str], ...]
    #: Item key to the authored body with its links to moved views rewritten.
    bodies: Mapping[str, str]
    #: Rows whose rendered relationship links name a moved view; they render again.
    rerenders: frozenset[str]


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def request_id(plan_id: str, item_key: str) -> str:
    """The request identity of one item's transition, so an apply replays by its plan."""
    return hashlib.sha256(_REQUEST_PREFIX + f"{plan_id}\0{item_key}".encode()).hexdigest()


def request_hash(collection_id: str, plan_id: str, source_snapshot: str, why: str, item_key: str) -> str:
    return canonical_hash({"action": "structured-files", "collection": collection_id, "plan_id": plan_id,
                           "source_snapshot": source_snapshot, "why": why, "item_key": item_key})


def _add_blocker(blockers: list[dict[str, str]], code: str, reason: str) -> None:
    blocker = {"code": code, "reason": reason}
    if blocker not in blockers and len(blockers) < _MAX_BLOCKERS:
        blockers.append(blocker)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _targets(
    manifest: collections.CollectionManifest, rows: list[Row], blockers: list[dict[str, str]]
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Each row's recipe path, assigned as inspection's ``filename_drift`` expects it.

    Rows whose recipe renders the same casefolded path form one group. A row already at that
    path keeps it; otherwise the lowest item key takes it. The others take the identity-suffixed
    path inspection accepts beside it, so an applied plan leaves no drift or collision behind.
    """
    targets: dict[str, str] = {}
    groups: dict[str, list[tuple[str, Row]]] = {}
    for row in rows:
        if manifest.item_filename is None:
            targets[row.key] = row.path
            continue
        try:
            base = collections.render_item_path(manifest, row.values, row.key)
        except collections.CollectionError as error:
            _add_blocker(blockers, error.code, "an item's filename recipe values cannot render a path")
            targets[row.key] = row.path
            continue
        groups.setdefault(base.casefold(), []).append((base, row))
    collisions = []
    for group in groups.values():
        exact = [row for base, row in group if row.path.casefold() == base.casefold()]
        keeper = exact[0] if len(exact) == 1 else None
        winner = keeper or group[0][1]
        occupied = {collections._portable_path_key(group[0][0])}
        for base, row in group:
            if row is winner:
                # A case-only difference is no drift, and no move.
                targets[row.key] = row.path if row.path.casefold() == base.casefold() else base
                continue
            try:
                targets[row.key] = collections.disambiguate_item_path(
                    manifest, base, row.key, occupied_path_keys=occupied)
            except collections.CollectionError as error:
                _add_blocker(blockers, error.code, "an item filename cannot be disambiguated portably")
                targets[row.key] = row.path
        if len(group) > 1:
            keys = sorted(row.key for _, row in group)
            collisions.append({"path": group[0][0], "item_keys": keys,
                               "resolved_paths": [targets[key] for key in keys]})
    return targets, sorted(collisions, key=lambda item: item["path"])


def plan(
    manifest: collections.CollectionManifest,
    rows: list[Row],
    *,
    source_snapshot: str,
    reserved_keys: set[str],
    referrers: Callable[[list[str]], Mapping[str, Iterable[str]]],
    regenerated: Callable[[str], bool],
    blockers: list[dict[str, str]],
) -> Plan:
    """One deterministic plan over ``rows``, the collection's complete row set.

    ``reserved_keys`` holds the portable keys of paths that something other than these rows
    holds: the collection's own aggregate and held views, and files already in its folders.
    ``referrers(paths)`` maps each path to every vault file that wikilinks to it, and
    ``regenerated(path)`` whether a referrer is a view this collection renders from current
    view paths (its history), so its links follow a move without a rewrite.
    """
    rows = sorted(rows, key=lambda row: row.key)
    truncated = len(rows) > MAX_PLAN_ITEMS
    if truncated:
        _add_blocker(blockers, "STRUCTURED_FILE_PLAN_LIMIT", "collection has too many items for one migration")
        rows = rows[:MAX_PLAN_ITEMS]
    targets, collisions = _targets(manifest, rows, blockers)
    moves = tuple((row.key, row.path, targets[row.key]) for row in rows if targets[row.key] != row.path)

    current = {collections._portable_path_key(row.path): row.key for row in rows}
    claimed: dict[str, str] = {}
    for key, source, target in moves:
        target_key = collections._portable_path_key(target)
        # A destination another row holds now is refused even when that row moves too: one
        # mutation stages no chain or swap of view paths.
        if (target_key in claimed or current.get(target_key, key) != key
                or target_key in reserved_keys):
            _add_blocker(blockers, "STRUCTURED_FILE_DESTINATION_EXISTS",
                         "a rendered item destination is already occupied")
        claimed[target_key] = key
        if PurePosixPath(source).parent != PurePosixPath(target).parent:
            _add_blocker(blockers, "STRUCTURED_FILE_CROSS_DIRECTORY",
                         "structured item migration cannot cross collection directories")

    by_path = {row.path: row for row in rows}
    bodies: dict[str, str] = {}
    links: dict[str, int] = {}
    rerenders: set[str] = set()
    inbound = referrers([source for _key, source, _target in moves]) if moves else {}
    for _key, source, target in moves:
        for referrer in sorted(set(inbound[source])):
            row = by_path.get(referrer)
            if row is None:
                if not regenerated(referrer):
                    _add_blocker(blockers, "IMMUTABLE_INBOUND_LINK",
                                 "an inbound link outside this collection's items cannot be rewritten "
                                 "in the collection's store transaction")
                continue
            if any(semantic_writes.rewrite_wikilinks_for_move(text, source, target)[1]
                   for text in _strings(row.values)):
                _add_blocker(blockers, "IMMUTABLE_INBOUND_LINK",
                             "an item value links to a moved item, and values are not rewritten")
            rewritten, changed = semantic_writes.rewrite_wikilinks_for_move(
                bodies.get(row.key, row.body), source, target)
            if changed:
                bodies[row.key] = rewritten
                links[row.key] = links.get(row.key, 0) + changed
            else:
                # The link is rendered from a relationship value (or recorded inside an origin
                # carrier, which keeps its bytes); rendering the row again retargets the former.
                rerenders.add(row.key)
    moved = {key for key, _source, _target in moves}
    rerenders -= moved | set(bodies)

    public_moves = [{"item_key": key, "from": source, "to": target} for key, source, target in moves]
    inbound_rewrites = [
        {"item_key": row.key, "path": row.path, "links": links[row.key],
         "before_hash": hashlib.sha256(row.body.encode()).hexdigest(),
         "after_hash": hashlib.sha256(bodies[row.key].encode()).hexdigest()}
        for row in rows if row.key in bodies
    ]
    blockers.sort(key=lambda item: (item["code"], item["reason"]))
    identity = {
        "version": 1, "collection_id": manifest.collection_id, "source_snapshot": source_snapshot,
        "moves": public_moves, "inbound_rewrites": inbound_rewrites, "rerenders": sorted(rerenders),
        "collisions": collisions, "blockers": blockers,
    }
    public = {
        "operation": "preview",
        "collection_id": manifest.collection_id,
        "manifest_path": manifest.path,
        "plan_id": canonical_hash(identity),
        "source_snapshot": source_snapshot,
        "moves": public_moves,
        # The store renders managed presentation at publication, so no plan rewrites it.
        "presentations": [],
        "inbound_rewrites": inbound_rewrites,
        "collisions": collisions,
        "blockers": blockers,
        "totals": {"moves": len(public_moves), "presentations": 0,
                   "inbound_rewrites": len(inbound_rewrites), "blockers": len(blockers)},
        "truncated": truncated or len(blockers) == _MAX_BLOCKERS,
    }
    return Plan(public, moves, bodies, frozenset(rerenders))
