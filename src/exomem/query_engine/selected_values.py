"""Bounded projection of selected JSON values from an admitted row stream."""

from __future__ import annotations

import json
from decimal import Decimal

import ijson
from ijson.common import ObjectBuilder

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
