"""Canonical relationship changes within the governed store transaction."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from sqlite3 import Connection, OperationalError

from .. import memory_refs
from ..cli_ops import OpError

UnitLocator = tuple[str, str, str | None]


@dataclass(frozen=True, slots=True)
class UnitIdentity:
    id: str
    parent_path: str
    anchor: str
    parent_ref: str | None = None

    @property
    def locator(self) -> UnitLocator:
        return self.parent_path, self.anchor, self.parent_ref


@dataclass(frozen=True, slots=True)
class Relationship:
    id: str
    source_unit_id: str
    relation_type: str
    target_unit_id: str | None = None
    target_page_path: str | None = None
    target_page_ref: str | None = None

    @property
    def key(self) -> RelationshipKey:
        return self.relation_type, self.target_unit_id, self.target_page_path, self.target_page_ref


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


RelationshipKey = tuple[str, str | None, str | None, str | None]
ALREADY_SUPERSEDED = "SUPERSEDE_TARGET_ALREADY_SUPERSEDED"


@dataclass(frozen=True, slots=True)
class SupersessionCheck:
    """The predecessor and its admitted, current same-page anchors."""

    parent_path: str
    parent_ref: str
    anchor: str
    source_anchors: tuple[str, ...]
    relation_types: tuple[str, ...]


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
    supersession_check: SupersessionCheck | None = None
    expected_retirement: tuple[str, tuple[RelationshipKey, ...], int] | None = None


def incident_window(
    tx: Connection, path: str, *, after: str, limit: int,
) -> Iterator[BoundRelationship]:
    """Seek a bounded window of distinct active edges touching the page."""
    try:
        rows = tx.execute(
            "WITH at_page AS (SELECT id FROM store_unit_ids WHERE parent_path = ?1),"
            " edges AS ("
            " SELECT r.id FROM at_page AS unit CROSS JOIN store_relationships AS r"
            " ON r.source_unit_id = unit.id WHERE r.retired_revision IS NULL AND r.id > ?2"
            " UNION"
            " SELECT r.id FROM at_page AS unit CROSS JOIN store_relationships AS r"
            " ON r.target_unit_id = unit.id WHERE r.retired_revision IS NULL AND r.id > ?2"
            " UNION"
            " SELECT id FROM store_relationships"
            " WHERE target_page_path = ?1 AND retired_revision IS NULL AND id > ?2"
            "), edge_window AS (SELECT id FROM edges ORDER BY id LIMIT ?3)"
            " SELECT r.id,r.source_unit_id,r.relation_type,r.target_unit_id,r.target_page_path,"
            " r.target_page_ref,s.id,s.parent_path,s.anchor,s.parent_ref,t.id,t.parent_path,t.anchor,t.parent_ref"
            " FROM edge_window CROSS JOIN store_relationships AS r ON r.id = edge_window.id"
            " JOIN store_unit_ids AS s ON s.id = r.source_unit_id"
            " LEFT JOIN store_unit_ids AS t ON t.id = r.target_unit_id ORDER BY r.id",
            (path, after, limit),
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
    tx: Connection, locations: set[UnitLocator],
) -> dict[UnitLocator, UnitIdentity]:
    """Find the units of these page identities even if the path has an older orphan."""
    rows = tx.execute(
        "SELECT u.id,u.parent_path,u.anchor,u.parent_ref FROM json_each(?) AS location"
        " CROSS JOIN store_unit_ids AS u ON u.parent_path = json_extract(location.value,'$[0]')"
        " AND u.anchor = json_extract(location.value,'$[1]')"
        " AND COALESCE(u.parent_ref,'') = COALESCE(json_extract(location.value,'$[2]'),'')",
        (json.dumps(sorted(locations, key=repr)),),
    )
    units = (UnitIdentity(*row) for row in rows)
    return {unit.locator: unit for unit in units}


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


def outgoing_unit(
    tx: Connection, source: UnitIdentity, *,
    after: tuple[str, str] | None = None, through: tuple[str, str] | None = None,
    limit: int | None = None,
) -> Iterator[BoundRelationship]:
    """Seek a unit's active adjacency in (relation type, edge id) order."""
    predicates = ["r.source_unit_id=?", "r.retired_revision IS NULL"]
    values: list[str | int] = [source.id]
    if after is not None:
        predicates.append("(r.relation_type,r.id) > (?,?)")
        values.extend(after)
    if through is not None:
        predicates.append("(r.relation_type,r.id) <= (?,?)")
        values.extend(through)
    suffix = ""
    if limit is not None:
        suffix = " LIMIT ?"
        values.append(limit)
    for row in tx.execute(
        "SELECT r.id,r.source_unit_id,r.relation_type,r.target_unit_id,r.target_page_path,"
        " r.target_page_ref,t.id,t.parent_path,t.anchor,t.parent_ref"
        " FROM store_relationships r LEFT JOIN store_unit_ids t ON t.id=r.target_unit_id"
        " WHERE " + " AND ".join(predicates) + " ORDER BY r.relation_type,r.id" + suffix,
        values,
    ):
        yield BoundRelationship(
            Relationship(*row[:6]), source,
            UnitIdentity(*row[6:]) if row[6] is not None else None,
        )


