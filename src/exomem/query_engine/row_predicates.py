"""Three-valued predicate evaluation over base and reference-path values.

``evaluate`` mirrors ``typed_sql._compile_legacy`` leaf for leaf: a comparison
is true or false exactly where the SQL predicate is, and a field whose value is
``UNRESOLVED`` — a reference the query names that did not resolve — makes its
leaf unknown. ``None`` propagates through ``all``, ``any`` and ``not`` as
SQL's unknown does, so a row whose reference does not resolve is excluded only
when no branch of the filter can decide it.
"""

from __future__ import annotations

import datetime as dt

from ..query_compat import match
from . import ir
from .scalars import LINK_TAG, MISSING, STRING_TAG, ScalarValueError, scalar_key

#: A path value whose reference did not resolve: every leaf reads it as unknown.
UNRESOLVED = object()


def _empty_text(value: object, kind: str) -> bool:
    """Whether ``value`` is an empty string the typed scalar domain stores as text, as the SQL path decides."""
    try:
        tag, key = scalar_key(value, kind)
    except ScalarValueError:
        return False
    return tag in (STRING_TAG, LINK_TAG) and key == ""


def _leaf(node: ir.Filter, value_of, *, instant: dt.datetime) -> bool | None:
    value = value_of(node.field)
    if value is UNRESOLVED:
        return None
    present = value is not MISSING and value is not None
    if node.op in ir.NULL_STATE_OPS:
        return {"is_null": value is None, "is_missing": value is MISSING,
                "is_not_null": present}[node.op]
    if node.op in ir.PRESENCE_OPS:
        # A present empty string on a string or link field does not exist, as in SQL.
        exists = present and not _empty_text(value, node.field.value_type)
        return exists if node.op == "exists" else not exists
    if node.op in ir.TEXT_OPS:
        return present and type(value) is str and match(
            {"value": value}, {"column": "value", "op": node.op, "value": node.value})
    if not present:
        return False
    kind = node.field.value_type
    try:
        value_tag, value_key = scalar_key(value, kind)
    except ScalarValueError:
        return node.op in {"ne", "nin"}

    def equal_to(literal) -> bool:
        try:
            tag, key = scalar_key(literal, kind)
        except ScalarValueError:
            return False
        return value_tag == tag and value_key == key

    if node.op in {"eq", "ne"}:
        return equal_to(node.value) if node.op == "eq" else not equal_to(node.value)
    if node.op in {"in", "nin"}:
        found = any(equal_to(item) for item in node.value)
        return found if node.op == "in" else not found
    if node.op == "between":
        bounds = node.value
        lower = _relative(bounds.lower, kind, instant)
        upper = _relative(bounds.upper, kind, instant)
        return (_ordered(value_tag, value_key, lower, kind, ">=" if bounds.include_lower else ">")
                and _ordered(value_tag, value_key, upper, kind, "<=" if bounds.include_upper else "<"))
    operator = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[node.op]
    return _ordered(value_tag, value_key, _relative(node.value, kind, instant), kind, operator)


def _relative(value: object, kind: str, instant: dt.datetime) -> object:
    from .typed_sql import _relative as resolve_relative

    return resolve_relative(value, kind, instant)


def _ordered(value_tag: int, value_key: str, literal: object, kind: str, operator: str) -> bool:
    """One ordered comparison of the stored key against the literal's, tags equal as in SQL."""
    try:
        tag, key = scalar_key(literal, kind)
    except ScalarValueError:
        return False
    if tag != value_tag:
        return False
    return {">": value_key > key, ">=": value_key >= key,
            "<": value_key < key, "<=": value_key <= key}[operator]


def evaluate(node: ir.Filter | None, value_of, *, instant: dt.datetime) -> bool | None:
    """True, False or None (unknown) for one predicate tree over ``value_of``.

    ``value_of(field)`` returns the field's value, ``MISSING`` or ``UNRESOLVED``.
    """
    if node is None:
        return True
    if node.op == "all":
        seen_unknown = False
        for child in node.children:
            result = evaluate(child, value_of, instant=instant)
            if result is False:
                return False
            seen_unknown = seen_unknown or result is None
        return None if seen_unknown else True
    if node.op == "any":
        seen_unknown = False
        for child in node.children:
            result = evaluate(child, value_of, instant=instant)
            if result is True:
                return True
            seen_unknown = seen_unknown or result is None
        return None if seen_unknown else False
    if node.op == "not":
        result = evaluate(node.children[0], value_of, instant=instant)
        return None if result is None else not result
    return _leaf(node, value_of, instant=instant)
