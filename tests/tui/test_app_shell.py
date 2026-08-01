"""App shell: startup, navigation, chrome, help overlay, quit confirmation."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from exomem.tui.widgets import AppFooter, AppHeader  # noqa: E402

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


async def test_chrome_rows_name_the_screen_and_the_vault(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        header = app.screen.query_one(AppHeader)
        left = str(header.query_one(".header-left").render())
        right = str(header.query_one(".header-right").render())
        assert left.startswith("exomem") and "Home" in left
        assert "sample-vault" in right and "normal" in right
        footer = str(app.screen.query_one(AppFooter).query_one(".footer-left").render())
        assert "? help" in footer, "help is always reachable from the footer"
        assert "^p" in str(app.screen.query_one(AppFooter).query_one(".footer-right").render())


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


async def test_enter_opens_the_highlighted_do_row(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("down")  # Continue -> Ask
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Ask"


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
        await pilot.press("escape")  # esc always closes without effect
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Home"
        assert app.return_code is None


async def test_every_destination_opens_and_returns(make_app):
    from exomem.tui.screens.home import DESTINATIONS

    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert len(DESTINATIONS) == 8, "1–8 must map to exactly eight destinations"
        for name, _key, title, _description in DESTINATIONS:
            app.goto(name)
            await _settle(app, pilot)
            assert app.screen.SCREEN_TITLE == title
            app.goto("home")
            await pilot.pause()
            assert app.screen.SCREEN_TITLE == "Home"


async def test_first_run_opens_when_no_vault_is_configured(make_app, fake_backend):
    fake_backend.initialized = False
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert "no vault" in app.context_label
        assert app.screen.SCREEN_TITLE == "First run"
        # runtime must not start against a missing vault
        assert fake_backend.runtime_started is False


async def test_session_receipts_appear_on_home(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(app, pilot)
        app.record_receipt("done", "captured", '"a thought"')
        await pilot.pause()
        pane = str(app.screen.query_one("#home-now-body").render())
        assert "This session" in pane
        assert "captured" in pane
        assert "session-local" in pane, "receipts must say they are process state"