def outgoing_unit_batch(
    tx: Connection, sources: dict[str, UnitIdentity], *,
    after: tuple[str, str, str] | None, limit: int,
) -> Iterator[BoundRelationship]:
    """Seek one window of a page's active outgoing edges across its live source identities."""
    cursor = "" if after is None else " AND (r.source_unit_id,r.relation_type,r.id) > (?,?,?)"
    active_ids = [unit_id for unit_id in sources if after is None or unit_id >= after[0]]
    for row in tx.execute(
        "SELECT r.id,r.source_unit_id,r.relation_type,r.target_unit_id,r.target_page_path,"
        " r.target_page_ref,t.id,t.parent_path,t.anchor,t.parent_ref"
        " FROM store_relationships r LEFT JOIN store_unit_ids t ON t.id=r.target_unit_id"
        " WHERE r.source_unit_id IN (SELECT value FROM json_each(?)) AND r.retired_revision IS NULL"
        + cursor + " ORDER BY r.source_unit_id,r.relation_type,r.id LIMIT ?",
        (json.dumps(active_ids), *(after or ()), limit),
    ):
        yield BoundRelationship(
            Relationship(*row[:6]), sources[row[1]],
            UnitIdentity(*row[6:]) if row[6] is not None else None,
        )


def last_retirement(tx: Connection, source_id: str, keys: tuple[RelationshipKey, ...]) -> int:
    """Find the newest retirement among the operation's visible relationship keys."""
    if not keys:
        return 0
    (revision,) = tx.execute(
        "SELECT COALESCE(MAX((SELECT retired_revision FROM store_relationships AS r"
        " WHERE r.source_unit_id=?1 AND r.relation_type=json_extract(wanted.value,'$[0]')"
        " AND r.target_unit_id IS json_extract(wanted.value,'$[1]')"
        " AND r.target_page_path IS json_extract(wanted.value,'$[2]')"
        " AND r.target_page_ref IS json_extract(wanted.value,'$[3]')"
        " AND r.retired_revision IS NOT NULL ORDER BY r.retired_revision DESC LIMIT 1)),0)"
        " FROM json_each(?2) AS wanted",
        (source_id, json.dumps(keys)),
    ).fetchone()
    return int(revision)


def active_keys(tx: Connection, source_id: str, keys: set[RelationshipKey]) -> set[RelationshipKey]:
    """Probe just the requested active keys through their partial unique indexes."""
    if not keys:
        return set()
    return set(tx.execute(
        "WITH wanted(kind,unit,path,ref) AS ("
        " SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),"
        " json_extract(value,'$[2]'),json_extract(value,'$[3]') FROM json_each(?2))"
        " SELECT kind,unit,path,ref FROM wanted WHERE CASE"
        " WHEN unit IS NOT NULL THEN EXISTS (SELECT 1 FROM store_relationships r"
        " WHERE r.source_unit_id=?1 AND r.relation_type=kind AND r.target_unit_id=unit"
        " AND r.retired_revision IS NULL)"
        " WHEN ref IS NOT NULL THEN EXISTS (SELECT 1 FROM store_relationships r"
        " WHERE r.source_unit_id=?1 AND r.relation_type=kind AND r.target_page_ref=ref"
        " AND r.target_page_path=path AND r.retired_revision IS NULL)"
        " ELSE EXISTS (SELECT 1 FROM store_relationships r"
        " WHERE r.source_unit_id=?1 AND r.relation_type=kind AND r.target_page_path=path"
        " AND r.target_page_ref IS NULL AND r.retired_revision IS NULL) END",
        (source_id, json.dumps(tuple(keys))),
    ))


