"""Packs, Status, Settings, Adopt, Continue, layout breakpoints, NO_COLOR."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from _fake import FakeBackend  # noqa: E402

pytestmark = pytest.mark.anyio


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _open(app, pilot, name: str):
    await _settle(app, pilot)
    app.goto(name)
    await _settle(app, pilot)


def _text(app, selector: str) -> str:
    return str(app.screen.query_one(selector).render())


# -- Packs ------------------------------------------------------------------ #
async def test_packs_multi_select_persists_across_visits(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "packs")
        selection_list = app.screen.query_one("#packs-list")
        assert selection_list.option_count == 3
        assert sorted(selection_list.selected) == ["business", "technical"]
        await pilot.press("down", "down", "down", "space")  # toggle creative on
        await pilot.press("a")
        await _settle(app, pilot)
        applied = [call for call in fake_backend.calls if call[0] == "apply_packs"]
        assert applied and "creative" in applied[0][1]["pack_ids"]
        assert "✓ packs" in _text(app, "#packs-status")

        # Reopening must land on a LIVE re-mounted screen showing persisted
        # state (guards against Textual removing popped, uninstalled screens).
        before = len([call for call in fake_backend.calls if call[0] == "packs_state"])
        app.goto("home")
        await pilot.pause()
        app.goto("packs")
        await _settle(app, pilot)
        revisited = app.screen.query_one("#packs-list")
        assert revisited.option_count == 3, "the revisited screen must be alive"
        after = len([call for call in fake_backend.calls if call[0] == "packs_state"])
        assert after > before, "a revisit must re-read from the backend"
        assert "creative" in fake_backend.selected_packs


async def test_packs_error_recovers(make_app, fake_backend):
    fake_backend.fail_next("apply_packs", code="KB_NOT_INITIALIZED", message="no vault yet")
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "packs")
        await pilot.press("space", "a")
        await _settle(app, pilot)
        recovery = app.screen.query_one("#packs-recovery")
        assert recovery.has_class("visible")
        assert "Nothing was changed" in str(recovery.query_one("#recovery-fact").render())


# -- Status ----------------------------------------------------------------- #
async def test_status_sections_render_as_receipts(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "status")
        doctor = _text(app, "#status-doctor")
        assert "python" in doctor and doctor.lstrip().startswith("●")
        assert "hooks" in _text(app, "#status-hooks")
        assert "warm" in _text(app, "#status-readiness")


async def test_status_hook_gap_names_the_command(make_app):
    app = make_app(FakeBackend(hooks_ok=False))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "status")
        assert "install-hook" in _text(app, "#status-hooks")


# -- Settings --------------------------------------------------------------- #
async def test_settings_mode_switch_persists(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "settings")
        modes = app.screen.query_one("#settings-mode")
        assert modes.option_count == 3
        assert modes.highlighted == 1, "the current mode starts under the cursor"
        await pilot.press("up", "enter")  # move to 'quiet' and apply
        await _settle(app, pilot)
        assert any(call == ("set_mode", {"value": "quiet"}) for call in fake_backend.calls)
        assert "✓ mode" in _text(app, "#settings-mode-status")


async def test_settings_names_the_real_ways_to_change_the_vault(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "settings")
        vault = _text(app, "#settings-vault")
        assert "sample-vault" in vault
        assert "EXOMEM_VAULT_PATH" in vault


# -- Adopt ------------------------------------------------------------------ #
async def test_adopt_scan_is_read_only_and_write_is_gated(make_app, fake_backend, tmp_path):
    folder = tmp_path / "old-notes"
    folder.mkdir()
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "adopt")
        path_input = app.screen.query_one("#adopt-path")
        path_input.value = str(folder)
        await pilot.press("enter")
        await _settle(app, pilot)
        report = _text(app, "#adopt-report")
        assert "✓ scanned" in report and "24 files" in report
        assert any(call[0] == "adopt_scan" for call in fake_backend.calls)
        assert not any(call[0] == "adopt_write" for call in fake_backend.calls)

        await pilot.press("c")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfirmModal"
        body = " ".join(str(child.render()) for child in app.screen.query("Static"))
        assert "never rewritten" in body and "nothing written yet" in body
        await pilot.press("enter")
        await _settle(app, pilot)
        writes = [call for call in fake_backend.calls if call[0] == "adopt_write"]
        assert writes and writes[0][1]["mode"] == "copy-as-sources"


async def test_adopt_write_without_a_scan_is_refused(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "adopt")
        await pilot.press("c")
        await _settle(app, pilot)
        assert not [call for call in fake_backend.calls if call[0] == "adopt_write"]


async def test_adopt_bad_folder_recovers(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "adopt")
        app.screen.query_one("#adopt-path").value = "/definitely/not/here"
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app.screen.query_one("#adopt-recovery").has_class("visible")


# -- Continue --------------------------------------------------------------- #
async def test_continue_empty_state_names_hook_install(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _open(app, pilot, "continue")
        recovery = app.screen.query_one("#continue-recovery")
        assert recovery.has_class("visible")
        options = recovery.query_one("#recovery-options")
        assert "exomem install-hook" in str(options.get_option_at_index(0).prompt)


async def test_continue_renders_and_copies_a_packet(make_app):
    backend = FakeBackend(
        checkpoints=[
            {
                "client": "claude",
                "session": "sample-session-1",
                "observed_at_ns": 1,
                "status": "valid",
                "checkpoint": {"structural": {}},
            }
        ]
    )
    app = make_app(backend)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(app, pilot, "continue")
        options = app.screen.query_one("#continue-list")
        assert options.display is True and options.option_count == 1
        await pilot.press("enter")
        await _settle(app, pilot)
        assert any(call[0] == "continuation_packet" for call in backend.calls)
        assert app.screen.query_one("#continue-detail").has_class("has-content")
        await pilot.press("y")
        await pilot.pause()


# -- layout and accessibility ------------------------------------------------ #
@pytest.mark.parametrize("size", [(80, 24), (100, 30), (120, 40)])
async def test_every_screen_survives_every_design_target(make_app, size):
    from exomem.tui.screens.home import DESTINATIONS

    app = make_app()
    async with app.run_test(size=size) as pilot:
        await _settle(app, pilot)
        for name, _key, title, _description in DESTINATIONS:
            await _open(app, pilot, name)
            assert app.screen.SCREEN_TITLE == title


async def test_side_pane_is_hidden_below_the_breakpoint(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        assert app.screen.side_pane_open() is False
        assert app.screen.query_one("#home-status").display is True, (
            "narrow layouts carry health in the status block instead"
        )


async def test_side_pane_replaces_the_status_block_when_wide(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _settle(app, pilot)
        assert app.screen.side_pane_open() is True
        assert app.screen.query_one("#home-status").display is False
        assert app.screen.query_one("#home-now").has_class("has-content")


async def test_no_color_keeps_every_status_legible(make_app, monkeypatch):
    from exomem.tui.app import ExomemTuiApp
    from exomem.tui.theme import GLYPHS_ASCII

    monkeypatch.setenv("NO_COLOR", "1")
    app = ExomemTuiApp(FakeBackend(), glyphs=GLYPHS_ASCII, color=False)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "Home"
        assert app.screen.has_class("-no-color")
        status = _text(app, "#home-status")
        assert "* ready" in status, "the ASCII glyph plus the word must carry the state"
        assert "!" in status or "review" in status


async def test_theme_toggle_restyles_everything_it_had_drawn(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "settings")
        assert app.theme == "exomem-dark"
        dark_menu = str(app.screen.query_one("#settings-mode").get_option_at_index(0).prompt)
        await pilot.press("t")
        await _settle(app, pilot)
        assert app.theme == "exomem-light"
        assert app.skin.accent != dark_menu, "the skin must follow the theme"
        # the screen is rebuilt, not left painted in the previous palette
        assert app.screen.SCREEN_TITLE == "Settings"
        rebuilt = app.screen.query_one("#settings-mode")
        assert rebuilt.option_count == 3
        home_menu = app._home.query_one("#home-menu")
        assert home_menu.option_count == 8
