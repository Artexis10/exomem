"""Normalize the retained legacy dialect without pretending it is typed v1."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .. import query_data
from ..query_compat import OPS, VERSION, QueryDataError


@dataclass(frozen=True, slots=True)
class LegacyQuery:
    filters_json: str
    columns: tuple[str, ...]
    available: tuple[str, ...]
    sort_by: str | None
    descending: bool
    limit: int
    offset: int
    aggregate: str | None
    date_column: str | None
    warnings: tuple[str, ...]
    compatibility_version: int = VERSION


def normalize(*, columns_available, filters=None, columns=None, sort_by=None,
              descending=False, limit=query_data.DEFAULT_LIMIT, offset=0,
              aggregate=None, date_from=None, date_to=None, date_column=None,
              warnings=None) -> LegacyQuery:
    """Freeze caller values; retain legacy operator/error/date-bound semantics."""
    predicates = []
    for predicate in filters or ():
        if not isinstance(predicate, dict) or not predicate.get("column"):
            raise QueryDataError("BAD_FILTER", f"each filter needs a 'column': {predicate!r}")
        if predicate.get("op", "eq") not in OPS:
            raise QueryDataError("BAD_OP", f"unknown op {predicate.get('op')!r}; allowed: {sorted(OPS)}")
        predicates.append(predicate)
    available = tuple(columns_available)
    date_col = date_column or ("date" if "date" in available else None)
    notices = list(warnings or ())
    if (date_from or date_to) and not date_col:
        notices.append("date_from/date_to ignored: no date column found (pass date_column=)")
    if date_col and date_from:
        predicates.append({"column": date_col, "op": "gte", "value": date_from})
    if date_col and date_to:
        predicates.append({"column": date_col, "op": "lte", "value": date_to})
    # JSON freezes mutable literal arrays/objects without changing Python types.
    frozen = json.dumps(predicates, ensure_ascii=False)
    return LegacyQuery(frozen, tuple(columns or ()), available, sort_by, bool(descending),
                       query_data._bounded_limit(limit), max(0, int(offset)),
                       aggregate, date_col, tuple(notices))
