"""Pure typed-row compilation; physical readiness and admission belong to callers."""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass

from ..collection_store.query_indexes import ProjectionPlan
from ..query_compat import match
from . import ir
from .runtime import QueryError
from .scalars import LINK_TAG, STRING_TAG, ScalarValueError, parse_instant, scalar_key


@dataclass(frozen=True, slots=True)
class CompiledRows:
    where_sql: str
    params: tuple[object, ...]
    order_sql: str
    order_terms: tuple[tuple[str, str], ...]
    projection_paths: tuple[str, ...]
    dependency_paths: tuple[str, ...]
    residual: bool
    usable_index: str | None
    page_bound: bool
    as_of: str


def text_predicate(op: str, payload: str, value: str) -> bool:
    """Caller-registered exomem_typed_text UDF; Unicode compatibility is shared."""
    if op not in {"contains", "icontains", "startswith"}:
        raise ValueError("unsupported string predicate")
    actual = bytes.fromhex(payload).decode("utf-8")
    return match({"value": actual}, {"column": "value", "op": op, "value": value})


def _relative(value: object, kind: str, instant: dt.datetime) -> object:
    if not isinstance(value, ir.RelativeDate):
        return value
    try:
        if value.unit in {"day", "week"}:
            result = instant + dt.timedelta(days=value.amount * (7 if value.unit == "week" else 1))
        elif value.unit == "month":
            year, month = divmod(instant.year * 12 + instant.month - 1 + value.amount, 12)
            result = instant.replace(year=year, month=month + 1,
                                     day=min(instant.day, calendar.monthrange(year, month + 1)[1]))
        else:
            raise ScalarValueError("unsupported relative date unit")
        return result.date().isoformat() if kind == "date" else result.isoformat()
    except (ValueError, OverflowError) as error:
        raise ScalarValueError("relative date is outside the supported calendar") from error


def _leaves(node: ir.Filter | None) -> list[ir.Filter] | None:
    if node is None:
        return []
    if node.op == "all":
        result = []
        for child in node.children:
            leaves = _leaves(child)
            if leaves is None:
                return None
            result.extend(leaves)
        return result
    return None if node.op in {"any", "not"} else [node]


def _index_proof(query: ir.Query, plan: ProjectionPlan) -> tuple[str | None, bool]:
    """Prove one contiguous index interval, not merely an available ORDER BY."""
    leaves = _leaves(query.where)
    equal = {leaf.field.path for leaf in leaves or () if leaf.op == "eq"}
    order = [(key.field.path, key.direction) for key in query.order_by.keys]
    if any(key.nulls != "last" for key in query.order_by.keys):
        return None, False
    candidates = [(name, [(key.field, key.direction) for key in index.keys] + [("item_key", "asc")])
                  for name, index in zip(plan.index_names, plan.indexes, strict=True)]
    candidates.append((f"sqlite_autoindex_{plan.table_name}_1", [("item_key", "asc")]))
    for name, physical in candidates:
        if [(path, direction) for path, direction in physical if path not in equal] != [
            (path, direction) for path, direction in order if path not in equal
        ]:
            continue
        if leaves is None or any(leaf.op not in {"eq", "gt", "gte", "lt", "lte", "between"} for leaf in leaves):
            return name, False
        by_path = {}
        for leaf in leaves:
            by_path.setdefault(leaf.field.path, []).append(leaf.op)
        interval_ended = False
        for path, _ in physical:
            operations = by_path.pop(path, [])
            if operations and interval_ended:
                return name, False
            if operations != ["eq"]:
                interval_ended = True
        return name, not by_path
    return None, False


