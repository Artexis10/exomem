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

from ..structured_collections import declared_references
from . import reductions, reference_resolution, runtime
from .errors import QueryError

_MAX_VALUES = 20
_WARN_VALUES = 5
_UNAVAILABLE = {"COLLECTION_NOT_FOUND", "QUERY_FIELD_UNAVAILABLE", "QUERY_UNAVAILABLE"}


def _bounded(pairs: Counter) -> dict:
    entries = [{"reference": name, "value": value, "rows": rows}
               for (name, value), rows in pairs.items()]
    entries.sort(key=lambda item: (-item["rows"], item["reference"], item["value"]))
    return {"values": entries[:_MAX_VALUES],
            "truncated": len(entries) > _MAX_VALUES, "total": len(entries)}


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
        report["unresolved_values"] = _bounded(pairs)
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
            for raw in cursor:
                values = declaration.values(decode(raw))
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
                       "distinct_values": len(distinct), "unresolved_values": _bounded(pairs)})
    return report


def warnings(report) -> list[str]:
    """The plain-language warnings a write result carries for its reference report."""
    out = []
    if report is None:
        return out
    if isinstance(report, list):
        # check_collection output: one entry per declaration
        for entry in report:
            if "incomplete" in entry:
                out.append(f"REFERENCE_CHECK_INCOMPLETE: the reference check stopped "
                           f"({entry['incomplete']}); run inspect to see resolution counts.")
            elif entry.get("state") == "unavailable":
                out.append(f"REFERENCE_UNAVAILABLE: {entry['reference']} refers to a collection or "
                           "key you cannot read, so its values were not checked.")
            elif entry.get("unresolved_values", {}).get("total"):
                shown = [f"{entry['reference']} {item['value']!r}"
                         for item in entry["unresolved_values"]["values"][:_WARN_VALUES]]
                rest = entry["unresolved_values"]["total"] - min(
                    _WARN_VALUES, len(entry["unresolved_values"]["values"]))
                listed = ", ".join(shown) + (f", and {rest} more" if rest > 0 else "")
                out.append(f"REFERENCE_UNRESOLVED: {entry['unresolved_values']['total']} value(s) "
                           f"name no row you can read: {listed}. The rows are saved; each reference "
                           "reads unresolved until a matching row exists.")
        return out
    if "incomplete" in report:
        return [f"REFERENCE_CHECK_INCOMPLETE: the reference check stopped ({report['incomplete']}); "
                "run inspect to see resolution counts."]
    unresolved = report.get("unresolved_values")
    if unresolved:
        shown = [f"{item['reference']} {item['value']!r}" for item in unresolved["values"][:_WARN_VALUES]]
        rest = unresolved["total"] - min(_WARN_VALUES, len(unresolved["values"]))
        listed = ", ".join(shown) + (f", and {rest} more" if rest > 0 else "")
        out.append(f"REFERENCE_UNRESOLVED: {unresolved['total']} value(s) name no row you can read: "
                   f"{listed}. The rows are saved; each reference reads unresolved until a matching "
                   "row exists.")
    for dotted in report.get("unavailable", ()):
        out.append(f"REFERENCE_UNAVAILABLE: {dotted} refers to a collection or key you cannot read, "
                   "so its values were not checked.")
    return out
