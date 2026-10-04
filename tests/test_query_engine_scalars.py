"""Exact typed keys must not inherit SQLite JSON1 affinity or text-date order."""

import math
import random
import sqlite3
import struct
from contextlib import closing
from decimal import Inexact, localcontext
from fractions import Fraction

import pytest

from exomem.collection_store.query_indexes import build_projection_plan
from exomem.query_engine.indexes import IndexKey, IndexSpec
from exomem.query_engine.scalars import ScalarValueError, scalar_key


def projection_order(rows, kind):
    with closing(sqlite3.connect(":memory:")) as conn:
        plan = build_projection_plan("2db90f18-70df-4e41-986e-2d7d7db1caca", {"value": {"type": kind}},
                                     (IndexSpec("value", (IndexKey("value"),)),))
        for sql in plan.ddl:
            conn.execute(sql)
        conn.executemany(plan.upsert_sql, ((n, str(n), 1, plan.encode(row)) for n, row in enumerate(rows)))
        return [row[0] for row in conn.execute(
            f"SELECT row_id FROM {plan.table_name} ORDER BY k0_tag,k0_key,item_key"
        )]


def test_adjacent_large_integers_do_not_collapse_to_real():
    """JSON1 REAL conversion must not reverse two distinct integers via a tie."""
    assert projection_order([{"value": 2**64 + 1}, {"value": 2**64}], "number") == [1, 0]


def test_utf8_order_includes_text_after_embedded_nul():
    """Strings with a common NUL prefix are still distinct binary strings."""
    assert projection_order([{"value": "a\0z"}, {"value": "a\0a"}], "string") == [1, 0]


def test_datetime_order_compares_instants_instead_of_authored_offset_text():
    """A later spelling's local date can describe an earlier UTC instant."""
    rows = [{"value": "2024-01-01T00:00:00+01:00"}, {"value": "2023-12-31T23:30:00Z"}]
    assert projection_order(rows, "datetime") == [0, 1]


def test_missing_and_present_null_have_distinct_comparison_keys():
    """JSON extraction's SQL NULL must not merge absence with present null."""
    assert projection_order([{"value": None}, {"value": ""}, {}], "string") == [2, 0, 1]


def test_numeric_index_order_matches_exact_fraction_oracle_under_small_decimal_context():
    """Neither binary64 conversion nor Decimal context can round distinct keys."""
    values = [-(10**400), -2**64 - 1, -2**64, -0.1, -5e-324, -0.0, 0, 0.0,
              5e-324, 0.1, 1, 1.0, 2**53, 2**53 + 1, float(2**53), 2**64, 2**64 + 1,
              math.nextafter(1.0, 0.0), math.nextafter(1.0, math.inf), 10**400]
    randomizer = random.Random(41)
    for _ in range(128):
        value = struct.unpack("!d", randomizer.getrandbits(64).to_bytes(8, "big"))[0]
        if math.isfinite(value):
            values.append(value)
    expected = sorted(range(len(values)), key=lambda n: (Fraction(values[n]), str(n)))
    with localcontext() as context:
        context.prec = 1
        context.traps[Inexact] = True
        assert projection_order([{"value": value} for value in values], "number") == expected
        assert scalar_key(0, "number") == scalar_key(-0.0, "number")
        assert scalar_key(1, "number") == scalar_key(1.0, "number")


def test_string_and_link_keys_preserve_binary_utf8_including_unicode_and_nul():
    """Typed text ordering performs no case folding or Unicode normalization."""
    values = ["😀", "\0", "a\0z", "a\0a", "é", "e\u0301", "", "Ω", "A", "a", "\U0010ffff"]
    expected = sorted(range(len(values)), key=lambda n: values[n].encode("utf-8"))
    for kind in ("string", "link"):
        assert projection_order([{"value": value} for value in values], kind) == expected


def test_enum_keys_do_not_merge_booleans_numbers_and_numeric_text():
    """Enums retain JSON type identity while int/float share mathematical equality."""
    assert scalar_key(True, "enum") != scalar_key(1, "enum") != scalar_key("1", "enum")
    assert scalar_key(1, "enum") == scalar_key(1.0, "enum")
    assert projection_order([{"value": "1"}, {"value": 1}, {"value": True}], "enum") == [2, 1, 0]


def test_dates_keep_calendar_identity_and_datetimes_keep_microseconds():
    """Far-future instants one microsecond apart cannot collapse through timestamps."""
    dates = ["2024-03-01", "2024-02-29", "0001-01-01", "9999-12-31"]
    assert projection_order([{"value": value} for value in dates], "date") == [2, 1, 0, 3]
    assert scalar_key("2024-01-01T00:00:00+01:00", "datetime") == scalar_key("2023-12-31T23:00:00Z", "datetime")
    instants = ["9999-12-31T23:59:59.000002Z", "9999-12-31T23:59:59.000001Z"]
    assert projection_order([{"value": value} for value in instants], "datetime") == [1, 0]


@pytest.mark.parametrize("value", ["2024-01-01T00:00:00.0000001Z", "2024-01-01T00:00:00+01:00:00.0000001"])
def test_sub_microsecond_precision_is_not_silently_truncated(value):
    """An unsupported instant must not become the index key of a different instant."""
    with pytest.raises(ScalarValueError):
        scalar_key(value, "datetime")


def test_zero_only_fractional_tail_keeps_exact_microseconds():
    assert scalar_key("2024-01-01T00:00:00.0000010000Z", "datetime") == scalar_key("2024-01-01T00:00:00.000001Z", "datetime")


@pytest.mark.parametrize("kind,value", [
    ("number", math.nan), ("number", math.inf), ("number", -math.inf), ("number", "1"),
    ("number", True), ("integer", 1.0), ("boolean", 1), ("enum", {}),
    ("date", "2023-02-29"), ("datetime", "2024-01-01T00:00:00"),
    ("datetime", "0001-01-01T00:00:00+01:00"), ("string", "\ud800"),
    ("datetime", "20240101T000000Z"), ("datetime", "20240101.1234567+00:00"),
    ("datetime", "2024-01-01T00:00:00+00:00:00.5"),
    ("datetime", "2024-01-01T00:00:00+00:60"),
])
def test_invalid_scalar_domain_is_refused(kind, value):
    """Invalid values cannot silently produce valid typed index keys."""
    with pytest.raises(ScalarValueError):
        scalar_key(value, kind)
