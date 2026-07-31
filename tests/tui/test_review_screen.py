"""Review queue: honest triage trio, fingerprints, stale refresh, empty state."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from _fake import FakeBackend  # noqa: E402

pytestmark = pytest.mark.anyio


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _open_review(app, pilot):
    await _settle(app, pilot)
    app.goto("review")
    await _settle(app, pilot)


async def test_queue_lists_items_with_counts(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_review(app, pilot)
        options = app.screen.query_one("#review-list")
        assert options.display is True
        assert options.option_count == 2
        summary = str(app.screen.query_one("#review-summary").render())
        assert "2" in summary


async def test_dismiss_sends_fingerprint_and_refreshes(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_review(app, pilot)
        await pilot.press("d")
        await _settle(app, pilot)
        assert fake_backend.triaged, "dismiss must call triage"
        call = fake_backend.triaged[0]
        assert call["action"] == "dismiss"
        assert call["expected_fingerprint"] == "fp-aa"
        attention_calls = [c for c in fake_backend.calls if c[0] == "attention"]
        assert len(attention_calls) >= 2, "queue must refresh after triage"


async def test_snooze_requires_date(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_review(app, pilot)
        await pilot.press("s")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "SnoozeModal"
        await pilot.press("enter")  # suggested date is prefilled and valid
        await _settle(app, pilot)
        call = fake_backend.triaged[0]
        assert call["action"] == "snooze"
        assert call["until"]


async def test_stale_fingerprint_refreshes_not_retries(make_app, fake_backend):
    fake_backend.fail_next("triage", code="REVIEW_ITEM_CHANGED", message="changed")
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_review(app, pilot)
        before = len([c for c in fake_backend.calls if c[0] == "attention"])
        await pilot.press("d")
        await _settle(app, pilot)
        after = len([c for c in fake_backend.calls if c[0] == "attention"])
        assert after == before + 1, "stale triage must refresh the queue once"
        assert len(fake_backend.triaged) == 0, "the stale action must not be retried"


async def test_item_context_renders_in_detail(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_review(app, pilot)
        await pilot.press("enter")
        await _settle(app, pilot)
        assert any(call[0] == "item_context" for call in fake_backend.calls)
        detail = app.screen.query_one("#ask-detail")
        assert detail.has_class("has-content")


async def test_empty_queue_state(make_app):
    backend = FakeBackend(
        attention={"items": [], "shown": 0, "total": 0, "all_total": 0, "state_summary": {}}
    )
    app = make_app(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_review(app, pilot)
        assert app.screen.query_one("#review-empty").display is True
        assert app.screen.query_one("#review-list").display is False
