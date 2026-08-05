"""Frontmatter temporal values: parsing, canonical rendering, four-valued order.

Notes record `created`/`updated` at two precisions permanently: bare calendar
dates (historical, and anything hand-authored) and second-granularity UTC
timestamps (written going forward). A date-only value denotes *an unknown
instant within that day*, so ordering two values is not always decidable — the
comparison is four-valued and reports `INDETERMINATE` rather than guessing.
"""

from __future__ import annotations

import datetime as dt

import pytest

from exomem import temporal


# --- parse ------------------------------------------------------------------


def test_parse_bare_date_has_no_instant() -> None:
    """A date-only value carries its day and an explicitly unknown time."""
    moment = temporal.parse("2026-08-05")
    assert moment is not None
    assert moment.day == dt.date(2026, 8, 5)
    assert moment.instant is None
    assert not moment.precise


def test_parse_timestamp_carries_instant() -> None:
    moment = temporal.parse("2026-08-05T09:12:33Z")
    assert moment is not None
    assert moment.day == dt.date(2026, 8, 5)
    assert moment.instant == dt.datetime(2026, 8, 5, 9, 12, 33, tzinfo=dt.UTC)
    assert moment.precise


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-05T09:12:33Z",
        "2026-08-05T09:12:33+00:00",
        "2026-08-05 09:12:33+00:00",  # PyYAML's str() of a loaded datetime
        dt.datetime(2026, 8, 5, 9, 12, 33, tzinfo=dt.UTC),
    ],
)
def test_parse_accepts_every_serialization_of_one_instant(value: object) -> None:
    """T-form, space-form, Z, +00:00, and the live object all agree."""
    moment = temporal.parse(value)
    assert moment is not None
    assert moment.instant == dt.datetime(2026, 8, 5, 9, 12, 33, tzinfo=dt.UTC)


def test_parse_normalizes_offset_to_utc() -> None:
    """Knowledge time is comparable across machines, so offsets fold to UTC."""
    moment = temporal.parse("2026-08-05T12:12:33+03:00")
    assert moment is not None
    assert moment.instant == dt.datetime(2026, 8, 5, 9, 12, 33, tzinfo=dt.UTC)
    assert moment.day == dt.date(2026, 8, 5)


def test_parse_yaml_date_and_datetime_objects() -> None:
    """PyYAML auto-types unquoted frontmatter dates; both forms must land."""
    assert temporal.parse(dt.date(2026, 8, 5)) == temporal.Moment(dt.date(2026, 8, 5), None)
    naive = temporal.parse(dt.datetime(2026, 8, 5, 9, 12, 33))
    assert naive is not None
    assert naive.instant == dt.datetime(2026, 8, 5, 9, 12, 33, tzinfo=dt.UTC)


def test_parse_drops_sub_second_precision() -> None:
    """Second granularity is the contract; milliseconds are false precision."""
    moment = temporal.parse("2026-08-05T09:12:33.987654Z")
    assert moment is not None
    assert moment.instant == dt.datetime(2026, 8, 5, 9, 12, 33, tzinfo=dt.UTC)


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-date", "2026-13-01", 42, []])
def test_parse_rejects_unusable_values(value: object) -> None:
    assert temporal.parse(value) is None


# --- stamp / render_date ----------------------------------------------------


def test_stamp_renders_second_granularity_utc() -> None:
    value = dt.datetime(2026, 8, 5, 9, 12, 33, 987654, tzinfo=dt.UTC)
    assert temporal.stamp(value) == "2026-08-05T09:12:33Z"


def test_stamp_of_a_bare_date_stays_bare() -> None:
    """Precision is carried by the Python type: a date cannot gain a time."""
    assert temporal.stamp(dt.date(2026, 8, 5)) == "2026-08-05"


