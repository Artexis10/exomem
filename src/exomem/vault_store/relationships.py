"""Canonical relationship changes within the governed store transaction."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from sqlite3 import Connection, OperationalError

from ..cli_ops import OpError


@dataclass(frozen=True, slots=True)
class UnitIdentity:
    id: str
    parent_path: str
    anchor: str
    parent_ref: str | None = None


@dataclass(frozen=True, slots=True)
class Relationship:
    id: str
    source_unit_id: str
    relation_type: str
    target_unit_id: str | None = None
    target_page_path: str | None = None
    target_page_ref: str | None = None


@dataclass(frozen=True, slots=True)
class Retirement:
    id: str
    successor_id: str | None = None


@dataclass(frozen=True, slots=True)
class BoundRelationship:
    relationship: Relationship
    source: UnitIdentity
    target: UnitIdentity | None

    @property
    def target_path(self) -> str:
        if self.target is not None:
            return self.target.parent_path
        assert self.relationship.target_page_path is not None
        return self.relationship.target_page_path


@dataclass(frozen=True, slots=True)
class ProjectedBase:
    """The exact relations file generation from which the editor started."""

    path: str
    source: str
    revision: int | None
    keep_store: bool = False


@dataclass(frozen=True, slots=True)
class Change:
    """A plan with admitted ends, guarded by the same page checks as its bytes."""

    actor: str
    identities: tuple[UnitIdentity, ...] = ()
    added: tuple[Relationship, ...] = ()
    retired: tuple[Retirement, ...] = ()
    reason: str | None = None
    expected_outgoing: tuple[str, frozenset[str]] | None = None
    projected_base: ProjectedBase | None = None


def incident(tx: Connection, path: str) -> Iterator[BoundRelationship]:
    """Read active edges touching a page through the unit and relationship indexes."""
    try:
        rows = tx.execute(
            "WITH at_page AS (SELECT id FROM store_unit_ids WHERE parent_path = ?),"
            " edges AS ("
            " SELECT r.id FROM at_page AS unit CROSS JOIN store_relationships AS r"
            " ON r.source_unit_id = unit.id WHERE r.retired_revision IS NULL"
            " UNION"
            " SELECT r.id FROM at_page AS unit CROSS JOIN store_relationships AS r"
            " ON r.target_unit_id = unit.id WHERE r.retired_revision IS NULL"
            " UNION"
            " SELECT id FROM store_relationships WHERE target_page_path = ? AND retired_revision IS NULL"
            ") SELECT r.id,r.source_unit_id,r.relation_type,r.target_unit_id,r.target_page_path,"
            " r.target_page_ref,s.id,s.parent_path,s.anchor,s.parent_ref,t.id,t.parent_path,t.anchor,t.parent_ref"
            " FROM edges CROSS JOIN store_relationships AS r ON r.id = edges.id"
            " JOIN store_unit_ids AS s ON s.id = r.source_unit_id"
            " LEFT JOIN store_unit_ids AS t ON t.id = r.target_unit_id",
            (path, path),
        )
    except OperationalError:
        # A reader can open a store before its writer migrates that store to relationship storage.
        if tx.execute("SELECT 1 FROM sqlite_schema WHERE name = 'store_relationships'").fetchone():
            raise
        return
    for row in rows:
        yield BoundRelationship(
            Relationship(*row[:6]), UnitIdentity(*row[6:10]),
            UnitIdentity(*row[10:]) if row[10] is not None else None,
        )


def bind_additions(tx: Connection, change: Change) -> tuple[BoundRelationship, ...]:
    """Load the existing identities that a proposed addition names."""
    units = {unit.id: unit for unit in change.identities}
    wanted = sorted({
        unit for edge in change.added for unit in (edge.source_unit_id, edge.target_unit_id)
        if unit is not None and unit not in units
    })
    if wanted:
        for row in tx.execute(
            "SELECT id,parent_path,anchor,parent_ref FROM store_unit_ids"
            " WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(wanted),),
        ):
            units[row[0]] = UnitIdentity(*row)
    return tuple(BoundRelationship(
        edge, units[edge.source_unit_id],
        units[edge.target_unit_id] if edge.target_unit_id is not None else None,
    ) for edge in change.added)


def identities_at(
    tx: Connection, locations: set[tuple[str, str]],
) -> dict[tuple[str, str], UnitIdentity]:
    """Find units by their stored path and anchor, using one indexed lookup per supplied key."""
    rows = tx.execute(
        "SELECT u.id,u.parent_path,u.anchor,u.parent_ref FROM json_each(?) AS location"
        " CROSS JOIN store_unit_ids AS u ON u.parent_path = json_extract(location.value,'$[0]')"
        " AND u.anchor = json_extract(location.value,'$[1]')",
        (json.dumps(sorted(locations)),),
    )
    return {(row[1], row[2]): UnitIdentity(*row) for row in rows}


def units_on(tx: Connection, path: str) -> Iterator[UnitIdentity]:
    for row in tx.execute(
        "SELECT id,parent_path,anchor,parent_ref FROM store_unit_ids"
        " WHERE parent_path = ? ORDER BY anchor,id", (path,),
    ):
        yield UnitIdentity(*row)


def outgoing_on(tx: Connection, path: str) -> Iterator[BoundRelationship]:
    """Stream the active edges for a source page in stable anchor and relationship order."""
    yield from outgoing_for_paths(tx, (path,))


def outgoing_for_paths(tx: Connection, paths: Iterable[str]) -> Iterator[BoundRelationship]:
    """Read the active edges of a batch of sources through their unit indexes."""
    for row in tx.execute(
        "SELECT r.id,r.source_unit_id,r.relation_type,r.target_unit_id,r.target_page_path,"
        " r.target_page_ref,s.id,s.parent_path,s.anchor,s.parent_ref,t.id,t.parent_path,t.anchor,t.parent_ref"
        " FROM json_each(?) AS pages CROSS JOIN store_unit_ids AS s ON s.parent_path=pages.value"
        " CROSS JOIN store_relationships AS r ON r.source_unit_id = s.id"
        " LEFT JOIN store_unit_ids AS t ON t.id = r.target_unit_id"
        " WHERE r.retired_revision IS NULL"
        " ORDER BY s.parent_path,s.anchor,r.relation_type,r.id",
        (json.dumps(sorted(set(paths))),),
    ):
        yield BoundRelationship(
            Relationship(*row[:6]), UnitIdentity(*row[6:10]),
            UnitIdentity(*row[10:]) if row[10] is not None else None,
        )


def outgoing_unit(tx: Connection, source: UnitIdentity) -> Iterator[BoundRelationship]:
    """Seek a unit's active adjacency in (relation type, edge id) order."""
    for row in tx.execute(
        "SELECT r.id,r.source_unit_id,r.relation_type,r.target_unit_id,r.target_page_path,"
        " r.target_page_ref,t.id,t.parent_path,t.anchor,t.parent_ref"
        " FROM store_relationships r LEFT JOIN store_unit_ids t ON t.id=r.target_unit_id"
        " WHERE r.source_unit_id=? AND r.retired_revision IS NULL ORDER BY r.relation_type,r.id",
        (source.id,),
    ):
        yield BoundRelationship(
            Relationship(*row[:6]), source,
            UnitIdentity(*row[6:]) if row[6] is not None else None,
        )


