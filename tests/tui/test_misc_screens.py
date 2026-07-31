"""Packs, Status, Settings, Adopt, Continue, Onboarding, NO_COLOR."""

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


async def test_packs_multi_select_persists(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "packs")
        selection_list = app.screen.query_one("#packs-list")
        assert selection_list.option_count == 3
        assert sorted(selection_list.selected) == ["business", "technical"]
        # toggle creative on via keyboard
        await pilot.press("down", "down", "down", "space")
        await pilot.press("a")
        await _settle(app, pilot)
        applied = [c for c in fake_backend.calls if c[0] == "apply_packs"]
        assert applied and "creative" in applied[0][1]["pack_ids"]
        # reopening shows persisted state
        app.goto("home")
        await pilot.pause()
        app.goto("packs")
        await _settle(app, pilot)
        assert "creative" in fake_backend.selected_packs


async def test_packs_error_surfaces(make_app, fake_backend):
    fake_backend.fail_next("apply_packs", code="KB_NOT_INITIALIZED", message="no KB yet")
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "packs")
        await pilot.press("space", "a")
        await _settle(app, pilot)
        assert app.screen.query_one("#packs-error").display is True


async def test_status_sections_render(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "status")
        doctor = str(app.screen.query_one("#status-doctor").render())
        assert "python" in doctor
        hooks = str(app.screen.query_one("#status-hooks").render())
        assert "hook" in hooks.lower()


async def test_settings_mode_switch_persists(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "settings")
        quiet = app.screen.query_one("#mode-quiet")
        quiet.value = True
        await _settle(app, pilot)
        assert any(call == ("set_mode", {"value": "quiet"}) for call in fake_backend.calls)


async def test_adopt_scan_and_gated_write(make_app, fake_backend, tmp_path):
    folder = tmp_path / "old-notes"
    folder.mkdir()
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "adopt")
        path_input = app.screen.query_one("#adopt-path")
        path_input.value = str(folder)
        await pilot.press("enter")
        await _settle(app, pilot)
        report = str(app.screen.query_one("#adopt-report").render())
        assert "24 files" in report
        assert any(call[0] == "adopt_scan" for call in fake_backend.calls)
        # write mode is gated behind an explicit confirmation
        await pilot.press("c")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfirmModal"
        await pilot.press("y")
        await _settle(app, pilot)
        writes = [c for c in fake_backend.calls if c[0] == "adopt_write"]
        assert writes and writes[0][1]["mode"] == "copy-as-sources"


async def test_adopt_write_without_scan_refused(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "adopt")
        await pilot.press("c")
        await _settle(app, pilot)
        assert not [c for c in fake_backend.calls if c[0] == "adopt_write"]


async def test_continue_empty_state_names_hook_install(make_app):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot, "continue")
        empty = app.screen.query_one("#continue-empty")
        assert empty.display is True


async def test_onboarding_create_vault_path(make_app, tmp_path):
    backend = FakeBackend(initialized=False)
    app = make_app(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "Welcome"
        # choose "Create a fresh vault"
        await pilot.press("down", "enter")
        await pilot.pause()
        path_input = app.screen.query_one("#onboarding-path")
        assert path_input.display is True
        path_input.value = str(tmp_path / "new-vault")
        path_input.focus()
        await pilot.press("enter")
        await _settle(app, pilot)
        assert any(call[0] == "init_vault" for call in backend.calls)
        assert backend.runtime_started is True


async def test_onboarding_skip_lands_home(make_app):
    backend = FakeBackend(initialized=False)
    app = make_app(backend)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "Welcome"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Home"


async def test_no_color_env_does_not_crash(make_app, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "Home"
