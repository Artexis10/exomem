"""add-cloud-media-store D10 and collect-released-media D4: the operator's physical meter of a cell's prefix."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from cellctl import alerts, main, media_meter
from cellctl.media_meter import PhysicalFigure
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.interface import cell_object_prefix

from . import test_media_edge, test_reconcile
from .test_media_edge import CANARY, NOW, OTHER, _alerting, _delete, _fleet

# The reconcile tests' staging bucket, which a deletion reads.
staging = test_reconcile.staging

PREFIX = cell_object_prefix(CANARY)
OVER_ALERT, UNKNOWN_ALERT = alerts.media_alerts(CANARY)
MINUTE = timedelta(minutes=1)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """The media alerts delivered, as (name, active). Every alert whose name starts with "media-" counts, so a
    per-cell alert on hides would show here."""

    delivered: list[tuple[str, bool]] = []

    def deliver(url: str, *, alert: str, active: bool, observed_at: object) -> None:
        if alert.startswith("media-"):
            delivered.append((alert, active))

    monkeypatch.setattr(alerts, "deliver", deliver)
    return delivered


def _listing(*versions: tuple[int | None, str]) -> FakeB2:
    """The cell's prefix as the provider lists it: each version as (size, action)."""
    listing = FakeB2()
    for index, (size, action) in enumerate(versions):
        listing.seed_object(f"{PREFIX}object-{index}", size=size, action=action)
    return listing


def _named(*versions: tuple[str, int | None, str]) -> FakeB2:
    """The cell's prefix as the provider lists it, in name order: each version as (name, size, action)."""
    listing = FakeB2()
    for name, size, action in versions:
        listing.seed_object(f"{PREFIX}{name}", size=size, action=action)
    return listing


async def _new_backup(fleet: test_media_edge.Fleet, cell_id: str, at: object) -> None:
    """A backup finishes, so the next pass lists the cell's prefix again."""
    await fleet.connection.execute("UPDATE exomem_cloud_cells SET last_backup_at=$2 WHERE cell_id=$1", cell_id, at)


# -- the figure --


def test_the_figure_sums_upload_bytes_and_counts_unfinished_files_and_lists_hidden_names_apart() -> None:
    figure = media_meter.meter_prefix(
        _named(("a", 40, "upload"), ("b", 2, "upload"), ("b", None, "hide"), ("c", None, "start"),
               ("d", None, "start")), CANARY)

    assert figure == PhysicalFigure(bytes=42, unfinished=2, hidden=(f"{PREFIX}b",), repeated=0)


@pytest.mark.parametrize(("versions", "repeated"), [
    pytest.param([("a", 1, "upload"), ("b", 1, "upload"), ("b", None, "hide")], 0, id="every name has one upload"),
    pytest.param([("a", 1, "upload"), ("a", 1, "upload"), ("b", 1, "upload")], 1, id="one name has two"),
    pytest.param([("a", 1, "upload"), ("a", 1, "upload"), ("a", 1, "upload"), ("b", 1, "upload"),
                  ("b", 1, "upload"), ("c", None, "start"), ("c", 1, "upload")], 2, id="several names have more"),
])
def test_the_figure_counts_each_name_with_more_than_one_upload_version_once(versions, repeated) -> None:
    """The lifecycle rule deletes a repeated name's older version, which a sidecar may still pin; the operator
    re-offloads those originals before the rule applies, so the count must not miss one or count one twice."""
    assert media_meter.meter_prefix(_named(*versions), CANARY).repeated == repeated


def test_a_listing_out_of_name_order_makes_the_repeated_count_unknown_not_low() -> None:
    """A name's versions split across the listing would each look single, and a low count would pass the gate."""
    figure = media_meter.meter_prefix(_named(("b", 1, "upload"), ("a", 1, "upload")), CANARY)

    assert figure.repeated is None and figure.bytes == 2