def same_page_pairs(
    tx: Connection, paths: Iterable[str], types: Iterable[str],
) -> Iterator[tuple[str, str, str, str, str]]:
    """Read same-page relation endpoints shaped like (path, source ref, source anchor, target ref, target anchor)."""
    try:
        rows = tx.execute(
            "SELECT src.parent_path,src.parent_ref,src.anchor,dst.parent_ref,dst.anchor"
            " FROM json_each(?) AS pages CROSS JOIN store_unit_ids AS src ON src.parent_path=pages.value"
            " CROSS JOIN store_relationships AS r ON r.source_unit_id=src.id"
            " JOIN store_unit_ids AS dst ON dst.id=r.target_unit_id AND dst.parent_path=src.parent_path"
            " WHERE r.retired_revision IS NULL AND r.relation_type IN (SELECT value FROM json_each(?))"
            " AND src.parent_ref IS NOT NULL AND dst.parent_ref IS NOT NULL AND src.id != dst.id",
            (json.dumps(sorted(set(paths))), json.dumps(sorted(set(types)))),
        )
    except OperationalError:
        if tx.execute("SELECT 1 FROM sqlite_schema WHERE name='store_relationships'").fetchone():
            raise
        return
    yield from rows


_AT_PAGE = (
    "WITH units AS (SELECT id,anchor FROM store_unit_ids WHERE parent_path = ?1), edges AS ("
    " SELECT u.anchor,r.relation_type,r.id,r.created_revision,0 AS incoming"
    " FROM units AS u CROSS JOIN store_relationships AS r ON r.source_unit_id = u.id"
    " WHERE r.retired_revision IS NULL UNION ALL"
    " SELECT u.anchor,r.relation_type,r.id,r.created_revision,1"
    " FROM units AS u CROSS JOIN store_relationships AS r ON r.target_unit_id = u.id"
    " WHERE r.retired_revision IS NULL UNION ALL"
    " SELECT NULL,r.relation_type,r.id,r.created_revision,1"
    " FROM store_relationships AS r WHERE r.target_page_path = ?1 AND r.retired_revision IS NULL"
    ") "
)


