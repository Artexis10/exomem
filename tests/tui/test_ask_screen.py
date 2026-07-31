"""Ask flow: results, empty state, degradation markers, errors, cancel."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from _fake import FakeBackend  # noqa: E402

pytestmark = pytest.mark.anyio


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _open_ask(app, pilot):
    await _settle(app, pilot)
    app.goto("ask")
    await pilot.pause()


async def _ask(app, pilot, query: str):
    await _open_ask(app, pilot)
    for character in query:
        await pilot.press(character if character != " " else "space")
    await pilot.press("enter")
    await _settle(app, pilot)


async def test_ask_lists_results_with_identity(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        results = app.screen.query_one("#ask-results")
        assert results.display is True
        assert results.option_count == 3
        banner = app.screen.query_one("#ask-degraded")
        assert not banner.has_class("visible")


async def test_ask_empty_state_is_honest(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "nothing")
        results = app.screen.query_one("#ask-results")
        empty = app.screen.query_one("#ask-empty")
        assert results.display is False
        assert empty.display is True


async def test_ask_surfaces_warming_marker(make_app):
    backend = FakeBackend(warming=True)
    app = make_app(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        banner = app.screen.query_one("#ask-degraded")
        assert banner.has_class("visible")


async def test_ask_error_shows_code_and_remediation(make_app, fake_backend):
    fake_backend.fail_next("ask", code="MUTATION_WARMING", message="warm-up in progress")
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        error = app.screen.query_one("#ask-error")
        assert error.display is True


async def test_enter_previews_selected_source(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        await pilot.press("enter")
        await _settle(app, pilot)
        # wide layout: preview lands in the side panel
        detail = app.screen.query_one("#ask-detail")
        assert detail.has_class("has-content")
        assert any(call[0] == "read_page" for call in fake_backend.calls)


async def test_cancel_drops_late_results(make_app, fake_backend):
    release = fake_backend.hold()
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_ask(app, pilot)
        for character in "queue":
            await pilot.press(character)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")  # cancels the in-flight ask
        release.set()
        await _settle(app, pilot)
        results = app.screen.query_one("#ask-results")
        assert results.display is False, "late results must not appear after cancel"
        assert app.screen.SCREEN_TITLE == "Ask"


async def test_unrelated_group_does_not_cancel_search(make_app, fake_backend):
    # Regression: generations are per worker group — a cache refresh while a
    # search is in flight must not drop the search results.
    release = fake_backend.hold()
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_ask(app, pilot)
        for character in "queue":
            await pilot.press(character)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("u")  # refresh-caches worker, different group
        release.set()
        await _settle(app, pilot)
        results = app.screen.query_one("#ask-results")
        assert results.display is True
        assert results.option_count == 3


async def test_write_back_uses_governed_remember(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        await pilot.press("w")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "WriteBackModal"
        text_area = app.screen.query_one("#writeback-content")
        text_area.text = "Bounded queues shed load predictably."
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert fake_backend.remembered, "write-back must call remember_note"
        assert fake_backend.remembered[0]["note_type"] == "insight"