def _require_new_successor(tx: Connection, check: SupersessionCheck) -> None:
    already = tx.execute(
        "SELECT successor.anchor FROM store_unit_ids AS prior"
        " CROSS JOIN store_relationships AS r ON r.target_unit_id=prior.id"
        " JOIN store_unit_ids AS successor ON successor.id=r.source_unit_id"
        " WHERE prior.parent_path=?1 AND prior.parent_ref=?2 AND prior.anchor=?3"
        " AND successor.parent_path=?1 AND successor.parent_ref=?2 AND successor.id<>prior.id"
        " AND successor.anchor IN (SELECT value FROM json_each(?4))"
        " AND r.retired_revision IS NULL AND r.relation_type IN (SELECT value FROM json_each(?5)) LIMIT 1",
        (check.parent_path, check.parent_ref, check.anchor, json.dumps(check.source_anchors),
         json.dumps(check.relation_types)),
    ).fetchone()
    if already is not None:
        raise OpError(
            ALREADY_SUPERSEDED, "the claim already has a successor", "Read the page's current units.",
            details={"successor_anchor": already[0]},
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


def orphaned_units(tx: Connection) -> Iterator[UnitIdentity]:
    """Read missing active endpoints from current parser rows, without rebinding locators."""
    bound = tx.execute(
        "WITH held AS (SELECT DISTINCT parent_path FROM store_unit_ids),"
        " present AS (SELECT s.parent_path,s.parent_ref,s.anchor FROM held"
        " CROSS JOIN semantic_units s ON s.parent_path=held.parent_path"
        " JOIN store_pages p ON p.path=s.parent_path AND p.sha256=s.parent_source_hash"
        " WHERE s.anchor IS NOT NULL GROUP BY s.parent_path,s.parent_ref,s.anchor HAVING COUNT(*)=1)"
        " SELECT u.id,u.parent_path,u.anchor,u.parent_ref FROM store_unit_ids u"
        " LEFT JOIN present ON present.parent_path=u.parent_path"
        " AND present.anchor=u.anchor AND (u.parent_ref IS NULL OR present.parent_ref=u.parent_ref)"
        " WHERE present.parent_path IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM store_derivation_queue q WHERE q.path=u.parent_path)"
        " AND (EXISTS (SELECT 1 FROM store_relationships r WHERE r.source_unit_id=u.id AND r.retired_revision IS NULL)"
        "  OR EXISTS (SELECT 1 FROM store_relationships r WHERE r.target_unit_id=u.id AND r.retired_revision IS NULL))"
        " ORDER BY u.parent_path,u.anchor,u.id",
    )
    for row in bound:
        yield UnitIdentity(*row)


def orphaned_pages(tx: Connection) -> Iterator[tuple[str, str | None]]:
    """Yield each absent or replaced page that an active relationship still names."""
    rows = tx.execute(
        "WITH targets AS (SELECT DISTINCT target_page_path AS path,target_page_ref AS ref"
        " FROM store_relationships WHERE retired_revision IS NULL AND target_page_path IS NOT NULL)"
        " SELECT targets.path,targets.ref,p.path,p.frontmatter_json FROM targets"
        " LEFT JOIN store_pages p ON p.path=targets.path ORDER BY targets.path,targets.ref",
    )
    for path, reference, live_path, fields in rows:
        if live_path is None or (reference is not None and page_reference(fields) != reference):
            yield path, reference


def relanding_sources(
    tx: Connection, path: str, *, after: str, limit: int,
) -> Iterator[tuple[str]]:
    """Seek the sources whose stored unit targets can land on this page."""
    yield from tx.execute(
        "SELECT DISTINCT s.parent_path FROM store_unit_ids t"
        " CROSS JOIN store_relationships r ON r.target_unit_id=t.id"
        " JOIN store_unit_ids s ON s.id=r.source_unit_id"
        " WHERE t.parent_path=? AND r.retired_revision IS NULL AND s.parent_path>?"
        " ORDER BY s.parent_path LIMIT ?",
        (path, after, limit),
    )


_AT_PAGE = (
    "WITH units AS (SELECT id,anchor FROM store_unit_ids WHERE parent_path=?1 AND parent_ref IS ?3),"
    " edges AS ("
    " SELECT u.anchor,r.relation_type,r.id,r.created_revision,0 AS incoming"
    " FROM units AS u CROSS JOIN store_relationships AS r ON r.source_unit_id = u.id"
    " WHERE r.retired_revision IS NULL UNION ALL"
    " SELECT u.anchor,r.relation_type,r.id,r.created_revision,1"
    " FROM units AS u CROSS JOIN store_relationships AS r ON r.target_unit_id = u.id"
    " WHERE r.retired_revision IS NULL UNION ALL"
    " SELECT NULL,r.relation_type,r.id,r.created_revision,1"
    " FROM store_relationships AS r WHERE r.target_page_path=?1 AND r.retired_revision IS NULL"
    " AND (r.target_page_ref IS NULL OR r.target_page_ref=?3)"
    ") "
)


def projection_groups(
    tx: Connection, path: str, parent_ref: str | None, limit: int,
) -> list[tuple[str | None, str, int, int]]:
    """Count edges by local anchor, type and direction for the owner projection."""
    return tx.execute(
        _AT_PAGE + "SELECT anchor,relation_type,incoming,COUNT(*) FROM edges"
        " GROUP BY anchor,relation_type,incoming"
        " ORDER BY COUNT(*) DESC,anchor,relation_type,incoming LIMIT ?2",
        (path, limit, parent_ref),
    ).fetchall()


def projection_samples(
    tx: Connection, path: str, parent_ref: str | None, limit: int,
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
        (path, limit, parent_ref),
    ).fetchall()


def apply(
    tx: Connection, change: Change, *, revision: int, reason: str, at: str,
) -> set[str]:
    """Store the planned changes and return the affected page paths.

    The caller owns the transaction and the page guards. A conflicting identity or
    relationship aborts the transaction rather than altering an existing record.
    """
    if change.supersession_check is not None:
        _require_new_successor(tx, change.supersession_check)
    if change.expected_retirement is not None:
        source_id, keys, revision_read = change.expected_retirement
        if last_retirement(tx, source_id, keys) != revision_read:
            raise OpError("RELATIONSHIP_CHANGED", "a relationship changed before the write")
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
        (current,) = tx.execute(
            "SELECT (SELECT COUNT(*) FROM store_relationships"
            " WHERE source_unit_id=?1 AND retired_revision IS NULL)=?2"
            " AND NOT EXISTS (SELECT 1 FROM json_each(?3) AS expected"
            " LEFT JOIN store_relationships r ON r.id=expected.value"
            " AND r.source_unit_id=?1 AND r.retired_revision IS NULL WHERE r.id IS NULL)",
            (source, len(expected), json.dumps(tuple(expected))),
        ).fetchone()
        if not current:
            raise OpError("RELATIONSHIP_CHANGED", "a relationship changed before the write")
    if change.identities:
        tx.execute(
            "INSERT INTO store_unit_ids(id,parent_path,parent_ref,anchor)"
            " SELECT value->>'id',value->>'parent_path',value->>'parent_ref',value->>'anchor'"
            " FROM json_each(?)",
            (json.dumps([asdict(unit) for unit in change.identities]),),
        )
    retirements = json.dumps([asdict(edge) for edge in change.retired])
    if change.retired:
        retired = tx.execute(
            "UPDATE store_relationships SET retired_revision=?1 WHERE retired_revision IS NULL "
            "AND id IN (SELECT value->>'id' FROM json_each(?2))",
            (revision, retirements),
        )
        if retired.rowcount != len(change.retired):
            raise OpError("RELATIONSHIP_CHANGED", "a relationship changed before the write")
    if change.added:
        additions = json.dumps([asdict(edge) for edge in change.added])
        tx.execute(
            "INSERT INTO store_relationships("
            "id,source_unit_id,relation_type,target_unit_id,target_page_path,target_page_ref,created_revision)"
            " SELECT value->>'id',value->>'source_unit_id',value->>'relation_type',value->>'target_unit_id',"
            " value->>'target_page_path',value->>'target_page_ref',?1 FROM json_each(?2)",
            (revision, additions),
        )
        tx.execute(
            "INSERT INTO store_relationship_history(relationship_id,revision,action,actor,reason,at)"
            " SELECT value->>'id',?1,'add',?2,?3,?4 FROM json_each(?5)",
            (revision, change.actor, reason, at, additions),
        )
    if change.retired:
        tx.execute(
            "INSERT INTO store_relationship_history("
            "relationship_id,revision,action,actor,reason,at,successor_id)"
            " SELECT value->>'id',?1,CASE WHEN value->>'successor_id' IS NULL THEN 'retire' ELSE 'supersede' END,"
            " ?2,?3,?4,value->>'successor_id' FROM json_each(?5)",
            (revision, change.actor, reason, at, retirements),
        )
    ids = [edge.id for edge in change.added] + [edge.id for edge in change.retired]
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


def page_reference(frontmatter_json: str | None) -> str | None:
    fields = json.loads(frontmatter_json) if frontmatter_json else {}
    identity = memory_refs.normalize_id(fields.get(memory_refs.ID_FIELD))
    return memory_refs.memory_ref(identity) if identity is not None else None


def move_page(tx: Connection, old: str, new: str, *, parent_ref: str | None) -> None:
    """Move the identity locators without changing the IDs of either their units or edges."""
    tx.execute(
        "UPDATE store_unit_ids SET parent_path = ? WHERE parent_path = ? AND parent_ref IS ?",
        (new, old, parent_ref),
    )
    tx.execute(
        "UPDATE store_relationships SET target_page_path = ? "
        "WHERE target_page_path = ? AND (target_page_ref IS NULL OR target_page_ref = ?)"
        " AND retired_revision IS NULL",
        (new, old, parent_ref),
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