def projection_groups(tx: Connection, path: str, limit: int) -> list[tuple[str | None, str, int, int]]:
    """Count edges by local anchor, type and direction for the owner projection."""
    return tx.execute(
        _AT_PAGE + "SELECT anchor,relation_type,incoming,COUNT(*) FROM edges"
        " GROUP BY anchor,relation_type,incoming"
        " ORDER BY COUNT(*) DESC,anchor,relation_type,incoming LIMIT ?2",
        (path, limit),
    ).fetchall()


def projection_samples(
    tx: Connection, path: str, limit: int,
) -> list[tuple[str | None, str, int, str, str | None]]:
    """Take a bounded sample across active edges whose source or target is on the page."""
    return tx.execute(
        _AT_PAGE + ", sample AS (SELECT * FROM edges ORDER BY created_revision DESC,id,incoming LIMIT ?2)"
        " SELECT sample.anchor,r.relation_type,sample.incoming,"
        " CASE sample.incoming WHEN 1 THEN s.parent_path ELSE COALESCE(t.parent_path,r.target_page_path) END,"
        " CASE sample.incoming WHEN 1 THEN s.anchor ELSE t.anchor END"
        " FROM sample CROSS JOIN store_relationships AS r ON r.id = sample.id"
        " JOIN store_unit_ids AS s ON s.id = r.source_unit_id"
        " LEFT JOIN store_unit_ids AS t ON t.id = r.target_unit_id",
        (path, limit),
    ).fetchall()


