"""Capture flow: auto-title, kinds, confirmation, error keeps input, esc guard."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from exomem.tui.screens.capture import derive_title  # noqa: E402

pytestmark = pytest.mark.anyio


def test_derive_title_first_line_trimmed():
    assert derive_title("# A heading line\nmore") == "A heading line"
    assert derive_title("\n\n  plain thought here \n") == "plain thought here"
    long = "x" * 120
    derived = derive_title(long)
    assert len(derived) <= 80 and derived.endswith("…")
    assert derive_title("   \n \n") == ""


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _open_capture(app, pilot):
    await _settle(app, pilot)
    app.goto("capture")
    await pilot.pause()


async def test_thought_capture_names_stored_path(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_capture(app, pilot)
        area = app.screen.query_one("#capture-content")
        area.text = "bounded queues shed load predictably"
        await pilot.pause()
        title_input = app.screen.query_one("#capture-title")
        assert title_input.value.startswith("bounded queues")
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert fake_backend.captured and fake_backend.captured[0]["source_type"] == "other"
        result = app.screen.query_one("#capture-result")
        assert "Sources" in str(result.render())
        assert app.screen.query_one("#capture-content").text == ""


async def test_insight_kind_routes_to_remember(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_capture(app, pilot)
        app.screen.query_one("#capture-content").text = "retry budgets beat unbounded retries"
        app.screen.query_one("#kind-insight").value = True
        await pilot.pause()
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert fake_backend.remembered, "insight kind must route through remember"
        assert not fake_backend.captured


async def test_error_keeps_typed_content(make_app, fake_backend):
    fake_backend.fail_next("capture_thought", code="MUTATION_BUSY", message="busy")
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_capture(app, pilot)
        app.screen.query_one("#capture-content").text = "a thought that must survive"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert app.screen.query_one("#capture-content").text == "a thought that must survive"
        assert app.screen.query_one("#capture-error").display is True


async def test_escape_with_content_asks_first(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_capture(app, pilot)
        app.screen.query_one("#capture-content").text = "unsaved words"
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfirmModal"
        await pilot.press("n")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Capture"
        assert app.screen.query_one("#capture-content").text == "unsaved words"


async def test_escape_without_content_goes_back(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_capture(app, pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Home"
