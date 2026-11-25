"""Reference resolution reports for writes, imports and inspection.

A write commits whether or not its references resolve — resolution happens at
read time. These helpers count what did not resolve under the writer's or
inspector's own view so the result can say so plainly, and they never fail the
underlying operation: a session-level refusal becomes an ``incomplete`` report
and a declaration the caller cannot check reads ``unavailable``, never
"unresolved".
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing

from ..collection_store import reference_index, typed_storage
from ..structured_collections import declared_links, declared_references, indexed_fields
from . import reductions, reference_resolution, runtime
from .errors import QueryError
from .reference_resolution import unresolved_values

_WARN_VALUES = 5
_UNAVAILABLE = {"COLLECTION_NOT_FOUND", "QUERY_FIELD_UNAVAILABLE", "QUERY_UNAVAILABLE"}


def _check(session, collection_id, rows) -> dict | None:
    """Resolve every declaration the caller can check and count unresolved (reference, value) pairs."""
    declarations = declared_references(session.admit_lookup(collection_id).manifest.schema)
    pairs: Counter = Counter()
    unavailable = []
    for declaration in declarations:
        try:
            bound = reference_resolution.bind(session, collection_id, declaration.dotted)
        except QueryError as error:
            if error.code in _UNAVAILABLE:
                unavailable.append(declaration.dotted)
                continue
            raise
        resolved = bound.resolve([value for row in rows for value in declaration.values(row)])
        for row in rows:
            pairs.update((declaration.dotted, value) for value in declaration.values(row)
                         if resolved.get(value) is None)
    if not pairs and not unavailable:
        return None
    report = {}
    if pairs:
        report["unresolved_values"] = unresolved_values(pairs)
    if unavailable:
        report["unavailable"] = unavailable
    return report


def check_values(root, store_path, collection_id, rows) -> dict | None:
    """The reference report of one committed write: unresolved values and unavailable declarations."""
    try:
        with runtime.read_session(root, store_path) as session:
            return _check(session, collection_id, rows)
    except QueryError as error:
        return {"incomplete": error.code}


def check_collection(root, store_path, collection_id) -> list:
    """Resolution counts for every declaration of a collection, under the caller's view.

    A session-level refusal (busy readers, the deadline, the visit bound) reads as one ``incomplete``
    entry, so the inspect or import status that asked still answers.
    """
    try:
        with runtime.read_session(root, store_path) as session:
            return _check_collection(session, collection_id)
    except QueryError as error:
        return [{"incomplete": error.code}]


def _check_collection(session, collection_id) -> list:
    report = []
    for declaration in declared_references(session.admit_lookup(collection_id).manifest.schema):
        try:
            bound = reference_resolution.bind(session, collection_id, declaration.dotted)
        except QueryError as error:
            if error.code not in _UNAVAILABLE:
                raise
            report.append({"reference": declaration.dotted, "state": "unavailable"})
            continue
        admitted = session.admit(collection_id)
        sql, decode = reductions._scan_sql(
            session, collection_id, admitted.layout, admitted.membership_sql, (declaration.path[0],))
        # Rows holding the same values collapse, so one scan keeps only the distinct value sets.
        held: Counter = Counter()
        with closing(session.connection.execute(sql, (collection_id,))) as cursor:
            for visited, raw in enumerate(cursor, 1):
                if not visited % reductions._CHECK_EVERY:
                    session.check()
                try:
                    values = declaration.values(decode(raw))
                except (typed_storage.TypedStorageError, ValueError, TypeError) as error:
                    raise QueryError("QUERY_UNAVAILABLE") from error
                if values:
                    held[values] += 1
        distinct = {value for values in held for value in values}
        resolved = bound.resolve(sorted(distinct))
        pairs: Counter = Counter()
        unresolved_rows = 0
        for values, rows in held.items():
            failed = [value for value in values if resolved[value] is None]
            pairs.update({(declaration.dotted, value): rows for value in failed})
            unresolved_rows += rows if failed else 0
        with_values = sum(held.values())
        report.append({"reference": declaration.dotted, "collection": bound.collection_id,
                       "state": "available", "rows_with_values": with_values,
                       "resolved_rows": with_values - unresolved_rows, "unresolved_rows": unresolved_rows,
                       "distinct_values": len(distinct), "unresolved_values": unresolved_values(pairs)})
    return report


def check_links(root, store_path, collection_id) -> list:
    """Per link field: the values the citation index holds and the values it does not, under the caller's view.

    A title or a path is not indexed (`reference_index.link_match`), so a field's ``not_indexed`` count
    says how many of its values no inbound-citation read can find. A built field's counts come from the
    index in one statement, when every row releases alike for the caller (`reference_index.link_counts`).
    Otherwise the report reads the field's visible values. ``indexed`` is None while the index is
    warming, because the partly built index is not the count of what it will hold. A field the index
    never holds, such as one of a derived collection, reads ``not_indexed`` and counts every value there.
    """
    try:
        with runtime.read_session(root, store_path) as session:
            return _check_links(session, collection_id)
    except QueryError as error:
        return [{"incomplete": error.code}]


def _check_links(session, collection_id) -> list:
    lookup = session.admit_lookup(collection_id, visit_rows=False)
    manifest = lookup.manifest
    held = {declaration.dotted for declaration in indexed_fields(manifest.schema, derived=manifest.derived)}
    declarations = declared_links(manifest.schema)
    ready = {field for field, built in session.connection.execute(
        "SELECT field_path, ready FROM item_reference_state WHERE collection_id=?", (collection_id,)) if built}
    counted = {declaration.dotted for declaration in declarations if lookup.uniform
               and declaration.dotted in held & ready and lookup.plan.admits_member_path(declaration.path)}
    counts = reference_index.link_counts(session.connection, collection_id, counted) if counted else {}
    report = []
    for declaration in declarations:
        entry = {"link": declaration.dotted,
                 **({"relation": declaration.relation} if declaration.relation is not None else {})}
        if declaration.dotted in counted:
            indexed, not_indexed = counts.get(declaration.dotted, (0, 0))
            report.append({**entry, "state": "ready", "indexed": indexed, "not_indexed": not_indexed})
            continue
        scanned = _scan_links(session, collection_id, declaration, indexed=declaration.dotted in held)
        if scanned is None:
            report.append({**entry, "state": "unavailable"})
        elif declaration.dotted not in held:
            report.append({**entry, "state": "not_indexed", "indexed": 0, "not_indexed": scanned[1]})
        else:
            built = declaration.dotted in ready
            report.append({**entry, "state": "ready" if built else "warming",
                           "indexed": scanned[0] if built else None, "not_indexed": scanned[1]})
    return report


def _scan_links(session, collection_id, declaration, *, indexed: bool) -> tuple[int, int] | None:
    """``(indexed, not_indexed)`` of one link field over the rows the caller may read, or None when it is withheld."""
    admitted = session.admit(collection_id)
    try:
        sql, decode = reductions._scan_sql(
            session, collection_id, admitted.layout, admitted.membership_sql, (declaration.path[0],))
    except QueryError as error:
        if error.code not in _UNAVAILABLE:
            raise
        return None
    found = missed = 0
    with closing(session.connection.execute(sql, (collection_id,))) as cursor:
        for visited, raw in enumerate(cursor, 1):
            if not visited % reductions._CHECK_EVERY:
                session.check()
            try:
                values = declaration.values(decode(raw))
            except (typed_storage.TypedStorageError, ValueError, TypeError) as error:
                raise QueryError("QUERY_UNAVAILABLE") from error
            for value in values:
                if indexed and reference_index.link_match(value) is not None:
                    found += 1
                else:
                    missed += 1
    return found, missed


def link_warnings(report) -> list[str]:
    """The warnings of a ``check_links`` report: a field holding values that no citation read can find."""
    out = []
    for entry in report:
        if "incomplete" in entry:
            out.append(_incomplete(entry["incomplete"]))
        elif entry.get("not_indexed"):
            out.append(f"LINK_UNRESOLVED: link field {entry['link']!r} holds {entry['not_indexed']} values "
                       "that are not page or row refs, so no page lists them as citations.")
    return out


def _incomplete(code: str) -> str:
    return f"REFERENCE_CHECK_INCOMPLETE: the reference check stopped ({code}); run inspect to see resolution counts."


def _unavailable(dotted: str) -> str:
    return f"REFERENCE_UNAVAILABLE: {dotted} is unavailable to you, so its values were not checked."


def _unresolved(unresolved: dict, *, whole_collection: bool) -> str:
    shown = [f"{item['reference']} {item['value']!r}" for item in unresolved["values"][:_WARN_VALUES]]
    rest = unresolved["total"] - len(shown)
    listed = ", ".join(shown) + (f", and {rest} more" if rest > 0 else "")
    if whole_collection:
        # A collection report counts every row, so it cannot say which of them an import wrote.
        return (f"REFERENCE_UNRESOLVED: the collection has {unresolved['total']} distinct value(s) that name "
                f"no row you can read: {listed}. Each reads unresolved until a matching row exists.")
    return (f"REFERENCE_UNRESOLVED: {unresolved['total']} value(s) name no row you can read: {listed}. "
            "The rows are saved; each reference reads unresolved until a matching row exists.")


def warnings(report) -> list[str]:
    """The plain-language warnings a write result carries for its reference report.

    ``report`` is one ``check_values`` result or the ``check_collection`` list of per-declaration entries.
    """
    if report is None:
        return []
    out = []
    whole_collection = isinstance(report, list)
    for entry in report if whole_collection else [report]:
        if "incomplete" in entry:
            out.append(_incomplete(entry["incomplete"]))
        if entry.get("state") == "unavailable":
            out.append(_unavailable(entry["reference"]))
        out.extend(_unavailable(dotted) for dotted in entry.get("unavailable", ()))
        if entry.get("unresolved_values", {}).get("total"):
            out.append(_unresolved(entry["unresolved_values"], whole_collection=whole_collection))
    return out
