"""The declarations that make an imported collection fast to query (OpenSpec add-collection-query-engine §12).

Import preview returns these so an agent applies them with one governed ``revise``
before ``start``. The function is pure: the compiled mapping and the target's
current manifest decide the answer, and nothing is written. Each recommendation
is spelled in the manifest's own grammar and is accepted by the same validators a
revise runs (``normalize_indexes`` and ``rollups.normalize``), so a recommendation
that fits the budgets is one the revise accepts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..query_engine.buckets import NUMERIC
from ..query_engine.indexes import IndexDeclarationError, IndexSpec, normalize_indexes
from . import rollups

#: The time basis is read and ordered by timestamp, so it gets the one index both flags declare.
TIME_FLAGS = {"filterable": True, "sortable": True}
#: What a daily total needs to answer count, sum and mean exactly.
REDUCTIONS = ("count", "sum", "avg")


def recommend(
    plan: Any,
    manifest: Any,
    data: Mapping[str, Any],
    inherited: Iterable[IndexSpec] = (),
) -> dict[str, Any]:
    """Field flags and rollups for ``plan``'s mapping, minus what the manifest declares.

    ``plan`` is a compiled import mapping and ``manifest`` the parsed target;
    ``data`` is the manifest's frontmatter (flags, indexes and rollups live there,
    not on the parsed fields) and ``inherited`` the indexes its collection type
    declares. The result mirrors the frontmatter: ``fields`` merges into
    ``item_schema.fields``, ``rollups`` into ``rollups``. ``omitted`` names each
    recommendation a budget stopped.
    """
    recommended: dict[str, Any] = {"fields": {}, "rollups": {}, "omitted": []}
    day = plan.local_date
    basis = None if day is None else rollups.basis(manifest.schema.fields, day)
    if basis is None:
        return recommended
    _index(recommended, day, data, inherited)
    _rollups(recommended, plan, manifest, basis, data)
    return recommended


def _index(
    recommended: dict[str, Any], day: str, data: Mapping[str, Any], inherited: Iterable[IndexSpec]
) -> None:
    fields = data["item_schema"]["fields"]
    declared = {
        index.name: {
            "keys": [{"field": key.field, "direction": key.direction} for key in index.keys]
        }
        for index in inherited
    } | dict(data.get("indexes") or {})
    try:
        if day in {index.keys[0].field for index in normalize_indexes(fields, declared)}:
            return
        # Run the revise's own check on the flagged schema: the index and path budgets live there.
        normalize_indexes({**fields, day: {**fields[day], **TIME_FLAGS}}, declared)
    except IndexDeclarationError as error:
        recommended["omitted"].append({"kind": "index", "field": day, "code": error.code})
        return
    recommended["fields"][day] = dict(TIME_FLAGS)


def _rollups(
    recommended: dict[str, Any], plan: Any, manifest: Any, basis: Any, data: Mapping[str, Any]
) -> None:
    declared = rollups.normalize(manifest.schema.fields, data.get("rollups"))
    served = {
        name
        for rollup in declared
        if (rollup.bucket, rollup.basis, rollup.groups) == ("day", basis, ())
        for name, reductions in rollup.values
        if set(REDUCTIONS) <= set(reductions)
    }
    wanted = [
        spec.target
        for spec in plan.fields
        if manifest.schema.fields[spec.target].type in NUMERIC
        # A natural key identifies a row; it is not a measure to total.
        and spec.target not in manifest.schema.natural_key
        and spec.target not in served
    ]
    # One rollup holds up to MAX_VALUE_FIELDS values, and the planner answers a query from a single rollup.
    chunks = [
        wanted[start : start + rollups.MAX_VALUE_FIELDS]
        for start in range(0, len(wanted), rollups.MAX_VALUE_FIELDS)
    ]
    room = rollups.MAX_ROLLUPS - len(declared)
    taken = {rollup.name for rollup in declared}
    for chunk in chunks[:room]:
        name, number = "daily", 1
        while name in taken:
            number += 1
            name = f"daily_{number}"
        taken.add(name)
        recommended["rollups"][name] = {
            "bucket": "day",
            "timestamp": plan.local_date,
            "values": {field: list(REDUCTIONS) for field in chunk},
        }
    if left_out := [field for chunk in chunks[room:] for field in chunk]:
        recommended["omitted"].append(
            {"kind": "rollup", "fields": left_out, "code": "ROLLUP_LIMIT"}
        )
