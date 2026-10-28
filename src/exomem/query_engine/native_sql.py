"""Physical native comparisons over the existing validated row-query contract."""

import math
from dataclasses import dataclass

from ..collection_store import native_identity, native_operands
from . import ordered_seeks, typed_sql
from .runtime import QueryError
from .scalars import LINK_TAG, STRING_TAG, ScalarValueError


@dataclass(frozen=True, slots=True)
class _Field:
    rank: str
    tag: str | None
    keys: tuple[str, ...]
    kind: str


def compile_rows(query, projection, *, as_of, after):
    instant = typed_sql._validate_shape(query, projection, as_of, after)
    params, dependencies = [], dict.fromkeys(field.path for field in query.select.fields)
    scalars = {scalar.field: scalar for scalar in projection.scalars}
    residual = False
    identity = projection.layout.identity
    cuts = {}

    def uuid_cuts(value):
        if value not in cuts:
            cuts[value] = native_identity.uuid_cuts(value)
        return cuts[value]

    def columns(field):
        dependencies[field.path] = None
        if field.source != query.source:
            raise QueryError("QUERY_UNSUPPORTED", "cross-source fields are unavailable")
        if field.path == "item_key":
            return _Field("0", None, ("p.item_key",), "string")
        scalar = scalars.get(field.path)
        if scalar is None:
            raise QueryError("QUERY_UNSUPPORTED", "a declared scalar projection is required")
        tag, key = projection.key_columns(field.path)
        return _Field("p." + projection.rank_column(field.path), "p." + tag,
                      ("p." + projection.band_column(field.path), "p." + key), scalar.kind)

    def bind(value):
        params.append(value)
        return "?"

    def tuple_sql(values):
        return "(" + ",".join(values) + ")" if len(values) > 1 else values[0]

    def key(value, field, *, exact):
        value = typed_sql._relative(value, field.kind, instant)
        if field.tag is None:
            try:
                return None, (native_identity.encode(value, identity),)
            except (ValueError, UnicodeError) as error:
                if identity == native_identity.UUID16 or (identity == native_identity.ORDERED and exact):
                    return None, None  # no stored key spells this literal, so an equality matches nothing
                if identity == native_identity.ORDERED:
                    raise QueryError("QUERY_VALUE_INVALID",
                                     "a key range bound must be the exact spelling of an ordered item key") from error
                raise ScalarValueError("identity literal must be valid UTF-8") from error
        tag, band, payload = native_operands.scalar_operand(value, field.kind)
        if tag < 2:
            raise ScalarValueError("ordinary comparisons require a non-null value")
        return tag, (band, payload)

    def identity_range(value, operator):
        lower, upper = uuid_cuts(value)
        cut = lower if operator in (">=", "<") else upper
        if operator in (">", ">="):
            return "0" if cut == 1 << 128 else "1" if cut == 0 else "p.item_key>=" + bind(cut.to_bytes(16, "big"))
        return "0" if cut == 0 else "1" if cut == 1 << 128 else "p.item_key<" + bind(cut.to_bytes(16, "big"))

    def comparison(value, field, operator):
        if field.tag is None and identity == native_identity.UUID16 and operator != "=":
            return identity_range(value, operator)
        tag, values = key(value, field, exact=operator == "=")
        if values is None:
            return "0"
        prefix = "" if field.tag is None else field.tag + "=" + bind(tag) + " AND "
        expression = tuple_sql(field.keys) + operator + tuple_sql(tuple(bind(value) for value in values))
        return "(" + prefix + expression + ")"

    def predicate(node):
        nonlocal residual
        if node is None:
            return "1"
        if node.op in ("all", "any"):
            parts = [predicate(child) for child in node.children]
            return "(" + (" AND " if node.op == "all" else " OR ").join(parts) + ")" if parts else ("1" if node.op == "all" else "0")
        if node.op == "not":
            return "(NOT " + predicate(node.children[0]) + ")"
        field = columns(node.field)
        present = field.rank + "=0"
        payload = field.keys[-1]
        if node.op in ("is_null", "is_missing", "is_not_null"):  # nosemgrep: ep-word-membership -- The query schema fixes these null operators.
            return f"({field.rank}={ {'is_null': 1, 'is_missing': 2, 'is_not_null': 0}[node.op]})"
        if node.op in ("exists", "missing"):
            empty = "p.item_key=X''" if field.tag is None else f"({field.tag} IN ({STRING_TAG},{LINK_TAG}) AND {payload}='')"
            exists = f"({present} AND NOT ({empty}))"
            return exists if node.op == "exists" else "(NOT " + exists + ")"
        if node.op in ("contains", "icontains", "startswith"):  # nosemgrep: ep-word-membership -- The query schema fixes these text operators.
            residual = True
            function = "exomem_typed_identity_text" if field.tag is None else "exomem_typed_text"
            encoded = payload if field.tag is None else f"hex(CAST({payload} AS BLOB))"
            return f"({present} AND {function}({bind(node.op)},{encoded},{bind(node.value)}))"
        if node.op in ("eq", "ne", "gt", "gte", "lt", "lte"):  # nosemgrep: ep-word-membership -- The query schema fixes these comparison operators.
            operator = {"eq": "=", "ne": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[node.op]
            expression = comparison(node.value, field, operator)
            if node.op == "ne":
                expression = "NOT " + expression
        elif node.op in ("in", "nin"):
            expression = "(" + " OR ".join(comparison(value, field, "=") for value in node.value) + ")" if node.value else "0"
            if node.op == "nin":
                expression = "NOT " + expression
        elif node.op == "between":
            expression = (comparison(node.value.lower, field, ">=" if node.value.include_lower else ">") + " AND "
                          + comparison(node.value.upper, field, "<=" if node.value.include_upper else "<"))
        else:
            raise QueryError("QUERY_UNSUPPORTED", "unsupported typed row predicate")
        return f"({present} AND {expression})"

    blocks = []
    for order in query.order_by.keys:
        field = columns(order.field)
        if field.tag is not None:
            blocks.extend((((field.rank,), "ASC" if order.nulls == "last" else "DESC"), ((field.tag,), "ASC")))
        blocks.append((field.keys, order.direction.upper()))
    terms = tuple((column, direction) for columns_, direction in blocks for column in columns_)
    if not terms:
        raise QueryError("QUERY_UNSUPPORTED", "deterministic row ordering is required")
    if after is not None and (len(after) != len(terms) or any(type(value) not in (int, float, str, bytes)
                             or (type(value) is float and not math.isfinite(value)) for value in after)):
        raise QueryError("QUERY_VALUE_INVALID", "continuation does not match the scalar order")
    index, page_bound = typed_sql._index_proof(query, projection)
    where = predicate(query.where)
    seeks = None
    if page_bound and not residual:
        fixed, ranges, empty = {}, {}, False

        def fix(column, value):
            nonlocal empty
            if column in fixed and fixed[column] != value:
                empty = True
            fixed[column] = value

        def bound(field, value, lower, inclusive):
            nonlocal empty
            if field.tag is None and identity == native_identity.UUID16:
                low, high = uuid_cuts(value)
                cut = low if (lower and inclusive) or (not lower and not inclusive) else high
                if (lower and cut == 1 << 128) or (not lower and cut == 0):
                    empty = True
                    return
                if (lower and cut == 0) or (not lower and cut == 1 << 128):
                    return
                values = (cut.to_bytes(16, "big"),)
                inclusive = lower
            else:
                tag, values = key(value, field, exact=False)
                if field.tag is not None:
                    fix(field.rank, 0)
                    fix(field.tag, tag)
            low, high = ranges.get(field.keys, (None, None))
            endpoint = values, inclusive
            if lower:
                low = ordered_seeks.intersection(low, endpoint, lower=True)
            else:
                high = ordered_seeks.intersection(high, endpoint, lower=False)
            ranges[field.keys] = low, high

        for leaf in typed_sql._leaves(query.where) or ():
            field = columns(leaf.field)
            if leaf.op == "eq":
                tag, values = key(leaf.value, field, exact=True)
                if values is None:
                    empty = True
                    continue
                if field.tag is not None:
                    fix(field.rank, 0)
                    fix(field.tag, tag)
                for column, value in zip(field.keys, values, strict=True):
                    fix(column, value)
            elif leaf.op == "between":
                bound(field, leaf.value.lower, True, leaf.value.include_lower)
                bound(field, leaf.value.upper, False, leaf.value.include_upper)
            else:
                bound(field, leaf.value, leaf.op in ("gt", "gte"), leaf.op in ("gte", "lte"))
        seeks = () if empty else tuple(ordered_seeks.compile_seeks(blocks, after, fixed, ranges))
    elif after is not None:
        branches = []
        for position, ((column, direction), value) in enumerate(zip(terms, after, strict=True)):
            prefix = [term[0] + "=" + bind(previous) for term, previous in zip(terms[:position], after[:position], strict=True)]
            prefix.append(column + (">" if direction == "ASC" else "<") + bind(value))
            branches.append("(" + " AND ".join(prefix) + ")")
        where = "(" + where + " AND (" + " OR ".join(branches) + "))"
    return typed_sql.CompiledRows(where, tuple(params), ",".join(column + " " + direction for column, direction in terms),
                                  terms, tuple(field.path for field in query.select.fields), tuple(dependencies),
                                  residual, index, page_bound and not residual, instant.isoformat(), seeks)
