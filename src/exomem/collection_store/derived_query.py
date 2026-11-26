"""Read a derived collection with the legacy query arguments.

A derived collection keeps its rows beside the store, where only the typed version-1 query
engine reads them. ``typed_query`` spells the legacy filters, columns, sort, limit,
continuation and date bounds as one typed query for that engine, so an agent reads a derived
collection as it reads any other. An argument with no typed spelling refuses and names the
typed query to send instead.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .. import capabilities
from ..query_compat import DEFAULT_OP
from . import derived_rows

#: The legacy arguments `typed_query` spells; any other argument of the call refuses.
# nosemgrep: ep-word-set -- the legacy query's argument names, fixed by the tool schema
_SPELLED = frozenset({"filters", "columns", "sort_by", "descending", "limit", "continuation",
                      "date_from", "date_to", "date_column"})


def _leaf(predicate: Mapping[str, Any]) -> dict[str, Any]:
    """One legacy filter as a typed predicate.

    The legacy dialect counts a missing cell as unequal to every value, and the typed engine lets
    a missing cell satisfy no comparison, so a negation also takes the rows that lack the field.
    """
    from ..query_engine.ir import PRESENCE_OPS

    op = predicate.get("op", DEFAULT_OP)
    # A presence test takes no value in the typed grammar, and the legacy one ignores it.
    value = {} if op in PRESENCE_OPS or "value" not in predicate else {"value": predicate["value"]}
    leaf = {"field": predicate["column"], "op": op, **value}
    if op in ("ne", "nin"):
        return {"any": [leaf, {"field": predicate["column"], "op": "missing"}]}
    return leaf


def typed_query(manifest, item_key: object, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The typed query that reads what the legacy ``arguments`` ask of derived ``manifest``.

    The legacy normalizer settles the filters, the date column, the date bounds and the limit,
    so the legacy dialect keeps one definition. ``item_key`` names the row that a legacy query
    reads with its body, which a derived collection does not keep.
    """
    from ..query_engine import legacy

    # An argument left at its default asks nothing the typed answer lacks, so only a set one refuses.
    unspelled = sorted(name for name, value in arguments.items() if name not in _SPELLED
                       and value not in (None, False) and not (name == "output_format" and value == "json"))
    if item_key is not None:
        unspelled.append("item_key")
    shaped = legacy.normalize(
        columns_available=tuple(manifest.schema.fields),
        **{name: value for name, value in arguments.items() if name in _SPELLED - {"continuation"}})
    # Without a date column the legacy query ignores the bounds with a warning, which a typed answer cannot carry.
    if shaped.warnings:
        unspelled.append("date_from/date_to without a date column")
    if unspelled:
        raise derived_rows.refused(
            "legacy query shaping by " + ", ".join(unspelled),
            repair="read it with the typed query, which also takes group_by and aggregates: "
            + capabilities.active_call_text("record_memory", {"action": "query", "query": {"version": 1}}))
    query: dict[str, Any] = {"version": 1, "page": {"limit": shaped.limit}}
    if arguments.get("continuation") is not None:
        query["page"]["after"] = arguments["continuation"]
    leaves = [_leaf(predicate) for predicate in json.loads(shaped.filters_json)]
    if leaves:
        query["where"] = {"all": leaves}
    if shaped.columns:
        query["select"] = list(shaped.columns)
    if shaped.sort_by:
        query["order_by"] = [{"field": shaped.sort_by, "direction": "desc" if shaped.descending else "asc"}]
    return query
