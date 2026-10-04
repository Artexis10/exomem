"""Version-one primitives shared by file queries and the legacy SQL adapter.

These are the existing dataset semantics, not typed v1 comparisons. In
particular membership uses Python string identity and text matching uses
Unicode lower(), not SQLite affinity, LIKE or NOCASE.
"""

from __future__ import annotations

import re
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

VERSION = 1
OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "contains", "icontains",
                 "startswith", "in", "nin", "exists", "missing"})
DATE_LIKE = re.compile(r"\d{1,4}[-/]\d")
_NUM_PREFIX = re.compile(r"^[<>≤≥=~\s]+")
_LEADING_NUM = re.compile(r"[+-]?\d+(?:[.,]\d+)?")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class QueryDataError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def get_field(row: Any, dotted: str) -> Any:
    """Nested access via dotted key — dicts by key, lists by integer index."""
    cur = row
    for part in str(dotted).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def coerce_num(value: Any) -> float | None:
    """Best-effort numeric coercion; tolerant of comma decimals and lab operators."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = _NUM_PREFIX.sub("", value.strip())
    if not text or DATE_LIKE.match(text) or _UUID.fullmatch(text):
        return None
    matched = _LEADING_NUM.match(text)
    if not matched:
        return None
    try:
        return float(matched.group(0).replace(",", "."))
    except ValueError:
        return None


def match(row: dict, filt: dict) -> bool:
    column, op, value = filt["column"], filt.get("op", "eq"), filt.get("value")
    actual = get_field(row, column)
    if op == "exists":
        return actual not in (None, "")
    if op == "missing":
        return actual in (None, "")
    if op in ("in", "nin"):
        values = value if isinstance(value, list) else [value]
        hit = str(actual) in {str(item) for item in values}
        return hit if op == "in" else not hit
    if op in ("contains", "icontains", "startswith"):
        left = "" if actual is None else str(actual)
        right = "" if value is None else str(value)
        if op == "contains":
            return right in left
        if op == "icontains":
            return right.lower() in left.lower()
        return left.lower().startswith(right.lower())
    left_number, right_number = coerce_num(actual), coerce_num(value)
    if op in ("eq", "ne"):
        if left_number is not None and right_number is not None:
            equal = left_number == right_number
        else:
            equal = ("" if actual is None else str(actual)) == ("" if value is None else str(value))
        return equal if op == "eq" else not equal
    if left_number is not None and right_number is not None:
        left, right = left_number, right_number
    elif left_number is None and right_number is None:
        left = "" if actual is None else str(actual)
        right = "" if value is None else str(value)
    else:
        return False
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    raise QueryDataError("BAD_OP", f"unknown filter op {op!r}; allowed: {sorted(OPS)}")


def sort_key(value: Any) -> tuple[int, float, str]:
    number = coerce_num(value)
    return (0, number, "") if number is not None else (1, 0.0, "" if value is None else str(value))


def distinct_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)


def group_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def numeric_aggregate(rows: Iterable[dict], func: str, column: str) -> dict:
    """Use Python's ordered numeric reduction without retaining a numeric array."""
    count = 0

    def numbers():
        nonlocal count
        for row in rows:
            number = coerce_num(get_field(row, column))
            if number is not None:
                count += 1
                yield number

    values = numbers()
    if func == "min":
        value = min(values, default=None)
    elif func == "max":
        value = max(values, default=None)
    else:
        value = sum(values)
        if func == "avg" and count:
            value /= count
    if not count:
        return {func: None, "n": 0, "note": f"no numeric values in {column!r}"}
    return {func: value, "n": count}


def latest_aggregate(rows: Iterable[dict], column: str) -> dict:
    best, best_key = None, None
    for row in rows:
        key = get_field(row, column)
        if key is not None and (best_key is None or str(key) > str(best_key)):
            best, best_key = row, key
    return {"latest_by": column, "row": best}


def profile_kind(name: str, non_null: int, numeric: int, date_like: int,
                 distinct: int, maximum: int) -> str:
    if non_null and ("date" in name.lower() or date_like >= 0.6 * non_null) and numeric < 0.6 * non_null:
        return "date"
    if non_null and numeric >= 0.6 * non_null:
        return "numeric"
    return "categorical" if distinct <= maximum else "text"