def test_a_failed_page_gives_an_unknown_figure_never_a_partial_sum() -> None:
    listing = _listing((40, "upload"))
    listing.fail_listing_with = RuntimeError("page 2 was malformed")

    figure = media_meter.meter_prefix(listing, CANARY)

    assert figure is media_meter.UNKNOWN and figure.bytes is None and figure.repeated is None


def test_an_upload_without_a_reported_size_makes_the_byte_sum_unknown_not_smaller() -> None:
    figure = media_meter.meter_prefix(_listing((40, "upload"), (None, "upload")), CANARY)

    assert figure.bytes is None and media_meter.over_allowance(figure, 10, 1.5) is None


# -- the pass --


async def test_the_log_lists_hidden_names_and_counts_repeated_ones_and_a_hide_raises_no_alert(
        cell_db, sent, caplog) -> None:
    """Every owner deletion now ends in a hide: an alert on each would page the operator for normal work."""
    fleet = await _fleet(cell_db, CANARY)
    _alerting(fleet)
    try:
        fleet.media_listing = _named(("kept", 40, "upload"), ("kept", 40, "upload"), ("released", 2, "upload"),
                                     ("released", None, "hide"))
        with caplog.at_level(logging.INFO, logger="cellctl"):
            await fleet.run(NOW)
        report = next(record.getMessage() for record in caplog.records if "hidden name(s)" in record.getMessage())

        assert sent == []
        assert f"cell {CANARY} holds 82 media byte(s)" in report
        assert "1 name(s) with more than one upload version" in report
        assert report.endswith(f"1 hidden name(s) in the bucket: {PREFIX}released")
    finally:
        await fleet.close()


@pytest.mark.parametrize(
    ("headroom", "bytes_held", "fires"),
    [(1.5, 150, False), (1.5, 151, True), (None, 10**15, False)],
    ids=["at the limit", "past the limit", "headroom not configured"],
)
async def test_a_prefix_past_the_allowance_times_the_headroom_alerts(cell_db, sent, headroom, bytes_held, fires) -> None:
    fleet = await _fleet(cell_db, CANARY, store_policy={"allowance_bytes": 100}, physical_headroom=headroom)
    _alerting(fleet)
    try:
        fleet.media_listing = _listing((bytes_held, "upload"))
        await fleet.run(NOW)

        assert sent == ([(OVER_ALERT, True)] if fires else [])
    finally:
        await fleet.close()


async def test_a_failed_listing_resolves_nothing_and_is_tried_again_after_five_minutes(cell_db, sent) -> None:
    fleet = await _fleet(cell_db, CANARY, store_policy={"allowance_bytes": 100}, physical_headroom=1.5)
    _alerting(fleet)
    try:
        listing = fleet.media_listing = _listing((151, "upload"))
        await fleet.run(NOW)
        listing.fail_listing_with = RuntimeError("the provider is away")
        await _new_backup(fleet, CANARY, NOW + MINUTE)
        await fleet.run(NOW + MINUTE)
        await fleet.run(NOW + 2 * MINUTE)

        # An unknown listing is not within the allowance: the alert stays, and the second pass does not list again.
        assert (sent, len(listing.listed), fleet.memory.media_physical[CANARY]) == (
            [(OVER_ALERT, True)], 2, media_meter.UNKNOWN)

        listing.fail_listing_with = None
        listing._objects.clear()
        await fleet.run(NOW + 7 * MINUTE)
        assert sent == [(OVER_ALERT, True), (OVER_ALERT, False)]
    finally:
        await fleet.close()


async def test_a_figure_unknown_for_three_listings_in_a_row_alerts_and_a_good_listing_resolves_it(cell_db, sent) -> None:
    # A revoked list key fails every listing; without this alert it would switch off both abuse alerts.
    fleet = await _fleet(cell_db, CANARY)
    _alerting(fleet)
    try:
        listing = fleet.media_listing = _listing((40, "upload"))
        listing.fail_listing_with = RuntimeError("the list key was revoked")
        await fleet.run(NOW)
        await fleet.run(NOW + 5 * MINUTE)
        assert sent == []

        await fleet.run(NOW + 10 * MINUTE)
        assert (sent, len(listing.listed)) == ([(UNKNOWN_ALERT, True)], 3)

        listing.fail_listing_with = None
        await fleet.run(NOW + 15 * MINUTE)
        assert sent == [(UNKNOWN_ALERT, True), (UNKNOWN_ALERT, False)]
    finally:
        await fleet.close()


