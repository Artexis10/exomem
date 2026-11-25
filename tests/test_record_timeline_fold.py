"""The one span function and the effective-time rules it rests on (OpenSpec record-value-timeline 4.1, 4.2).

Each case builds a row's versions and facts by hand and names the failure only it catches. Writes through the
real writer are in `test_record_timeline_writes.py`. The values and transition ids are invented.
"""

from __future__ import annotations

import datetime as dt

import pytest

from exomem import structured_collections as collections
from exomem.collection_store import timeline

MISSING = timeline.MISSING


def version(number, values, *, stated=None, written="2026-03-20T12:00:00Z"):
    """Version `number` of one row, written by transaction `100 + number` with transition id `t<number>`."""
    return timeline.Version(row_version=number, txn_id=100 + number, transition_id=f"t{number}", commit_seq=number,
                            committed_at=written, stated=stated, values=values)


def timeline_of(points):
    return [(point.value, point.effective.text, point.basis, point.placement) for point in points]


def test_a_field_gains_a_point_only_where_a_version_changed_it():
    """A fold that gives every field a point on every version shows a restated value as a change, and merges
    the timelines of fields that one edit did not touch."""
    versions = [
        version(1, {"location": "Shelf A"}, stated="2026-03-01"),
        version(2, {"location": "Shelf B"}, stated="2026-03-07"),
        version(3, {"location": "Shelf B", "status": "lent"}, stated="2026-03-08"),
    ]

    line = timeline.fold(versions, [])

    assert timeline_of(line.live["location"]) == [("Shelf A", "2026-03-01", "stated", "current"),
                                                  ("Shelf B", "2026-03-07", "stated", "current")]
    # The first version gives the field it lacked an absent point; the third version gives it its value.
    assert timeline_of(line.live["status"]) == [(MISSING, "2026-03-01", "stated", "current"),
                                                ("lent", "2026-03-08", "stated", "current")]


def test_a_version_without_a_stated_time_takes_effect_when_it_was_written():
    """History written before timeline facts existed must read, every point with basis `written`."""
    line = timeline.fold([version(1, {"location": "Shelf A"}, written="2026-03-01T08:30:00Z")], [])

    assert timeline_of(line.live["location"]) == [("Shelf A", "2026-03-01T08:30:00Z", "written", "current")]


def test_a_date_and_an_instant_on_the_same_day_tie_at_the_coarser_precision():
    """Comparing order instants would put the date at 00:00, before the 09:00 change, and file a same-day
    edit as past."""
    date, morning = timeline.parse("2026-03-07"), timeline.parse("2026-03-07T09:00:00Z")

    assert timeline.compare(date, morning) == 0 == timeline.compare(morning, date)
    assert timeline.compare(timeline.parse("2026-03-07T08:00:00Z"), morning) == -1
    assert timeline.compare(timeline.parse("2026-03-06"), morning) == -1


def test_a_current_point_orders_no_earlier_than_the_point_it_follows():
    """A same-day date that sorts at its 00:00 lands before the 09:00 point it followed, so the field's
    latest point no longer holds the current value."""
    versions = [
        version(1, {"location": "Shelf A"}, stated="2026-03-01"),
        version(2, {"location": "Shelf B"}, stated="2026-03-07T09:00:00Z"),
        version(3, {"location": "Shelf C"}, stated="2026-03-07"),
    ]

    points = timeline.fold(versions, []).live["location"]

    assert [point.value for point in points] == ["Shelf A", "Shelf B", "Shelf C"]
    assert points[2].effective.text == "2026-03-07" and points[2].order[0] == points[1].order[0]
    assert timeline.value_at(points, timeline.parse("2026-03-07T10:00:00Z")) == "Shelf C"


def test_two_moves_on_one_date_keep_a_zero_length_span():
    """Dropping a span that starts and ends at one instant hides a move the row really made."""
    versions = [
        version(1, {"location": "Shelf A"}, stated="2026-03-01"),
        version(2, {"location": "Shelf B"}, stated="2026-03-07"),
        version(3, {"location": "Shelf C"}, stated="2026-03-07"),
    ]

    spans = timeline.spans(timeline.fold(versions, []).live["location"])

    assert [(span.value, span.previous, span.first) for span in spans] == [
        ("Shelf A", MISSING, True), ("Shelf B", "Shelf A", False), ("Shelf C", "Shelf B", False)]
    assert spans[1].start.order[0] == spans[1].end.order[0]
    assert spans[2].end is None


def test_a_correction_replaces_the_point_it_names():
    """A fold that keeps the replaced point shows a change from the wrong value to the corrected one."""
    versions = [
        version(1, {"location": "Shelf A"}, stated="2026-03-01"),
        version(2, {"location": "Shelf B"}, stated="2026-03-07"),
        version(3, {"location": "Shelf C"}),  # the correction states no time of its own
    ]
    correction = timeline.Fact("location", txn_id=103, row_version=3, placement="current",
                               effective_at="2026-03-07", value=MISSING, corrects_txn=102)

    line = timeline.fold(versions, [correction])

    assert timeline_of(line.live["location"]) == [("Shelf A", "2026-03-01", "stated", "current"),
                                                  ("Shelf C", "2026-03-07", "stated", "current")]
    assert line.live["location"][1].corrects == "t2"
    assert line.replaced_by[("location", 102)] is line.live["location"][1]
    assert line.recorded[("location", 102)].value == "Shelf B"  # the replaced point stays in history
    assert [span.previous for span in timeline.spans(line.live["location"])] == [MISSING, "Shelf A"]


def test_equal_neighbouring_points_merge_into_one_span():
    """A past point that equals the value after it is one stay, not a change to the same value."""
    versions = [
        version(1, {"location": "Shelf A"}, stated="2026-03-01"),
        version(2, {"location": "Shelf B"}, stated="2026-03-07"),
        version(3, {"location": "Shelf B"}, stated="2026-03-05"),
    ]
    filled_in = timeline.Fact("location", txn_id=103, row_version=3, placement="past",
                              effective_at="2026-03-05", value="Shelf B", corrects_txn=None)

    line = timeline.fold(versions, [filled_in])
    spans = timeline.spans(line.live["location"])

    assert [point.effective.text for point in line.live["location"]] == ["2026-03-01", "2026-03-05", "2026-03-07"]
    assert [(span.value, span.start.effective.text, span.end) for span in spans] == [
        ("Shelf A", "2026-03-01", line.live["location"][1]), ("Shelf B", "2026-03-05", None)]


NOW = dt.datetime(2026, 3, 10, 23, 30, tzinfo=dt.UTC)


@pytest.mark.parametrize(("value", "accepted"), [
    ("2026-03-11", True),  # a writer ahead of UTC names its own today
    ("2026-03-12", False),
    ("2026-03-10T23:35:00Z", True),  # the clock-skew allowance
    ("2026-03-10T23:35:01Z", False),
    ("2026-03-11T01:00:00+02:00", True),  # 23:00 UTC, before the write
    ("2026-03-07T09:00:00", False),  # an instant without its offset
    ("2026-3-7", False),
])
def test_an_effective_time_may_lie_ahead_only_by_the_skew_or_into_the_next_utc_day(value, accepted):
    """A bound off by one refuses a writer east of UTC its own today, or lets intended future state in."""
    if accepted:
        assert timeline.stated(value, now=NOW).text == timeline.parse(value).text
        return
    with pytest.raises(collections.CollectionError) as refused:
        timeline.stated(value, now=NOW)
    assert refused.value.code == "EFFECTIVE_AT_INVALID" and "Planning" in refused.value.details["remediation"]
