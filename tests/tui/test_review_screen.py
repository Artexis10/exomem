"""Review: the queue, in-place triage receipts, snooze, context, empty state."""

from __future__ import annotations

import datetime as _dt

import pytest

pytest.importorskip("textual")

from _fake import FakeBackend  # noqa: E402

pytestmark = pytest.mark.anyio


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _open(app, pilot):
    await _settle(app, pilot)
    app.goto("review")
    await _settle(app, pilot)


def _text(app, selector: str) -> str:
    return str(app.screen.query_one(selector).render())


def _rows(app) -> str:
    options = app.screen.query_one("#review-list")
    return "\n".join(
        str(options.get_option_at_index(index).prompt) for index in range(options.option_count)
    )


async def test_queue_leads_with_the_measured_reason(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        options = app.screen.query_one("#review-list")
        assert options.display is True and options.option_count == 2
        rows = _rows(app)
        assert "contradiction" in rows
        assert "a newer note reaches the opposite conclusion" in rows
        assert "unprocessed" in rows
        view = _text(app, "#review-view")
        assert view.startswith("  view open")
        assert "2 of 2 shown" in view and "v cycles" in view


async def test_studio_is_a_pointer_not_an_affordance(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        studio = " ".join(_text(app, "#review-studio").split())
        assert "Review Studio" in studio
        assert "exomem serve http" in studio and "/studio/" in studio


async def test_dismiss_leaves_a_struck_receipt_in_place(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        await pilot.press("d")
        await _settle(app, pilot)
        assert fake_backend.triaged and fake_backend.triaged[0]["action"] == "dismiss"
        assert fake_backend.triaged[0]["expected_fingerprint"] == "fp-aa", (
            "triage must be bound to the fingerprint it was shown for"
        )
        options = app.screen.query_one("#review-list")
        assert options.option_count == 2, "the item stays where it was"
        rows = _rows(app)
        assert "dismissed" in rows and "o reopens" in rows
        assert "1 triaged this session" in _text(app, "#review-view")


async def test_reopen_clears_the_session_receipt(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        await pilot.press("d")
        await _settle(app, pilot)
        await pilot.press("o")
        await _settle(app, pilot)
        assert [entry["action"] for entry in fake_backend.triaged] == ["dismiss", "reopen"]
        assert "dismissed" not in _rows(app)


async def test_snooze_offers_only_dates_the_backend_can_honor(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        await pilot.press("s")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "SnoozeModal"
        body = " ".join(str(child.render()) for child in app.screen.query("Static"))
        assert "fingerprint" in body and "returns early" in body
        assert "nothing recorded yet" in body
        assert "after the next sweep" not in body, (
            "an option the triage contract cannot express would be a fake affordance"
        )
        await pilot.press("enter")  # tomorrow
        await _settle(app, pilot)
        snoozed = [entry for entry in fake_backend.triaged if entry["action"] == "snooze"]
        assert snoozed
        assert snoozed[0]["until"] == (_dt.date.today() + _dt.timedelta(days=1)).isoformat()


async def test_snooze_escape_records_nothing(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("escape")
        await _settle(app, pilot)
        assert not fake_backend.triaged
        assert app.screen.SCREEN_TITLE == "Review"


async def test_context_pane_shows_what_was_measured(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(app, pilot)
        await pilot.press("enter")
        await _settle(app, pilot)
        assert any(call[0] == "item_context" for call in fake_backend.calls)
        detail = app.screen.query_one("#review-detail")
        assert detail.has_class("has-content")
        body = _text(app, "#review-detail-body")
        assert "fingerprint" in body
        assert "what" in body
        assert "d dismiss" in body and "studio" in body


async def test_narrow_layout_opens_context_full_screen(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app.screen.__class__.__name__ == "DetailModal"


async def test_view_cycles_through_states(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        await pilot.press("v")
        await _settle(app, pilot)
        assert "view snoozed" in _text(app, "#review-view")
        states = [call[1]["state"] for call in fake_backend.calls if call[0] == "attention"]
        assert "snoozed" in states


async def test_empty_queue_carries_the_doctrine_and_two_ways_on(make_app):
    app = make_app(FakeBackend(attention={"items": [], "shown": 0, "total": 0}))
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        assert app.screen.query_one("#review-list").display is False
        recovery = app.screen.query_one("#review-recovery")
        assert recovery.has_class("visible")
        assert "● clear" in str(recovery.query_one("#recovery-head").render())
        assert "never act on their own" in str(recovery.query_one("#recovery-fact").render())
        assert recovery.query_one("#recovery-options").option_count == 2


async def test_stale_fingerprint_refreshes_instead_of_writing(make_app, fake_backend):
    fake_backend.fail_next(
        "triage", code="REVIEW_ITEM_CHANGED", message="the item changed since it was listed"
    )
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        before = len([call for call in fake_backend.calls if call[0] == "attention"])
        await pilot.press("d")
        await _settle(app, pilot)
        after = len([call for call in fake_backend.calls if call[0] == "attention"])
        assert after > before, "a stale triage must re-measure, not overwrite"
        assert "dismissed" not in _rows(app)
