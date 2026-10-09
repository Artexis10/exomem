"""Rows of one streamed JSON document along a declared row path (OpenSpec bring-in-large-exports §3).

A ``json-document`` import names a row path such as ``days[].samples[]``: each ``[]``
crosses an array, and each element of the last one is a row. ``ijson`` events drive a
walk down that spine. Declared ancestor fields, of the document or of an element the
path crosses, are captured as they stream past, so they must come before the spine
array: one that comes after it is reported as a late ancestor. Only one row and the
captured ancestors are held at a time, so memory does not grow with the document.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import ijson

_BOM = b"\xef\xbb\xbf"
_LATE = "IMPORT_ANCESTOR_LATE"
_TOO_LARGE = "IMPORT_ROW_TOO_LARGE"
_TOO_DEEP = "IMPORT_ROW_TOO_DEEP"
# nosemgrep: ep-word-set -- ijson's basic_parse fixes these container event names.
_OPENS, _CLOSES = ("start_map", "start_array"), ("end_map", "end_array")


class Malformed(Exception):
    """The bytes are not one JSON document; nothing after this point can be read."""


@dataclass(slots=True)
class Found:
    """One row, or one row-level error, in document order."""

    value: Any = None
    index: int = 0
    scopes: tuple[dict, ...] = ()
    array: int = 0
    error: tuple[str, str] | None = None
    size: int = 0


class _Unmarked:
    """The byte stream without a leading UTF-8 byte-order mark."""

    def __init__(self, handle) -> None:
        self.handle, self.head = handle, None

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""  # ijson probes read(0) for the stream's type
        if self.head is None:
            head = self.handle.read(len(_BOM))
            self.head = b"" if head == _BOM else head
        if self.head:
            data, self.head = self.head, b""
            return data
        return self.handle.read(size)


class Rows:
    """Walk one document: ``routes[n]`` is the key path from the nth scope to the next array.

    Scope 0 is the document and scope n the current element of the nth crossed array;
    ``wanted[n]`` maps each declared key path within scope n to the mapping location
    that names it.
    """

    def __init__(
        self,
        handle,
        routes: tuple[tuple[str, ...], ...],
        wanted: tuple[Mapping[tuple[str, ...], str], ...],
        *,
        row_bytes: int,
        depth: int,
    ) -> None:
        self.events = ijson.basic_parse(_Unmarked(handle), use_float=True)
        self.routes, self.wanted = routes, wanted
        self.row_bytes, self.depth = row_bytes, depth
        self.scopes: list[dict] = [{} for _ in routes]
        self.arrays = 0

    def __iter__(self) -> Iterator[Found]:
        try:
            for event, value in self.events:
                yield from self._walk(event, value, 0, self.routes[0], self.wanted[0], ())
            # basic_parse refuses trailing values, so the document has ended here.
        except ijson.JSONError as error:
            raise Malformed from error

    def _walk(self, event, value, level, route, wanted, prefix):
        """One value inside scope ``level``; ``route`` is what remains of the spine from here.

        Returns whether the walk reached the spine array.
        """
        if not route:
            if event != "start_array":
                self._skip(event)
                return False
            yield from self._array(level)
            return True
        if event != "start_map":
            self._skip(event)
            return False
        passed = False
        for event, key in self.events:
            if event == "end_map":
                return passed
            event, value = next(self.events)
            here = (*prefix, key)
            beneath = {path: at for path, at in wanted.items() if path[: len(here)] == here}
            if key == route[0]:
                passed = (yield from self._walk(event, value, level, route[1:], beneath, here)) or passed
            elif not beneath:
                self._skip(event)
            elif passed:
                self._skip(event)
                for path in sorted(beneath):
                    yield Found(error=(_LATE, beneath[path]))
            else:
                yield from self._capture(event, value, level, beneath, here)

    def _array(self, level: int) -> Iterator[Found]:
        rows = level + 1 == len(self.routes)
        if rows:
            self.arrays += 1
        index = 0
        for event, value in self.events:
            if event == "end_array":
                return
            if rows:
                built, error, size = self._build(event, value)
                if error is not None:
                    yield Found(index=index, error=(error, "mapping.rows"), size=size)
                else:
                    yield Found(built, index, tuple(self.scopes), self.arrays, size=size)
            else:
                self.scopes[level + 1] = {}
                yield from self._walk(
                    event, value, level + 1, self.routes[level + 1], self.wanted[level + 1], ()
                )
            index += 1

    def _capture(self, event, value, level, wanted, here) -> Iterator[Found]:
        """Keep the declared ancestors at or beneath ``here``, the key path read so far."""
        if here in wanted:
            built, error, _ = self._build(event, value)
            if error is not None:
                yield Found(error=(error, wanted[here]))
                return
            for path in wanted:
                found = _descend(built, path[len(here) :])
                if found is not _MISSING:
                    self.scopes[level][path] = found
            return
        if event != "start_map":
            self._skip(event)
            return
        for event, key in self.events:
            if event == "end_map":
                return
            event, value = next(self.events)
            deeper = (*here, key)
            beneath = {path: at for path, at in wanted.items() if path[: len(deeper)] == deeper}
            if beneath:
                yield from self._capture(event, value, level, beneath, deeper)
            else:
                self._skip(event)

    def _skip(self, event: str) -> None:
        if event not in _OPENS:
            return
        depth = 1
        for event, _ in self.events:
            if event in _OPENS:
                depth += 1
            elif event in _CLOSES:
                depth -= 1
                if depth == 0:
                    return

    def _build(self, event: str, value: Any) -> tuple[Any, str | None, int]:
        """One whole value from its first event, its refusal if it breaks a bound, and its size.

        The size counts characters, not encoded bytes: it bounds what one row holds.
        """
        if event not in _OPENS:
            size = len(value) + 2 if type(value) is str else 8
            return (None, _TOO_LARGE, size) if size > self.row_bytes else (value, None, size)
        root: Any = {} if event == "start_map" else []
        stack, keys, size, error = [root], [None], 2, None
        for event, value in self.events:
            if event in _CLOSES:
                stack.pop()
                keys.pop()
                if not stack:
                    return (None, error, size) if error else (root, None, size)
                continue
            if event == "map_key":
                keys[-1] = value
                size += len(value) + 3
            elif event in _OPENS:
                child: Any = {} if event == "start_map" else []
                if error is None:
                    _attach(stack[-1], keys[-1], child)
                stack.append(child)
                keys.append(None)
                size += 2
                if error is None and len(stack) > self.depth:
                    error = _TOO_DEEP
            else:
                size += len(value) + 2 if type(value) is str else 8
                if error is None:
                    _attach(stack[-1], keys[-1], value)
            if error is None and size > self.row_bytes:
                error = _TOO_LARGE
            if error is not None:
                root = None  # stop holding a row that is refused anyway
        raise Malformed


_MISSING = object()


def _attach(container: Any, key: str | None, value: Any) -> None:
    if type(container) is dict:
        container[key] = value
    else:
        container.append(value)


def _descend(value: Any, path: tuple[str, ...]) -> Any:
    for part in path:
        if type(value) is not dict or part not in value:
            return _MISSING
        value = value[part]
    return value
