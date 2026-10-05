"""Transaction-coupled field revisions for uniformly released query cursors.

These private counters describe declared dependencies, not policy decisions.
Mixed-release readers must establish their own authorized-value basis instead.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UniformBasis:
    identity: str
    membership_revision: int
    dependencies: tuple[tuple[str, int], ...]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _value(values, path):
    value = values
    for part in path:
        if isinstance(part, str) and isinstance(value, Mapping) and part in value:
            value = value[part]
        elif type(part) is int and isinstance(value, list | tuple) and 0 <= part < len(value):
            value = value[part]
        else:
            return _json([False, None])
    return _json([True, value])


def declare(conn, collection_id, paths) -> None:
    """Establish coverage at a governed manifest write, never during a read."""
    if not conn.in_transaction:
        raise RuntimeError("cursor basis maintenance requires a writer transaction")
    row = conn.execute("SELECT fields_json FROM query_cursor_state WHERE collection_id=?", (collection_id,)).fetchone()
    fields = {name: [list(path), 0] for name, path in paths.items()}
    if row is not None and {name: spec[0] for name, spec in json.loads(row[0]).items()} == {
        name: spec[0] for name, spec in fields.items()
    }:
        return
    # A new coverage identity invalidates old cursors without scanning old
    # values. Future writes are tracked from this transaction's snapshot.
    conn.execute("INSERT INTO query_cursor_state(collection_id,basis_id,membership_revision,fields_json) "
                 "VALUES(?,?,0,?) ON CONFLICT(collection_id) DO UPDATE SET "
                 "basis_id=excluded.basis_id,membership_revision=0,fields_json=excluded.fields_json",
                 (collection_id, str(uuid.uuid4()), _json(fields)))


def maintain(conn, collection_id, values, *, previous=None) -> None:
    """Record exact value/presence changes, including unindexed selected fields."""
    if not conn.in_transaction:
        raise RuntimeError("cursor basis maintenance requires a writer transaction")
    row = conn.execute("SELECT membership_revision,fields_json FROM query_cursor_state WHERE collection_id=?",
                       (collection_id,)).fetchone()
    if row is None:
        return  # Older declarations have no covered cursor capability.
    membership, fields = row[0], json.loads(row[1])
    changed = previous is None
    for spec in fields.values():
        if previous is None or _value(previous, spec[0]) != _value(values, spec[0]):
            spec[1] += 1
            changed = True
    if changed:
        conn.execute("UPDATE query_cursor_state SET membership_revision=?,fields_json=? WHERE collection_id=?",
                     (membership + (previous is None), _json(fields), collection_id))


def uniform_basis(conn, collection_id, dependencies) -> UniformBasis | None:
    """Read a bounded basis after the caller proves uniform field admission.

    Returning None means coverage is unavailable. This function supplies no
    authorization and must not be used for a mixed-release relation.
    """
    row = conn.execute("SELECT basis_id,membership_revision,fields_json FROM query_cursor_state WHERE collection_id=?",
                       (collection_id,)).fetchone()
    if row is None:
        return None
    fields = json.loads(row[2])
    paths = sorted(set(dependencies) - {"item_key"})
    if any(path not in fields for path in paths):
        return None
    return UniformBasis(row[0], row[1], tuple((path, fields[path][1]) for path in paths))