async def test_a_missing_list_key_counts_as_a_failed_listing_and_alerts_after_three_tries(cell_db, sent) -> None:
    # A missing key switches off both abuse alerts just as a revoked one does, so it must not stay quiet.
    fleet = await _fleet(cell_db, CANARY)
    _alerting(fleet)
    try:
        fleet.media_listing = None
        await fleet.run(NOW)
        await fleet.run(NOW + 5 * MINUTE)
        assert sent == []

        await fleet.run(NOW + 10 * MINUTE)
        assert (sent, fleet.memory.media_physical[CANARY]) == ([(UNKNOWN_ALERT, True)], media_meter.UNKNOWN)
    finally:
        await fleet.close()


async def test_one_failed_listing_ends_the_passs_metering_so_a_provider_outage_costs_one_timeout(cell_db, sent) -> None:
    fleet = await _fleet(cell_db, CANARY, OTHER, cell_ids=(CANARY, OTHER))
    try:
        listing = fleet.media_listing = _listing((40, "upload"))
        listing.fail_listing_with = RuntimeError("the provider is away")
        await fleet.run(NOW)
        assert listing.listed == [PREFIX]

        await fleet.run(NOW + MINUTE)
        assert listing.listed == [PREFIX, cell_object_prefix(OTHER)]
    finally:
        await fleet.close()


async def test_a_cell_is_listed_once_per_backup_and_only_by_its_own_prefix(cell_db, sent) -> None:
    fleet = await _fleet(cell_db, CANARY, OTHER)
    try:
        listing = fleet.media_listing = _listing((40, "upload"))
        await fleet.run(NOW)
        await fleet.run(NOW + MINUTE)
        assert listing.listed == [PREFIX]

        await _new_backup(fleet, CANARY, NOW + 2 * MINUTE)
        await fleet.run(NOW + 3 * MINUTE)
        assert listing.listed == [PREFIX, PREFIX]
    finally:
        await fleet.close()


async def test_a_deleted_cell_leaves_the_meter_and_its_allowance_alert_resolves(cell_db, sent, staging) -> None:
    # A deleted cell is listed no more, so no later listing could resolve its alert.
    fleet = await _fleet(cell_db, CANARY, store_policy={"allowance_bytes": 100}, physical_headroom=1.5)
    _alerting(fleet)
    try:
        listing = fleet.media_listing = _listing((151, "upload"))
        await fleet.run(NOW)
        assert sent == [(OVER_ALERT, True)]

        await _delete(fleet, CANARY, NOW + MINUTE)

        assert sent == [(OVER_ALERT, True), (OVER_ALERT, False)]
        assert CANARY not in fleet.memory.media_physical and len(listing.listed) == 1
    finally:
        await fleet.close()


def test_cellctl_starts_without_the_list_key_and_says_so_once(monkeypatch, caplog) -> None:
    # A release can reach the cluster before its list-key Secret; the meter is a report, so cellctl must start.
    monkeypatch.setenv("CELLCTL_MEDIA_EDGE_URL", "http://10.43.0.30:8769")
    monkeypatch.setenv("CELLCTL_MEDIA_BUCKET_ID", "4a48fe8875c6214145260818")
    monkeypatch.delenv("CELLCTL_MEDIA_LIST_KEY_ID", raising=False)
    monkeypatch.delenv("CELLCTL_MEDIA_LIST_APPLICATION_KEY", raising=False)

    assert main.build_media_listing() is None
    assert [record.levelname for record in caplog.records if record.name == "cellctl"] == ["WARNING"]
