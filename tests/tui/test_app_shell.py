"""App shell: startup, navigation, help overlay, quit confirmation."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

pytestmark = pytest.mark.anyio


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_home_shows_status_after_startup(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "Home"
        assert fake_backend.runtime_started is True
        assert "sample-vault" in app.context_label
        assert "normal" in app.context_label


async def test_number_key_opens_ask_and_escape_returns(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("2")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Ask"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Home"


async def test_help_overlay_opens_and_closes(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("question_mark")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "HelpModal"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Home"


async def test_quit_requires_confirmation(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfirmModal"
        await pilot.press("n")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Home"
        assert app.return_code is None


async def test_first_run_state_shows_guidance(make_app, fake_backend):
    fake_backend.initialized = False
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert "no vault" in app.context_label
        # runtime must not start against a missing vault
        assert fake_backend.runtime_started is False
