"""The items that one write can change under a validator that follows references, read by index.

A named validator that declares references (`types.Reference`) judges a write only through them.
The writer then passes it the write's neighbourhood instead of every item: the written item, the
items whose reference fields name it, and every item that those name in turn. Each rule then reads
the same items that it reads in the whole collection, and a rule that the write cannot change is not
judged again.

The items that name one item come from range reads on the collection's ready query projection,
through an index that leads with the reference field (`index_migrations._declared` asks for one).
Without a ready projection or such an index, `neighbourhood` returns None and the writer reads every
item.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from ..query_engine import ir, typed_sql
from ..query_engine.scalars import ScalarValueError
from . import index_migrations, native_identity, typed_storage
from .native_projections import NativeProjectionPlan
from .types import Reference


def neighbourhood(conn, collection_id: str, references: Sequence[Reference], key: str,
                  values: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    """The written item with its new values and each stored item a write to it can change, or None."""
    plan = index_migrations.ready_plan(conn, collection_id)
    if plan is None:
        return None
    candidates: set[str] = set()
    for reference in references:
        found = _referrers(conn, plan, collection_id, reference, key)
        if found is None:
            return None
        candidates |= found
    candidates.discard(key)
    plans = {key: dict(values)}
    for item_key, item_values in typed_storage.keyed_values(conn, collection_id, sorted(candidates)):
        # A spelling range holds every reference to the item and may hold other values too.
        if any(reference.target(collection_id, item_values.get(reference.field)) == key
               for reference in references):
            plans[item_key] = item_values
    # Close under the references, so each item that a rule judges finds the items it names.
    frontier, absent = list(plans), set()
    while frontier:
        wanted = {target for item_key in frontier for reference in references
                  if (target := reference.target(collection_id, plans[item_key].get(reference.field)))}
        wanted -= plans.keys() | absent
        found = dict(typed_storage.keyed_values(conn, collection_id, sorted(wanted)))
        absent |= wanted - found.keys()
        plans.update(found)
        frontier = list(found)
    return plans


def _referrers(conn, plan, collection_id: str, reference: Reference, key: str) -> set[str] | None:
    """The keys of the items whose reference value lies in a spelling range of `key`.

    None when no index of the ready projection leads with the reference field.
    """
    index = next((pair for pair in zip(plan.index_names, plan.indexes, strict=True)
                  if pair[1].keys[0].field == reference.field), None)
    kinds = {scalar.field: scalar.kind for scalar in plan.scalars if scalar.path == (scalar.field,)}
    if index is None or any(part.field not in kinds for part in index[1].keys):
        return None
    name, spec = index
    source = ir.Source("collections", collection_id, "")
    field = ir.Field(source, reference.field, kinds[reference.field])
    # The index's own order, so the compiler proves one index interval for each range.
    order = ir.Sort((*(ir.SortKey(ir.Field(source, part.field, kinds[part.field]), part.direction)
                       for part in spec.keys), ir.SortKey(ir.Field(source, "item_key", "string"))))
    as_of = dt.datetime.now(dt.UTC).isoformat()
    found = set()
    for lower, upper in reference.spellings(collection_id, key):
        bounds = tuple(ir.Filter(op, field, value) for op, value in (("gte", lower), ("lt", upper))
                       if value is not None)
        where = bounds[0] if len(bounds) == 1 else ir.Filter("all", children=bounds)
        query = ir.Query(source, ir.Project(()), where, (), None, None, order, ir.Page())
        try:
            compiled = typed_sql.compile_rows(query, plan, as_of=as_of)
        except ScalarValueError:
            return None  # A field whose declared type does not hold text has no spelling range.
        found.update(_keys(conn, plan, name, compiled))
    return found


def _keys(conn, plan, index: str, compiled: typed_sql.CompiledRows) -> list[str]:
    if not isinstance(plan, NativeProjectionPlan):
        return [row[0] for row in conn.execute(
            f"SELECT p.item_key FROM {plan.table_name} p INDEXED BY {index} WHERE {compiled.where_sql}",
            compiled.params)]
    relation = plan.read_relation("main", index=index) + " p"
    # A native interval reads through its index seeks; without a proved interval, the predicate alone.
    seeks = [(None, ())] if compiled.seeks is None else [(seek.where_sql, seek.params) for seek in compiled.seeks]
    keys = []
    for where, parameters in seeks:
        condition = compiled.where_sql if where is None else f"({where}) AND {compiled.where_sql}"
        keys.extend(native_identity.decode(row[0], plan.layout.identity) for row in conn.execute(
            f"SELECT p.item_key FROM {relation} WHERE {condition}", (*parameters, *compiled.params)))
    return keys
