"""Resolve a declared row reference at read time, under the caller's own view of source, target and row.

``bind`` is the entry point. It checks that the caller may read the declaring field, the target
collection and the target's natural key, and returns a ``BoundReference``. ``BoundReference.resolve``
maps the field's values to the target rows the caller may read, and ``BoundReference.read`` reads
fields of such a row under the target's own field plan.

A value that names no row, a row the caller may not read and a value that cannot be a key all
resolve to ``None`` and cost the same, so a caller cannot probe which target rows exist. A caller who
cannot read the target collection gets the refusal for a collection that does not exist.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from .. import state_paths
from .. import structured_collections as collections
from ..collection_store import derived_rows, governance
from .errors import QueryError

#: Key values matched by one SQL statement, which keeps each statement's parameters few.
_CHUNK = 100
#: Distinct unresolved values a report lists before it says how many more there are.
MAX_UNRESOLVED_VALUES = 20


@dataclass(frozen=True, slots=True)
class Target:
    """One target row that a reference resolved to and the caller may read."""

    row_id: int
    item_key: str


def unresolved_values(pairs: Counter) -> dict:
    """The bounded list of distinct unresolved ``(reference, value)`` pairs, most rows first.

    ``pairs`` counts the rows holding each pair. The result also holds how many pairs there are.
    """
    entries = [{"reference": name, "value": value, "rows": rows} for (name, value), rows in pairs.items()]
    entries.sort(key=lambda item: (-item["rows"], item["reference"], item["value"]))
    return {"values": entries[:MAX_UNRESOLVED_VALUES],
            "truncated": len(entries) > MAX_UNRESOLVED_VALUES, "total": len(entries)}


def row_released(session, lookup, item_key: str) -> bool:
    """Whether the caller may read the row ``item_key`` of an admitted collection, decided as `admit` decides it.

    ``lookup`` is the collection's `LookupAdmission`. A row whose basis cannot be built is not released,
    and reads like a withheld one.
    """
    if lookup.uniform:
        return True
    operation = session._authorization
    session.check()
    try:
        with closing(governance.iter_subjects(session.connection, lookup.collection_id,
                                              operation.logical_vault_id, row_key=item_key,
                                              include_held=False)) as subjects:
            subject = next(subjects, None)
        return subject is not None and operation.decision(subject).level >= 6
    except (ValueError, TypeError, collections.CollectionError):
        return False


def rows_released(session, lookup, row_ids: Iterable[int]) -> set[int]:
    """The rows of ``row_ids`` the caller may read in an admitted collection, each decided as `row_released` decides it.

    One read serves every row, so the cost is the rows asked for, never a statement a row. Rows of the
    store whose decision inputs are equal share one decision (`OperationAuthorization.released_rows`), so a
    policy that names thousands of rows costs a lookup a row. A row whose basis cannot be built is not
    released: when one stops the shared read, the rows it left are decided one by one, so that row alone
    reads like a withheld one.
    """
    wanted = sorted(set(row_ids))
    if lookup.uniform or not wanted:
        return set(wanted)
    # Every row of the batch is decided for one reader against one host configuration, so the connector
    # snapshot that each decision reads is taken once for the batch (`state_paths.resolution_scope`).
    with state_paths.resolution_scope():
        return _rows_released(session, lookup, wanted)


def row_filter(session, lookup) -> governance.RowFilter | None:
    """Which rows of an admitted collection the caller may read, as one statement tests each row, or None.

    None leaves each row to `rows_released`: a uniform collection needs no filter, and one whose rows
    are outside the store, or that `OperationAuthorization.row_filter` cannot describe, is decided by row.
    The decisions that build a filter are charged as row visits.
    """
    if lookup.uniform or lookup.source is not derived_rows.STORE:
        return None
    session.check()
    try:
        with state_paths.resolution_scope():
            found = session._authorization.row_filter(lookup.collection_id)
    except (ValueError, TypeError, collections.CollectionError):
        return None
    if found is not None:
        session.charge_visits(found.decisions)
    return found


def _rows_released(session, lookup, wanted: list[int]) -> set[int]:
    operation = session._authorization
    released, decided = set(), set()
    session.check()
    if lookup.source is derived_rows.STORE:
        try:
            released = operation.released_rows(lookup.collection_id, wanted)
        except (ValueError, TypeError, collections.CollectionError):
            pass  # each row is decided by itself below, so a row that cannot be read alone stays withheld
        else:
            session.check()
            return released
    try:
        with closing(governance.iter_subjects(session.connection, lookup.collection_id,
                                              operation.logical_vault_id, row_ids=wanted,
                                              include_held=False)) as subjects:
            for subject in subjects:
                session.check()
                decided.add(subject.row_id)
                if operation.decision(subject).level >= 6:
                    released.add(subject.row_id)
        return released
    except (ValueError, TypeError, collections.CollectionError):
        pass
    left = [row_id for row_id in wanted if row_id not in decided]
    keys = dict(session.connection.execute(
        f"SELECT row_id, item_key FROM {lookup.source.items} WHERE row_id IN (SELECT value FROM json_each(?))",
        (json.dumps(left),)).fetchall())
    return released | {row_id for row_id in left if row_id in keys and row_released(session, lookup, keys[row_id])}


@dataclass(frozen=True, slots=True)
class ResolvedRow:
    """A row that a canonical row ref names and the caller may read."""

    lookup: object  # the collection's `LookupAdmission`
    row_id: int
    item_key: str


def resolve_row(session, ref: tuple[str, str, str]) -> ResolvedRow | None:
    """The row that ``ref`` (profile, collection id, item key, as `parse_row_ref` reads it) names, if the caller may read it.

    A missing row, a withheld row, a withheld collection, a row of another profile and a collection whose
    item keys the caller may not read all answer None and cost one visit, so the answer shows nothing
    about rows the caller may not read.
    """
    profile, collection_id, item_key = ref
    session.charge_visits(1)
    try:
        lookup = session.admit_lookup(collection_id)
    except QueryError as error:
        if error.code == "COLLECTION_NOT_FOUND":
            return None
        raise
    if (lookup.manifest.semantic_profile != profile or lookup.source is not derived_rows.STORE
            or lookup.plan.item_key_withheld):
        return None
    session.check()
    row = session.connection.execute(
        f"SELECT row_id FROM {lookup.source.items} WHERE collection_id=? AND item_key=?",
        (collection_id, item_key)).fetchone()
    if row is None or not row_released(session, lookup, item_key):
        return None
    return ResolvedRow(lookup, row[0], item_key)


def label_fields(lookup) -> tuple[str, ...]:
    """The natural-key fields that may name a row of this lookup to its caller; none when the key is withheld."""
    plan = lookup.plan
    return () if plan.natural_key_withheld else tuple(
        name for name in lookup.manifest.schema.natural_key if name in plan.whole_fields)


def label_of(values: Mapping[str, Any], fields: Sequence[str], item_key: str) -> str:
    """The label of a row from the values of its `label_fields`: those values joined, else its item key."""
    label = " / ".join(str(values[name]) for name in fields if values.get(name) not in (None, ""))
    return label or item_key


def row_label(session, row: ResolvedRow) -> str:
    """What names a resolved row to the caller: its natural-key values they may read, else its item key."""
    fields = label_fields(row.lookup)
    values = session.lookup_values(row.lookup.collection_id, row.row_id, fields) if fields else {}
    return label_of(values, fields, row.item_key)


def row_labeller(session):
    """The label of the row a canonical row ref names, or None, for a link projector reading as ``session`` reads."""
    def label(ref: tuple[str, str, str]) -> str | None:
        found = resolve_row(session, ref)
        return None if found is None else row_label(session, found)
    return label


def bind(session, source_collection_id: str, dotted_path: str) -> BoundReference:
    """Bind the reference that ``dotted_path`` declares on a source collection to ``session``.

    ``dotted_path`` is ``ReferenceDeclaration.dotted``: a field name, or ``field.member`` for a
    property of the objects in an array. Every refusal is a ``QueryError`` that holds no fact about
    the target beyond what the caller may read. Each check below runs before the next so that no
    later check can reveal something that an earlier one withholds.
    """
    source = session.admit_lookup(source_collection_id)
    declaration = next((found for found in collections.declared_references(source.manifest.schema)
                        if found.dotted == dotted_path), None)
    # An undeclared path and a withheld one read the same, so the error tells nothing about hidden fields.
    if declaration is None or not source.plan.admits_member_path(declaration.path):
        raise QueryError("QUERY_FIELD_UNAVAILABLE", "this field is not available to resolve")
    target = session.admit_lookup(declaration.collection)
    if target.plan.natural_key_withheld:
        raise QueryError("QUERY_FIELD_UNAVAILABLE", "this field is not available to resolve")
    key = target.manifest.schema.natural_key
    # A target whose key has changed since the declaration was accepted is no longer one this resolver can match.
    if (len(key) != 1 or target.manifest.schema.fields[key[0]].type not in collections.REFERENCE_KEY_TYPES
            or target.source is not derived_rows.STORE):
        raise QueryError("QUERY_UNAVAILABLE", "this reference's target can no longer be matched by key")
    return BoundReference(session, declaration, target, key[0])


class BoundReference:
    """One declared reference, ready to resolve values and read target rows for one session.

    ``manifest`` is the target manifest as the caller may see it, and ``many`` says whether the
    declared field holds several values per source row.
    """

    def __init__(self, session, declaration: collections.ReferenceDeclaration, target, key_field: str):
        self.session = session
        self.declaration = declaration
        self._target = target
        self._key_field = key_field
        self._found: dict[str, Target | None] = {}
        self._links = None

    @property
    def collection_id(self) -> str:
        return self._target.collection_id

    @property
    def basis(self):
        """The subject basis of the target's admission: manifest hash, type and declaration."""
        return self._target.basis

    @property
    def manifest(self) -> collections.CollectionManifest:
        return self._target.manifest

    @property
    def many(self) -> bool:
        return self.declaration.many

    def resolve(self, values: Iterable[str]) -> dict[str, Target | None]:
        """Map each distinct value to the target row it names, or None when no row it names is released.

        A value matches the target's natural key, exactly: case and spaces count, and text is
        compared in NFC. A canonical row reference matches that row's item key, and only when it names
        this target collection.
        """
        distinct = list(dict.fromkeys(values))
        fresh = [value for value in distinct if value not in self._found]
        if fresh:
            # One visit for each value whatever it resolves to, so the cost shows nothing about the target.
            self.session.charge_visits(len(fresh))
            self._found.update(self._lookup(fresh))
        return {value: self._found[value] for value in distinct}

    def read(self, target: Target, fields: Sequence[str]) -> dict:
        """The named fields of a resolved target row, under the target's field plan.

        A field that the caller may not read raises QUERY_FIELD_UNAVAILABLE. A link in a field is
        dropped when the caller may not read the page it names, as in any other query.
        """
        # Lazy, as in `route.run`: that module imports the whole record layer.
        from ..record_governance import _LinkProjector

        values = self.session.lookup_values(self.collection_id, target.row_id, fields)
        schema = self.manifest.schema.fields
        if any(_LinkProjector.carries_links(schema[name]) for name in values):
            if self._links is None:
                operation = self.session._authorization
                # The session's own authorization snapshot decides links, as it decides rows.
                self._links = _LinkProjector.create(self.session._root, self.manifest, policy=operation.policy,
                                                    authorize_path=operation.allows_file,
                                                    row_label=row_labeller(self.session))
            values = self._links(values)
        return values

    def _lookup(self, values: list[str]) -> dict[str, Target | None]:
        found: dict[str, Target | None] = dict.fromkeys(values)
        by_item_key: dict[str, list[str]] = {}
        by_natural_key: dict[str, list[str]] = {}
        for value in values:
            ref = collections.parse_row_ref(value)
            if ref is not None:
                # A row reference to another collection is not a key that happens to spell like one.
                if ref[:2] == (self.manifest.semantic_profile, self.collection_id):
                    by_item_key.setdefault(ref[2], []).append(value)
                continue
            try:
                serialized = collections.manifest_natural_key(self.manifest, {self._key_field: value})
            except (ValueError, collections.CollectionError):
                continue  # text that cannot be this key's type names no row
            by_natural_key.setdefault(serialized, []).append(value)
        for column, wanted in (("item_key", by_item_key), ("natural_key", by_natural_key)):
            for matched, row_id, item_key in self._rows(column, list(wanted)):
                if self._released(item_key):
                    for value in wanted[matched]:
                        found[value] = Target(row_id, item_key)
        return found

    def _rows(self, column: str, keys: list[str]):
        """The ``(matched key, row id, item key)`` of each target row whose ``column`` is one of ``keys``."""
        for start in range(0, len(keys), _CHUNK):
            chunk = keys[start:start + _CHUNK]
            self.session.check()
            # ``column`` is one of two fixed names from `_lookup`, never caller text.
            yield from self.session.connection.execute(
                f"SELECT {column}, row_id, item_key FROM {self._target.source.items} "
                f"WHERE collection_id=? AND {column} IN ({','.join('?' * len(chunk))})",
                (self.collection_id, *chunk)).fetchall()

    def _released(self, item_key: str) -> bool:
        """Whether the caller may read this target row, decided as `admit` decides it."""
        return row_released(self.session, self._target, item_key)


