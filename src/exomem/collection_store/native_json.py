"""Validate the canonical JSON escape wire without hydrating unselected siblings."""

from decimal import Decimal
from io import BytesIO

from .typed_storage import TypedStorageError, canonical_json


def canonical_events(payload, *, max_bytes: int, check, comparison=None, byte_length=None):
    """Use the existing parser and canonical serializer, comparing one token at a time."""
    from ..query_engine.selected_values import _events

    buffered = type(payload) is bytes
    size = len(payload) if buffered else byte_length
    if (type(max_bytes) is not int or max_bytes < 0 or type(size) is not int or not 0 <= size <= max_bytes
            or (not buffered and comparison is None)):
        raise TypedStorageError("native JSON exceeds the decoding budget")
    offset, stack = 0, []
    view = memoryview(payload) if buffered else None
    events = _events(BytesIO(payload) if buffered else payload, check)
    try:
        for event, value in events:
            check()
            prefix = ""
            if event == "map_key":
                state = stack[-1]
                if state[2] is not None and state[2] >= value:
                    raise TypedStorageError("native JSON is not canonical")
                prefix = "," if state[1] else ""
                state[1] += 1
                state[2] = value
                text = prefix + canonical_json(value) + ":"
            elif event in ("end_map", "end_array"):
                stack.pop()
                text = "}" if event == "end_map" else "]"
            else:
                if stack and stack[-1][0] == "start_array":
                    prefix = "," if stack[-1][1] else ""
                    stack[-1][1] += 1
                if event in ("start_map", "start_array"):
                    text = prefix + ("{" if event == "start_map" else "[")
                    stack.append([event, 0, None])
                else:
                    text = prefix + canonical_json(float(value) if isinstance(value, Decimal) else value)
            encoded = text.encode("utf-8")
            actual = view[offset:offset + len(encoded)] if buffered else comparison.read(len(encoded))
            if actual != encoded:
                raise TypedStorageError("native JSON is not canonical")
            offset += len(encoded)
            yield event, value
        if offset != size:
            raise TypedStorageError("native JSON is not canonical")
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        raise TypedStorageError("native JSON is invalid") from error
    finally:
        events.close()
        if view is not None:
            view.release()
