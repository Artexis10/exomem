"""Read many rows of one statement in a single step.

CPython's sqlite3 releases and re-takes the GIL around every statement step. Beside a busy
in-process thread each re-take costs milliseconds, so a scan that steps once per row spends its
time waiting, not reading. A packed read asks SQLite for one JSON array holding every row and
decodes it in Python, which is one step per chunk of rows.

The transport is exact: each cell travels as its own storage class (a closed set SQLite
defines), and a cell JSON cannot carry exactly travels as text that Python decodes back.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence

#: Cells per inner ``json_array``: SQLite before 3.48 caps a function at 127 arguments, and a wide
#: typed row has more cells than that.
_GROUP = 100


def _cell(name: str) -> str:
    # A REAL travels as text that round-trips every double; '%!.17g' drops the sign of zero, so a
    # zero is flagged and its chunk is read again unpacked. A BLOB travels as hex.
    return (f"CASE typeof({name}) WHEN 'real' THEN printf('%!.17g',{name}) "
            f"WHEN 'blob' THEN hex({name}) ELSE {name} END")


def _signature(name: str) -> str:
    # One letter per cell: the first letter of its storage class, or 'z' for a REAL zero.
    return (f"CASE WHEN typeof({name})='real' AND {name}=0 THEN 'z' "
            f"ELSE substr(typeof({name}),1,1) END")


def rows(conn: sqlite3.Connection, sql: str, parameters: Sequence, width: int) -> list[tuple]:
    """The rows ``sql`` returns, each exactly as a cursor returns it, in no particular order.

    ``width`` is the number of result columns of ``sql``. SQLITE_TOOBIG reaches the caller, which
    reads fewer rows per statement.
    """
    names = [f"c{index}" for index in range(width)]
    signature = "||".join(_signature(name) for name in names)
    groups = ",".join("json_array(" + ",".join(map(_cell, names[start:start + _GROUP])) + ")"
                      for start in range(0, width, _GROUP))
    # Internal SQL with bound parameters only.
    packed = (f"WITH s({','.join(names)}) AS ({sql}) "  # noqa: S608
              f"SELECT json_group_array(json_array({signature},{groups})) FROM s")
    (text,) = conn.execute(packed, tuple(parameters)).fetchone()
    decoded = []
    for kinds, *parts in json.loads(text):
        values = parts[0] if len(parts) == 1 else [value for part in parts for value in part]
        # Most rows hold no REAL or BLOB; three substring tests are cheaper than a walk per cell.
        if "r" in kinds or "b" in kinds or "z" in kinds:
            if "z" in kinds:
                return conn.execute(sql, tuple(parameters)).fetchall()
            for index, kind in enumerate(kinds):
                if kind == "r":
                    values[index] = float(values[index])
                elif kind == "b":
                    values[index] = bytes.fromhex(values[index])
        decoded.append(tuple(values))
    return decoded


def too_big(error: sqlite3.Error) -> bool:
    """Whether a packed read failed only because its one result passed the length limit."""
    return getattr(error, "sqlite_errorcode", 0) & 255 == sqlite3.SQLITE_TOOBIG


def in_chunks(read: Callable[[Sequence], list], keys: Sequence, size: int) -> list:
    """``read`` over consecutive slices of ``keys``, halving a slice whose result is too big."""
    found, start = [], 0
    while start < len(keys):
        part = keys[start:start + size]
        try:
            found.extend(read(part))
        except sqlite3.Error as error:
            if not too_big(error) or size == 1:
                raise
            size //= 2
            continue
        start += len(part)
    return found
