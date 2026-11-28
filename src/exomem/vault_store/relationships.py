"""Canonical relationship changes within the governed store transaction."""

from __future__ import annotations

import json
from dataclasses import dataclass
from sqlite3 import Connection

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
class Change:
    """A plan with admitted ends, guarded by the same page checks as its bytes."""

    actor: str
    identities: tuple[UnitIdentity, ...] = ()
    added: tuple[Relationship, ...] = ()
    retired: tuple[Retirement, ...] = ()


def apply(
    tx: Connection, change: Change, *, revision: int, reason: str, at: str,
) -> set[str]:
    """Store the planned changes and return the affected page paths.

    The caller owns the transaction and the page guards. A conflicting identity or
    relationship aborts the transaction rather than altering an existing record.
    """
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
    return {path for row in rows for path in row}


def move_page(tx: Connection, old: str, new: str) -> None:
    """Move the identity locators without changing the IDs of either their units or edges."""
    tx.execute("UPDATE store_unit_ids SET parent_path = ? WHERE parent_path = ?", (new, old))
    tx.execute(
        "UPDATE store_relationships SET target_page_path = ? "
        "WHERE target_page_path = ? AND retired_revision IS NULL",
        (new, old),
    )