def compile_rows(query: ir.Query, projection: ProjectionPlan, *, as_of: str,
                 after: tuple[object, ...] | None = None) -> CompiledRows:
    """Compile validated IR against a trusted physical template, never authorize it.

    SQL uses the fixed alias p. Projection/dependency paths are logical data for
    the caller's admitted projector, not executable JSON paths. page_bound is
    only a structural interval proof; callers still charge and seal execution.
    """
    if (query.source.domain != "collections" or query.source.ref != projection.collection_id
            or query.joins or query.aggregate is not None or query.having is not None
            or query.graph is not None or query.text is not None):
        raise QueryError("QUERY_UNSUPPORTED", "typed row compilation does not support this query shape")
    instant = parse_instant(as_of)
    if query.as_of is not None and parse_instant(query.as_of) != instant:
        raise QueryError("QUERY_VALUE_INVALID", "query as_of differs from its frozen instant")
    if query.page.after is not None and after is None:
        raise QueryError("QUERY_VALUE_INVALID", "an authenticated continuation is required")
    params: list[object] = []
    dependencies = dict.fromkeys(field.path for field in query.select.fields)
    scalars = {scalar.field: scalar for scalar in projection.scalars}
    residual = False

    def columns(field):
        dependencies[field.path] = None
        if field.source != query.source:
            raise QueryError("QUERY_UNSUPPORTED", "cross-source fields are unavailable")
        if field.path == "item_key":
            return "0", None, "p.item_key", "string"
        scalar = scalars.get(field.path)
        if scalar is None:
            raise QueryError("QUERY_UNSUPPORTED", "a declared scalar projection is required")
        tag, payload = projection.key_columns(field.path)
        return "p." + projection.rank_column(field.path), "p." + tag, "p." + payload, scalar.kind

    def bind(value):
        params.append(value)
        return "?"

    def predicate(node):
        nonlocal residual
        if node is None:
            return "1"
        if node.op in {"all", "any"}:
            parts = [predicate(child) for child in node.children]
            return "(" + (" AND " if node.op == "all" else " OR ").join(parts) + ")" if parts else ("1" if node.op == "all" else "0")
        if node.op == "not":
            return "(NOT " + predicate(node.children[0]) + ")"
        rank, tag, payload, kind = columns(node.field)
        present = f"{rank}=0"

        def comparison(value, operator):
            value = _relative(value, kind, instant)
            key_tag, key = scalar_key(value, kind)
            if key_tag < 2:
                raise ScalarValueError("ordinary comparisons require a non-null value")
            if tag is None:
                return f"{payload}{operator}{bind(value)}"
            return f"({tag}={bind(key_tag)} AND {payload}{operator}{bind(key)})"

        if node.op in {"is_null", "is_missing", "is_not_null"}:
            return f"({rank}={ {'is_null': 1, 'is_missing': 2, 'is_not_null': 0}[node.op]})"
        if node.op in {"exists", "missing"}:
            nonempty = f"{payload}<>''" if tag is None else f"NOT ({tag} IN ({STRING_TAG},{LINK_TAG}) AND {payload}='')"
            exists = f"({present} AND {nonempty})"
            return exists if node.op == "exists" else f"(NOT {exists})"
        if node.op in {"contains", "icontains", "startswith"}:
            residual = True
            encoded = f"hex(CAST({payload} AS BLOB))" if tag is None else payload
            return f"({present} AND exomem_typed_text({bind(node.op)},{encoded},{bind(node.value)}))"
        if node.op in {"eq", "ne", "gt", "gte", "lt", "lte"}:
            operator = {"eq": "=", "ne": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[node.op]
            expression = comparison(node.value, operator)
            if node.op == "ne":
                expression = "NOT " + expression
        elif node.op in {"in", "nin"}:
            expression = "(" + " OR ".join(comparison(value, "=") for value in node.value) + ")" if node.value else "0"
            if node.op == "nin":
                expression = "NOT " + expression
        elif node.op == "between":
            bounds = node.value
            expression = (comparison(bounds.lower, ">=" if bounds.include_lower else ">") + " AND "
                          + comparison(bounds.upper, "<=" if bounds.include_upper else "<"))
        else:
            raise QueryError("QUERY_UNSUPPORTED", "unsupported typed row predicate")
        return f"({present} AND {expression})"

    where = predicate(query.where)
    terms = []
    for key in query.order_by.keys:
        rank, tag, payload, _ = columns(key.field)
        if tag is not None:
            terms.extend(((rank, "ASC" if key.nulls == "last" else "DESC"), (tag, "ASC")))
        terms.append((payload, key.direction.upper()))
    if not terms:
        raise QueryError("QUERY_UNSUPPORTED", "deterministic row ordering is required")
    index, page_bound = _index_proof(query, projection)
    if after is not None:
        if len(after) != len(terms) or any(type(value) not in {int, str} for value in after):
            raise QueryError("QUERY_VALUE_INVALID", "continuation does not match the scalar order")
        directions = {direction for _, direction in terms}
        if len(directions) == 1:
            operator = ">" if terms[0][1] == "ASC" else "<"
            continuation = "(" + ",".join(column for column, _ in terms) + ")" + operator
            continuation += "(" + ",".join(bind(value) for value in after) + ")"
        else:
            page_bound = False
            branches = []
            for position, (column, direction) in enumerate(terms):
                prefix = [f"{terms[n][0]}={bind(after[n])}" for n in range(position)]
                prefix.append(f"{column}{'>' if direction == 'ASC' else '<'}{bind(after[position])}")
                branches.append("(" + " AND ".join(prefix) + ")")
            continuation = "(" + " OR ".join(branches) + ")"
        where = f"({where} AND {continuation})"
    return CompiledRows(where, tuple(params), ",".join(f"{column} {direction}" for column, direction in terms),
                        tuple(terms), tuple(field.path for field in query.select.fields), tuple(dependencies),
                        residual, index, page_bound and not residual, instant.isoformat())