def bind_inverse(session, source_id: str, dotted: str, queried_id: str) -> BoundInverse:
    """Bind one inverse expansion: a source declaration that names the queried collection.

    The checks run in refusal order so that no later check reveals something an earlier
    one withholds: a missing or withheld source reads COLLECTION_NOT_FOUND exactly like a
    forward target; an undeclared, withheld or other-target declaration and a withheld
    natural key on the queried collection all read QUERY_FIELD_UNAVAILABLE.
    """
    source = session.admit_lookup(source_id)
    declaration = next((found for found in collections.declared_references(source.manifest.schema)
                        if found.dotted == dotted), None)
    if (declaration is None or declaration.collection != queried_id
            or not source.plan.admits_member_path(declaration.path)):
        raise QueryError("QUERY_FIELD_UNAVAILABLE", "this field is not available to resolve")
    if session._field_plans[queried_id].natural_key_withheld:
        raise QueryError("QUERY_FIELD_UNAVAILABLE", "this field is not available to resolve")
    return BoundInverse(session, source, declaration)


class BoundInverse:
    """One inverse expansion, ready to list the source rows that name each returned row.

    ``manifest`` and ``plan`` describe the source as the caller may see it; candidate
    source rows are filtered by the same release decision a forward target applies.
    """

    def __init__(self, session, lookup, declaration: collections.ReferenceDeclaration):
        self.session = session
        self._source = lookup
        self.declaration = declaration

    @property
    def collection_id(self) -> str:
        return self._source.collection_id

    @property
    def basis(self):
        return self._source.basis

    @property
    def manifest(self) -> collections.CollectionManifest:
        return self._source.manifest

    @property
    def dotted(self) -> str:
        return self.declaration.dotted

    def ready(self, conn) -> bool:
        """Whether the source declaration's index is built, from its state row."""
        row = conn.execute(
            "SELECT ready FROM item_reference_state WHERE collection_id=? AND field_path=?",
            (self.collection_id, self.dotted)).fetchone()
        return row is not None and bool(row[0])

    def candidates(self, conn, natural_key, item_key) -> list[tuple[int, str]]:
        """The distinct (source row id, item key) pairs whose match names this target row."""
        keys = [key for key in (natural_key, f"ref:{item_key}") if key is not None]
        if not keys:
            return []
        return [(row_id, key) for row_id, key in conn.execute(
            "SELECT DISTINCT r.row_id, i.item_key FROM item_references r"
            " JOIN items i ON i.row_id = r.row_id WHERE r.collection_id=? AND r.field_path=?"
            f" AND r.match IN ({','.join('?' * len(keys))})",
            (self.collection_id, self.dotted, *keys)).fetchall()]

    def released(self, item_key: str) -> bool:
        """Whether the caller may read this source row, decided as `admit` decides it."""
        return row_released(self.session, self._source, item_key)

    def read(self, row_id: int) -> dict:
        """The source row's released fields, under the source field plan."""
        from ..record_governance import _LinkProjector

        fields = sorted(self._source.plan.whole_fields)
        values = self.session.lookup_values(self.collection_id, row_id, fields)
        schema = self.manifest.schema.fields
        if any(name in schema and _LinkProjector.carries_links(schema[name]) for name in values):
            operation = self.session._authorization
            links = _LinkProjector.create(self.session._root, self.manifest, policy=operation.policy,
                                          authorize_path=operation.allows_file,
                                          row_label=row_labeller(self.session))
            values = links(values)
        return values
