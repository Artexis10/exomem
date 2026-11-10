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

from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass

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
                                                    authorize_path=operation.allows_file)
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
        if self._target.uniform:
            return True
        operation = self.session._authorization
        self.session.check()
        try:
            with closing(governance.iter_subjects(self.session.connection, self.collection_id,
                                                  operation.logical_vault_id, row_key=item_key,
                                                  include_held=False)) as subjects:
                subject = next(subjects, None)
            return subject is not None and operation.decision(subject).level >= 6
        except (ValueError, TypeError, collections.CollectionError):
            # A row whose basis cannot be built is not released, and reads like a withheld one.
            return False
