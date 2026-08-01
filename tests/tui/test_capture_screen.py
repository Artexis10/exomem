"""Capture: compose, kind, the governed relation review, receipt, recovery."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

pytestmark = pytest.mark.anyio


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _open(app, pilot):
    await _settle(app, pilot)
    app.goto("capture")
    await _settle(app, pilot)


def _text(app, selector: str) -> str:
    return str(app.screen.query_one(selector).render())


async def test_compose_surface_states_the_promise_and_derives_a_title(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        assert "preserved as-is" in _text(app, "#capture-promise")
        assert app.focused.id == "capture-content"
        app.screen.query_one("#capture-content").text = "reranker cutoffs"
        await pilot.pause()
        title_line = _text(app, "#capture-title-line")
        assert "reranker cutoffs" in title_line
        assert "e edits" in title_line
        assert "\n" not in title_line, "the title row must fit on one line"
        kind = _text(app, "#capture-kind")
        assert "(●) thought" in kind and "(○) insight" in kind


async def test_tab_cycles_the_kind_without_leaving_the_text(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        await pilot.press("tab")
        await pilot.pause()
        kind = _text(app, "#capture-kind")
        assert "(○) thought" in kind and "(●) insight" in kind
        assert app.focused.id == "capture-content", "tab must not steal focus"


async def test_thought_saves_through_the_source_path(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        app.screen.query_one("#capture-content").text = "a raw thought worth keeping"
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert fake_backend.captured, "a thought is an immutable Source"
        receipt = _text(app, "#capture-receipt")
        assert "✓ saved" in receipt and "thought" in receipt
        next_options = app.screen.query_one("#capture-next")
        assert next_options.display is True and next_options.option_count == 3


async def test_saved_receipt_can_ask_for_the_note_it_just_wrote(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(app, pilot)
        app.screen.query_one("#capture-content").text = "queue backpressure needs limits"
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        await pilot.press("enter")  # 'Ask for it — see it cited'
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "Ask"
        asked = [call for call in fake_backend.calls if call[0] == "ask"]
        assert asked and "queue backpressure" in asked[-1][1]["query"]


async def test_unlinked_insight_asks_the_governed_question(make_app, fake_backend):
    fake_backend.require_relation_review = True
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        app.screen.query_one("#capture-content").text = "bounded queues shed load predictably"
        await pilot.press("tab")  # thought -> insight
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert app.screen.__class__.__name__ == "ConfirmModal"
        body = " ".join(str(child.render()) for child in app.screen.query("Static"))
        assert "No typed relation" in body
        assert "governed review — recorded, not skipped" in body
        assert "nothing saved yet" in body
        assert not fake_backend.remembered


async def test_declining_the_governed_question_writes_nothing(make_app, fake_backend):
    fake_backend.require_relation_review = True
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        app.screen.query_one("#capture-content").text = "bounded queues shed load"
        await pilot.press("tab")
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        await pilot.press("down")  # 'Back to editing'
        await pilot.press("enter")
        await _settle(app, pilot)
        assert not fake_backend.remembered
        assert "not saved" in _text(app, "#capture-receipt")
        assert app.screen.query_one("#capture-content").text.strip() == "bounded queues shed load"


async def test_confirming_records_the_review_and_says_so(make_app, fake_backend):
    fake_backend.require_relation_review = True
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        app.screen.query_one("#capture-content").text = (
            "bounded queues shed load\n\n## Observations\n- [finding] limits beat hope\n"
        )
        await pilot.press("tab")
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        await pilot.press("enter")  # 'Save unlinked — records your review'
        await _settle(app, pilot)
        assert fake_backend.remembered and fake_backend.remembered[0]["unlinked"] is True
        receipt = _text(app, "#capture-receipt")
        assert "observation" in receipt and "[finding] limits beat hope" in receipt
        assert "review" in receipt and "confirmed by you" in receipt


async def test_write_failure_keeps_the_text_and_offers_recoveries(make_app, fake_backend):
    fake_backend.fail_next(
        "capture_thought", code="PERMISSION_DENIED", message="permission denied writing Sources/"
    )
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        app.screen.query_one("#capture-content").text = "words that must survive a failure"
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        recovery = app.screen.query_one("#capture-recovery")
        assert recovery.has_class("visible")
        assert "✗ not saved" in str(recovery.query_one("#recovery-head").render())
        assert "kept below" in str(recovery.query_one("#recovery-fact").render())
        assert recovery.query_one("#recovery-options").option_count == 3
        assert (
            app.screen.query_one("#capture-content").text.strip()
            == "words that must survive a failure"
        )


async def test_escape_with_unsaved_text_asks_before_discarding(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot)
        app.screen.query_one("#capture-content").text = "half a thought"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfirmModal"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Capture"
        assert app.screen.query_one("#capture-content").text == "half a thought"
