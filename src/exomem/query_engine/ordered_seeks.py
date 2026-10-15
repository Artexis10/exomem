"""Bounded disjoint seeks for an indexed order with mixed scalar directions."""

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Seek:
    where_sql: str
    params: tuple
    order_sql: str


def intersection(left, right, *, lower):
    """Intersect like endpoints, each containing a native tuple and inclusion bit."""
    if left is None:
        return right
    if right is None:
        return left
    if left[0] == right[0]:
        return left[0], left[1] and right[1]
    return left if (left[0] > right[0]) == lower else right


def normalize(fixed, ranges):
    """Remove ranges proved by equality and reject contradictory conjunctions."""
    remaining = {}
    for columns, (low, high) in ranges.items():
        if low is not None and high is not None:
            if low[0] > high[0] or (low[0] == high[0] and not (low[1] and high[1])):
                return None
        present = [column in fixed for column in columns]
        if all(present):
            point = tuple(fixed[column] for column in columns)
            if low is not None and (point < low[0] or (point == low[0] and not low[1])):
                return None
            if high is not None and (point > high[0] or (point == high[0] and not high[1])):
                return None
        elif any(present):
            raise ValueError("a native scalar order block must be fixed as a whole")
        else:
            remaining[columns] = low, high
    return remaining


def _scalar_intervals(columns, low, high, direction):
    """Split a logical tuple interval into disjoint scalar-prefix B-tree intervals."""
    if low is not None and high is not None:
        if low[0] > high[0] or (low[0] == high[0] and not (low[1] and high[1])):
            return
        if low[0] == high[0]:
            yield dict(zip(columns, low[0], strict=True)), {}
            return
    if len(columns) == 1:
        yield {}, {columns[0]: (None if low is None else (low[0][0], low[1]),
                               None if high is None else (high[0][0], high[1]))}
        return
    if low is None and high is None:
        yield {}, {}
        return
    first, rest = columns[0], columns[1:]
    if low is not None and high is not None and low[0][0] == high[0][0]:
        for equality, intervals in _scalar_intervals(rest, (low[0][1:], low[1]), (high[0][1:], high[1]), direction):
            yield {first: low[0][0], **equality}, intervals
        return

    def edge(endpoint, lower):
        tail = endpoint[0][1:], endpoint[1]
        for equality, intervals in _scalar_intervals(rest, tail if lower else None, None if lower else tail, direction):
            yield {first: endpoint[0][0], **equality}, intervals

    sides = (True, None, False) if direction == "ASC" else (False, None, True)
    for side in sides:
        if side is True and low is not None:
            yield from edge(low, True)
        elif side is False and high is not None:
            yield from edge(high, False)
        elif side is None:
            yield {}, {first: (None if low is None else (low[0][0], False),
                                None if high is None else (high[0][0], False))}


def _lower_intervals(ordered, terms, position, equalities, scalar_ranges):
    if position == len(ordered):
        clauses, values = [], []
        for column, value in equalities.items():
            clauses.append(column + "=?")
            values.append(value)
        for column, endpoints in scalar_ranges.items():
            for is_lower, endpoint in zip((True, False), endpoints, strict=True):
                if endpoint is not None:
                    clauses.append(column + (">" if is_lower else "<") + ("=" if endpoint[1] else "") + "?")
                    values.append(endpoint[0])
        # Equality-fixed expressions otherwise force SQLite to sort expression-index results.
        order = ",".join(column + " " + direction for column, direction in terms if column not in equalities)
        yield Seek(" AND ".join(clauses) or "1", tuple(values), order)
        return
    columns, direction, endpoints = ordered[position]
    for added, bounded in _scalar_intervals(columns, *endpoints, direction):
        yield from _lower_intervals(ordered, terms, position + 1, {**equalities, **added}, {**scalar_ranges, **bounded})


def compile_seeks(blocks, boundary, fixed, ranges) -> Iterator[Seek]:
    """Emit nearest suffixes first, intersecting both ends before each B-tree seek.

    Column names come from the validated compiler. A numeric band/key pair is
    one block because its components share a direction; presence stays separate.
    """
    terms = tuple((column, direction) for columns, direction in blocks for column in columns)
    # A compiler mismatch must not silently discard a filter during interval lowering.
    if set(ranges) - {columns for columns, _ in blocks}:
        raise ValueError("a seek interval must belong to the compiled order")
    offsets, offset = [], 0
    for columns, _ in blocks:
        offsets.append(offset)
        offset += len(columns)
    if boundary is not None and len(boundary) != len(terms):
        raise ValueError("a continuation must match the physical scalar order")
    suffixes = (None,) if boundary is None else reversed(range(len(blocks)))
    for suffix in suffixes:
        equality, intervals = dict(fixed), dict(ranges)
        if suffix is not None:
            prefix = dict(zip((column for column, _ in terms[:offsets[suffix]]), boundary[:offsets[suffix]], strict=True))
            if any(column in equality and equality[column] != value for column, value in prefix.items()):
                continue
            equality.update(prefix)
            columns, direction = blocks[suffix]
            endpoint = tuple(boundary[offsets[suffix]:offsets[suffix] + len(columns)]), False
            low, high = intervals.get(columns, (None, None))
            if direction == "ASC":
                low = intersection(low, endpoint, lower=True)
            else:
                high = intersection(high, endpoint, lower=False)
            intervals[columns] = low, high
        intervals = normalize(equality, intervals)
        if intervals is None:
            continue
        ordered = [(columns, direction, intervals[columns]) for columns, direction in blocks if columns in intervals]
        yield from _lower_intervals(ordered, terms, 0, equality, {})
