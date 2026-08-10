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


async def test_the_way_out_is_always_on_screen(make_app):
    """A user who cannot find the exit is trapped, however many keys exist."""
    from exomem.tui.screens.home import DESTINATIONS

    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        for name, _key, _title, _description in (("home", "", "", ""), *DESTINATIONS):
            app.goto(name)
            await _settle(app, pilot)
            footer = app.screen.query_one(AppFooter)
            shown = str(footer.query_one(".footer-left").render()) + str(
                footer.query_one(".footer-right").render()
            )
            assert "quit" in shown, f"{name} offers no visible way out"
            assert "? help" in shown, f"{name} hides the help hint"


async def test_ctrl_c_also_quits_because_a_multiplexer_may_own_ctrl_q(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfirmModal"
        assert app.return_code is None, "quitting stays confirmed, never abrupt"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Home"


async def test_q_quits_from_home(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        await pilot.press("q")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfirmModal"


async def test_first_run_advertises_quit_too(make_app, fake_backend):
    fake_backend.initialized = False
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "First run"
        right = str(app.screen.query_one(AppFooter).query_one(".footer-right").render())
        assert "^q quit" in right


async def test_narrow_footer_sheds_keys_but_never_the_help_hint(make_app):
    from exomem.tui.widgets import AppFooter as Footer

    crowded = [("space", "toggle"), ("enter", "continue"), ("s", "skip"), ("esc", "rewind")]
    fitted = Footer._fit([*crowded, ("?", "help")], budget=44)
    assert fitted[-1] == ("?", "help")
    assert len(fitted) < len(crowded) + 1, "a crowded footer must give way somewhere"
    assert Footer._fit([*crowded, ("?", "help")], budget=200) == [*crowded, ("?", "help")]


async def test_pointing_at_a_row_makes_it_the_one_enter_acts_on(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        menu = app.screen.query_one("#home-menu")
        assert menu.highlighted == 0
        await pilot.hover("#home-menu", offset=(4, 3))
        await pilot.pause()
        assert menu.highlighted == 3, "hover must move the cursor, not just tint the row"
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "Review"


async def test_the_app_picks_its_palette_from_what_the_terminal_can_show(make_app):
    from _fake import FakeBackend

    from exomem.tui.app import ExomemTuiApp
    from exomem.tui.theme import GLYPHS_UNICODE, TEXT

    exact = ExomemTuiApp(
        FakeBackend(), glyphs=GLYPHS_UNICODE, color=True, truecolor=True
    )
    async with exact.run_test(size=(80, 24)) as pilot:
        await _settle(exact, pilot)
        assert exact.skin.text == TEXT

    safe = ExomemTuiApp(
        FakeBackend(), glyphs=GLYPHS_UNICODE, color=True, truecolor=False
    )
    async with safe.run_test(size=(80, 24)) as pilot:
        await _settle(safe, pilot)
        assert safe.skin.text != TEXT, "a 256-colour terminal needs the safe token"


async def test_mouse_capture_can_be_released_for_native_selection(make_app):
    from _fake import FakeBackend

    from exomem.tui.app import ExomemTuiApp
    from exomem.tui.theme import GLYPHS_UNICODE

    app = ExomemTuiApp(FakeBackend(), glyphs=GLYPHS_UNICODE, mouse=False)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        assert app.mouse is False
        assert app.screen.SCREEN_TITLE == "Home", "releasing the mouse must not break the UI"