def apply(
    tx: Connection, change: Change, *, revision: int, reason: str, at: str,
) -> set[str]:
    """Store the planned changes and return the affected page paths.

    The caller owns the transaction and the page guards. A conflicting identity or
    relationship aborts the transaction rather than altering an existing record.
    """
    if (base := change.projected_base) is not None:
        stamp = tx.execute(
            "SELECT revision FROM store_manifest WHERE path = ?", (base.path,),
        ).fetchone()
        pending = tx.execute(
            "SELECT 1 FROM store_projection_queue WHERE path = ?", (base.source,),
        ).fetchone()
        recorded = stamp[0] if stamp is not None else None
        if recorded != base.revision or (pending and not base.keep_store):
            raise OpError("RELATIONS_FILE_STALE", "the stored relationships changed since projection")
    if change.expected_outgoing is not None:
        source, expected = change.expected_outgoing
        current = frozenset(row[0] for row in tx.execute(
            "SELECT id FROM store_relationships WHERE source_unit_id = ? AND retired_revision IS NULL",
            (source,),
        ))
        if current != expected:
            raise OpError("RELATIONSHIP_CHANGED", "a relationship changed before the write")
    tx.executemany(
        "INSERT INTO store_unit_ids(id,parent_path,parent_ref,anchor) VALUES (?,?,?,?)",
        ((unit.id, unit.parent_path, unit.parent_ref, unit.anchor) for unit in change.identities),
    )
    retired = tx.executemany(
        "UPDATE store_relationships SET retired_revision = ? "
        "WHERE id = ? AND retired_revision IS NULL",
        ((revision, edge.id) for edge in change.retired),
    )
    if retired.rowcount != len(change.retired):
        raise OpError("RELATIONSHIP_CHANGED", "a relationship changed before the write")
    tx.executemany(
        "INSERT INTO store_relationships("
        "id,source_unit_id,relation_type,target_unit_id,target_page_path,target_page_ref,created_revision"
        ") VALUES (?,?,?,?,?,?,?)",
        ((edge.id, edge.source_unit_id, edge.relation_type, edge.target_unit_id,
          edge.target_page_path, edge.target_page_ref, revision) for edge in change.added),
    )
    tx.executemany(
        "INSERT INTO store_relationship_history(relationship_id,revision,action,actor,reason,at) "
        "VALUES (?,?,'add',?,?,?)",
        ((edge.id, revision, change.actor, reason, at) for edge in change.added),
    )
    tx.executemany(
        "INSERT INTO store_relationship_history("
        "relationship_id,revision,action,actor,reason,at,successor_id) "
        "VALUES (?,?,?,?,?,?,?)",
        ((edge.id, revision, "supersede" if edge.successor_id else "retire", change.actor,
          reason, at, edge.successor_id) for edge in change.retired),
    )
    ids = [edge.id for edge in (*change.added, *change.retired)]
    rows = tx.execute(
        "SELECT src.parent_path, COALESCE(dst.parent_path,r.target_page_path) "
        "FROM json_each(?) AS wanted CROSS JOIN store_relationships AS r ON r.id = wanted.value "
        "JOIN store_unit_ids AS src ON src.id = r.source_unit_id "
        "LEFT JOIN store_unit_ids AS dst ON dst.id = r.target_unit_id",
        (json.dumps(ids),),
    )
    affected = {path for row in rows for path in row}
    if base is not None:
        affected.add(base.source)
    return affected


def move_page(tx: Connection, old: str, new: str) -> None:
    """Move the identity locators without changing the IDs of either their units or edges."""
    tx.execute("UPDATE store_unit_ids SET parent_path = ? WHERE parent_path = ?", (new, old))
    tx.execute(
        "UPDATE store_relationships SET target_page_path = ? "
        "WHERE target_page_path = ? AND retired_revision IS NULL",
        (new, old),
    )


def reproject_moved(tx: Connection, paths: tuple[str, ...], *, revision: int, at: str) -> None:
    """A move's derivation intent refreshes the source pages that name its far ends."""
    sources = (
        "WITH moved AS (SELECT value AS path FROM json_each(?1)), active AS ("
        " SELECT r.source_unit_id FROM moved CROSS JOIN store_unit_ids t ON t.parent_path=moved.path"
        " CROSS JOIN store_relationships r ON r.target_unit_id=t.id WHERE r.retired_revision IS NULL"
        " UNION SELECT r.source_unit_id FROM moved"
        " CROSS JOIN store_relationships r ON r.target_page_path=moved.path WHERE r.retired_revision IS NULL"
        "), affected AS (SELECT DISTINCT u.parent_path AS path FROM active"
        " CROSS JOIN store_unit_ids u ON u.id=active.source_unit_id"
        " JOIN store_pages p ON p.path=u.parent_path WHERE u.parent_path NOT IN (SELECT path FROM moved)) "
    )
    params = (json.dumps(paths), revision, at)
    tx.execute(
        sources + "INSERT INTO store_projection_queue(path,revision,operation,queued_at)"
        " SELECT path,?2,'write',?3 FROM affected WHERE 1 ON CONFLICT(path) DO NOTHING", params,
    )
    tx.execute(
        sources + "INSERT INTO store_derivation_queue(path,revision,operation,queued_at)"
        " SELECT path,?2,'derive',?3 FROM affected WHERE 1 ON CONFLICT(path) DO NOTHING", params,
    )
