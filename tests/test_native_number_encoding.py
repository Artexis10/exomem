"""Failures that would change persisted typed-v2 values or their decoding bound."""

import sqlite3
import struct
import sys

import pytest

from exomem.collection_store import native_numbers
from exomem.query_engine.scalars import ScalarValueError


def test_numeric_cells_keep_exact_subtype_and_bits_after_sqlite_reopen(tmp_path):
    values = [1, 1.0, 0.0, -0.0, 9007199254740993, -(2**63), 2**63,
              -(2**70), 2**70, 5e-324, sys.float_info.max, -sys.float_info.max,
              10**400 + 1]
    database = tmp_path / "numbers.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE numbers(ordinal INTEGER PRIMARY KEY, value ANY NOT NULL) STRICT")
        connection.executemany("INSERT INTO numbers VALUES (?,?)",
                               enumerate(native_numbers.encode(value) for value in values))
    with sqlite3.connect(database) as connection:
        restored = [native_numbers.decode(row[0], digit_budget=4300)
                    for row in connection.execute("SELECT value FROM numbers ORDER BY ordinal")]
    for expected, actual in zip(values, restored, strict=True):
        assert type(actual) is type(expected)
        if type(expected) is float:
            assert struct.pack(">d", actual) == struct.pack(">d", expected)
        else:
            assert actual == expected


def test_number_wire_v1_reads_existing_integer_and_float_frames():
    # Fixed v1 frames for +/-10**20 protect immutable history against a format change.
    positive = bytes.fromhex("000280000000000000140200")
    negative = bytes.fromhex("00007fffffffffffffebfdff")
    floating = bytes.fromhex("010280000000000000140200")
    assert native_numbers.decode(positive, digit_budget=21) == 10**20
    assert native_numbers.decode(negative, digit_budget=21) == -(10**20)
    result = native_numbers.decode(floating, digit_budget=21)
    assert type(result) is float and result == 1e20
    assert native_numbers.encode(10**20) == positive
    assert native_numbers.encode(-(10**20)) == negative
    assert native_numbers.encode(1e20) == floating


def test_overflow_decode_refuses_values_outside_the_callers_digit_budget():
    cell = native_numbers.encode(10**200)
    with pytest.raises(ScalarValueError):
        native_numbers.decode(cell, digit_budget=200)
    assert native_numbers.decode(cell, digit_budget=201) == 10**200


def test_malformed_float_frame_returns_encoding_error_instead_of_overflow():
    integer_frame = native_numbers.encode(10**400)
    malformed = bytes((native_numbers.FLOAT,)) + integer_frame[1:]
    with pytest.raises(ScalarValueError):
        native_numbers.decode(malformed, digit_budget=401)
