"""Bounded projection of selected JSON values from an admitted row stream."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal

import ijson
from ijson.common import ObjectBuilder

from ..collection_store.field_admission import WHOLE_SUBTREE
from .runtime import QueryError


def _events(source, check):
    events = ijson.sendable_list()
    parser = ijson.basic_parse_coro(events, use_float=False)
    try:
        while True:
            check()
            chunk = source.read(16 * 1024)
            check()
            if chunk:
                parser.send(chunk)
            else:
                parser.close()
            yield from events
            events.clear()
            if not chunk:
                break
    finally:
        # An intentionally cancelled/oversized projection can end mid-token.
        try:
            parser.close()
        except ijson.JSONError:
            pass


def read_selected_values(source, fields, *, max_bytes, check):
    """Build only selected top-level values, preserving exact numeric subtypes.

    The caller owns admission and the read-only stream's lifetime. Byte accounting
    matches compact UTF-8 JSON before each event enters the selected-value builder.
    """
    fields = frozenset(fields)
    result, containers = {}, []
    depth, size = 0, 2
    seen_root, builder, name = False, None, None
    events = _events(source, check)
    try:
        for event, value in events:
            check()
            if depth == 0:
                if seen_root or event != "start_map":
                    raise ValueError("stored values must be one object")
                seen_root, depth = True, 1
                continue
            if depth == 1 and event == "map_key":
                name = value
                builder = ObjectBuilder() if name in fields else None
                if builder is not None:
                    size += len(json.dumps(name, ensure_ascii=False).encode()) + 1 + bool(result)
            elif depth == 1 and event == "end_map":
                depth = 0
            else:
                if builder is not None:
                    if isinstance(value, Decimal):
                        value = float(value)
                    if event in {"end_map", "end_array"}:
                        size += 1
                        containers.pop()
                    else:
                        if containers and (event == "map_key" or containers[-1][0]):
                            size += bool(containers[-1][1])
                            containers[-1][1] += 1
                        if event in {"start_map", "start_array"}:
                            size += 1
                            containers.append([event == "start_array", 0])
                        else:
                            size += len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
                            size += event == "map_key"
                    if size > max_bytes:
                        raise QueryError("QUERY_RESULT_TOO_LARGE")
                    builder.event(event, value)
                if event in {"start_map", "start_array"}:
                    depth += 1
                elif event in {"end_map", "end_array"}:
                    depth -= 1
                if builder is not None and depth == 1:
                    result[name], builder = builder.value, None
            if size > max_bytes:
                raise QueryError("QUERY_RESULT_TOO_LARGE")
        check()
        return result
    except (ijson.JSONError, ValueError, TypeError, RecursionError) as error:
        raise QueryError("QUERY_COST_LIMIT") from error
    finally:
        events.close()


def read_selected_tree(source, selection, *, max_bytes, check):
    """Hydrate only admitted object properties and array members from a JSON stream."""
    events = iter(_events(source, check))
    retained = 0

    def charge(value):
        nonlocal retained
        retained += len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) + 1
        if retained > max_bytes:
            raise QueryError("QUERY_RESULT_TOO_LARGE")

    def read(chosen, first=None):
        check()
        event, value = next(events) if first is None else first
        if event == "start_map":
            if chosen is not None and chosen is not WHOLE_SUBTREE and not isinstance(chosen, Mapping):
                raise ValueError("stored container does not match admitted field shape")
            result = {} if chosen is not None else None
            if chosen is not None:
                charge({})
            while (event_value := next(events))[0] != "end_map":
                if event_value[0] != "map_key":
                    raise ValueError("invalid stored object")
                name = event_value[1]
                child = WHOLE_SUBTREE if chosen is WHOLE_SUBTREE else chosen.get(name) if chosen is not None else None
                value = read(child)
                if child is not None:
                    charge(name)
                    result[name] = value
            return result
        if event == "start_array":
            if chosen is not None and chosen is not WHOLE_SUBTREE and not isinstance(chosen, tuple):
                raise ValueError("stored container does not match admitted field shape")
            result = [] if chosen is not None else None
            if chosen is not None:
                charge([])
            child = WHOLE_SUBTREE if chosen is WHOLE_SUBTREE else chosen[0] if chosen is not None else None
            while (event_value := next(events))[0] != "end_array":
                value = read(child, event_value)
                if child is not None:
                    result.append(value)
            return result
        if chosen is None:
            return None
        if value is not None and chosen is not True and chosen is not WHOLE_SUBTREE:
            raise ValueError("stored scalar does not match admitted field shape")
        value = float(value) if isinstance(value, Decimal) else value
        charge(value)
        return value

    try:
        result = read(selection)
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()) > max_bytes:
            raise QueryError("QUERY_RESULT_TOO_LARGE")
        check()
        return result
    except (ijson.JSONError, ValueError, TypeError, StopIteration, RecursionError) as error:
        raise QueryError("QUERY_UNAVAILABLE") from error
    finally:
        events.close()
