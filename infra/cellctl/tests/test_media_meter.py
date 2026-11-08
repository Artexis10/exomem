"""add-cloud-media-store D10: the operator's physical meter of a cell's prefix in the media bucket."""

from __future__ import annotations

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
HIDE_ALERT, OVER_ALERT, UNKNOWN_ALERT = alerts.media_alerts(CANARY)
MINUTE = timedelta(minutes=1)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """The media alerts delivered, as (name, active)."""

    delivered: list[tuple[str, bool]] = []

    def deliver(url: str, *, alert: str, active: bool, observed_at: object) -> None:
        if alert.startswith((alerts.MEDIA_HIDE_ALERT, alerts.MEDIA_OVER_ALLOWANCE_ALERT,
                             alerts.MEDIA_METER_UNKNOWN_ALERT)):
            delivered.append((alert, active))

    monkeypatch.setattr(alerts, "deliver", deliver)
    return delivered


def _listing(*versions: tuple[int | None, str]) -> FakeB2:
    """The cell's prefix as the provider lists it: each version as (size, action)."""
    listing = FakeB2()
    for index, (size, action) in enumerate(versions):
        listing.seed_object(f"{PREFIX}object-{index}", size=size, action=action)
    return listing


async def _new_backup(fleet: test_media_edge.Fleet, cell_id: str, at: object) -> None:
    """A backup finishes, so the next pass lists the cell's prefix again."""
    await fleet.connection.execute("UPDATE exomem_cloud_cells SET last_backup_at=$2 WHERE cell_id=$1", cell_id, at)


# -- the figure --


def test_the_figure_sums_upload_bytes_and_counts_unfinished_files_and_hide_entries_apart() -> None:
    figure = media_meter.meter_prefix(
        _listing((40, "upload"), (2, "upload"), (None, "hide"), (None, "start"), (None, "start")), CANARY)

    assert figure == PhysicalFigure(bytes=42, unfinished=2, hides=1)


def test_a_failed_page_gives_an_unknown_figure_never_a_partial_sum() -> None:
    listing = _listing((40, "upload"))
    listing.fail_listing_with = RuntimeError("page 2 was malformed")

    figure = media_meter.meter_prefix(listing, CANARY)

    assert figure is media_meter.UNKNOWN and figure.bytes is None and figure.hides is None


def test_an_upload_without_a_reported_size_makes_the_byte_sum_unknown_not_smaller() -> None:
    figure = media_meter.meter_prefix(_listing((40, "upload"), (None, "upload")), CANARY)

    assert figure.bytes is None and media_meter.over_allowance(figure, 10, 1.5) is None


# -- the pass --


async def test_a_hide_entry_alerts_at_once_and_resolves_when_a_later_listing_shows_none(cell_db, sent) -> None:
    fleet = await _fleet(cell_db, CANARY)
    _alerting(fleet)
    try:
        fleet.media_listing = _listing((40, "upload"), (None, "hide"))
        await fleet.run(NOW)
        assert sent == [(HIDE_ALERT, True)]

        fleet.media_listing = _listing((40, "upload"))
        await _new_backup(fleet, CANARY, NOW + MINUTE)
        await fleet.run(NOW + 2 * MINUTE)
        assert sent == [(HIDE_ALERT, True), (HIDE_ALERT, False)]
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
    fleet = await _fleet(cell_db, CANARY)
    _alerting(fleet)
    try:
        listing = fleet.media_listing = _listing((None, "hide"))
        await fleet.run(NOW)
        listing.fail_listing_with = RuntimeError("the provider is away")
        await _new_backup(fleet, CANARY, NOW + MINUTE)
        await fleet.run(NOW + MINUTE)
        await fleet.run(NOW + 2 * MINUTE)

        # An unknown listing is not a clean one: the alert stays, and the second pass does not list again.
        assert (sent, len(listing.listed), fleet.memory.media_physical[CANARY]) == (
            [(HIDE_ALERT, True)], 2, media_meter.UNKNOWN)

        listing.fail_listing_with = None
        listing._objects.clear()
        await fleet.run(NOW + 7 * MINUTE)
        assert sent == [(HIDE_ALERT, True), (HIDE_ALERT, False)]
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


async def test_a_deleted_cell_leaves_the_meter_but_its_hide_alert_stays(cell_db, sent, staging) -> None:
    # Deletion is no evidence that the hide entry is gone; only a listing that shows none would be.
    fleet = await _fleet(cell_db, CANARY)
    _alerting(fleet)
    try:
        listing = fleet.media_listing = _listing((None, "hide"))
        await fleet.run(NOW)
        assert sent == [(HIDE_ALERT, True)]

        await _delete(fleet, CANARY, NOW + MINUTE)

        assert sent == [(HIDE_ALERT, True)]
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
