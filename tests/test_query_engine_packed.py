"""A packed read returns each cell exactly as a cursor would, or a stored value changes on read."""

import sqlite3

import pytest

from exomem.query_engine import packed

#: Cells whose JSON text form differs from what a cursor returns.
TRICKY = [
    # REAL precision, subnormals, the sign of zero, integral reals
    0.1, 0.30000000000000004, 1 / 3, 1e308, 5e-324, -0.0, 0.0, 2.0,
    9223372036854775807, -9223372036854775808,  # INTEGER extremes
    b"\x00\xff", b"",  # BLOBs
    "[1,2]", '{"a":1}', "é\"\\\n\u2028", "",  # text that looks like JSON, escapes, the empty string
    None,
]


@pytest.mark.parametrize("value", TRICKY, ids=repr)
def test_each_storage_class_round_trips_exactly(value):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(n INTEGER, v ANY) STRICT")
    conn.executemany("INSERT INTO t VALUES(?,?)", [(1, value), (2, "plain")])
    sql = "SELECT n, v, json('[3]'), json_extract('{\"a\":{\"b\":1}}', '$.a') FROM t WHERE n >= ?"
    expected = conn.execute(sql, (0,)).fetchall()
    got = packed.rows(conn, sql, (0,), 4)
    assert sorted(got) == sorted(expected)
    assert [tuple(type(cell) for cell in row) for row in sorted(got)] == \
        [tuple(type(cell) for cell in row) for row in sorted(expected)]
    if value == 0.0:
        assert str(next(row for row in got if row[0] == 1)[1]) == str(value)


def test_a_row_wider_than_the_function_argument_floor_reads_whole():
    """SQLite before 3.48 caps a function at 127 arguments; a wide row must not hit that cap."""
    conn = sqlite3.connect(":memory:")
    conn.setlimit(sqlite3.SQLITE_LIMIT_FUNCTION_ARG, 127)
    width = 300
    sql = "SELECT " + ",".join(f"{index} AS c{index}" for index in range(width))
    assert packed.rows(conn, sql, (), width) == conn.execute(sql).fetchall()


def test_a_result_past_the_length_limit_is_read_in_smaller_slices():
    """A packed result past SQLITE_LIMIT_LENGTH must split, not fail or truncate."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(k INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t VALUES(?,?)", [(key, "x" * 1000) for key in range(64)])
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 20_000)
    statements = []

    def read(part):
        statements.append(len(part))
        sql = f"SELECT k, v FROM t WHERE k IN ({','.join('?' * len(part))})"
        return packed.rows(conn, sql, part, 2)

    keys = list(range(64))
    found = packed.in_chunks(read, keys, 64)
    assert sorted(found) == [(key, "x" * 1000) for key in keys]
    assert statements[0] == 64 and max(statements[1:]) < 64