def test_stamp_converts_local_offsets_to_utc() -> None:
    local = dt.datetime(2026, 8, 5, 12, 12, 33, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    assert temporal.stamp(local) == "2026-08-05T09:12:33Z"


def test_stamp_treats_naive_datetimes_as_utc() -> None:
    assert temporal.stamp(dt.datetime(2026, 8, 5, 9, 12, 33)) == "2026-08-05T09:12:33Z"


def test_render_date_is_always_a_bare_day() -> None:
    """Paths and draft tokens stay day-granular whatever the clock supplies."""
    assert temporal.render_date(dt.date(2026, 8, 5)) == "2026-08-05"
    assert temporal.render_date(dt.datetime(2026, 8, 5, 23, 59, 59, tzinfo=dt.UTC)) == "2026-08-05"


def test_render_date_keeps_the_authors_own_day() -> None:
    """Filenames must not shift month when a local instant folds to UTC.

    `dt.date.today()` is local, and note paths have always used it. Stamping in
    UTC must not silently move a note written at 01:30 on Sep 1 (UTC+3) into
    the August folder, so `render_date` reads the day off the value as given.
    """
    late = dt.datetime(2026, 9, 1, 1, 30, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    assert temporal.render_date(late) == "2026-09-01"
    assert temporal.stamp(late) == "2026-08-31T22:30:00Z"


def test_stamp_round_trips_through_parse() -> None:
    for value in (dt.date(2026, 8, 5), dt.datetime(2026, 8, 5, 9, 12, 33, tzinfo=dt.UTC)):
        assert temporal.parse(temporal.stamp(value)) == temporal.parse(value)


def test_now_is_timezone_aware_and_second_granular() -> None:
    """One clock read, carrying the local offset explicitly."""
    now = temporal.now()
    assert now.tzinfo is not None
    assert now.utcoffset() is not None
    assert now.microsecond == 0


def test_now_stamps_as_utc() -> None:
    """Whatever the host offset, the written form is UTC — one standard."""
    assert temporal.stamp(temporal.now()).endswith("Z")


# --- compare: the four-valued order -----------------------------------------


def _m(value: str) -> temporal.Moment:
    moment = temporal.parse(value)
    assert moment is not None
    return moment


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        # The table from docs/handoff-note-timestamps.md, every row.
        ("2026-08-04", "2026-08-05T09:00:00Z", temporal.Order.BEFORE),
        ("2026-08-05", "2026-08-05T09:00:00Z", temporal.Order.INDETERMINATE),
        ("2026-08-05", "2026-08-05", temporal.Order.INDETERMINATE),
        ("2026-08-05T09:00:00Z", "2026-08-05T11:00:00Z", temporal.Order.BEFORE),
        # Mirrors, so the relation is antisymmetric.
        ("2026-08-05T09:00:00Z", "2026-08-04", temporal.Order.AFTER),
        ("2026-08-05T09:00:00Z", "2026-08-05", temporal.Order.INDETERMINATE),
        ("2026-08-05T11:00:00Z", "2026-08-05T09:00:00Z", temporal.Order.AFTER),
        # Two whole days never overlap, whatever their unknown times.
        ("2026-08-04", "2026-08-05", temporal.Order.BEFORE),
        ("2026-08-05", "2026-08-04", temporal.Order.AFTER),
        # A day strictly precedes a later day's instant, and vice versa.
        ("2026-08-05T23:59:59Z", "2026-08-06", temporal.Order.BEFORE),
        # Only two known instants can be equal.
        ("2026-08-05T09:00:00Z", "2026-08-05T09:00:00Z", temporal.Order.SAME),
    ],
)
def test_compare_four_valued(a: str, b: str, expected: temporal.Order) -> None:
    assert temporal.compare(_m(a), _m(b)) is expected


def test_compare_is_antisymmetric() -> None:
    """Swapping the arguments mirrors BEFORE/AFTER and preserves the rest."""
    mirror = {
        temporal.Order.BEFORE: temporal.Order.AFTER,
        temporal.Order.AFTER: temporal.Order.BEFORE,
        temporal.Order.SAME: temporal.Order.SAME,
        temporal.Order.INDETERMINATE: temporal.Order.INDETERMINATE,
    }
    values = ["2026-08-04", "2026-08-05", "2026-08-05T09:00:00Z", "2026-08-05T11:00:00Z"]
    for a in values:
        for b in values:
            assert temporal.compare(_m(b), _m(a)) is mirror[temporal.compare(_m(a), _m(b))]


def test_same_day_never_collapses_an_unknown_into_a_guess() -> None:
    """The load-bearing rule: a date-only value is not midnight."""
    assert temporal.compare(_m("2026-08-05"), _m("2026-08-05T00:00:00Z")) is (
        temporal.Order.INDETERMINATE
    )


# --- sort_key ---------------------------------------------------------------


def test_sort_key_orders_across_mixed_precision() -> None:
    values = ["2026-08-05T11:00:00Z", "2026-08-04", "2026-08-05", "2026-08-05T09:00:00Z"]
    ordered = sorted(values, key=lambda v: temporal.sort_key(_m(v)))
    assert ordered == [
        "2026-08-04",
        "2026-08-05",
        "2026-08-05T09:00:00Z",
        "2026-08-05T11:00:00Z",
    ]


def test_sort_key_is_total_where_compare_is_not() -> None:
    """UIs need a stable total order even where the true order is unknowable."""
    a, b = _m("2026-08-05"), _m("2026-08-05T09:00:00Z")
    assert temporal.compare(a, b) is temporal.Order.INDETERMINATE
    assert temporal.sort_key(a) < temporal.sort_key(b)
