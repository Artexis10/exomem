"""First run: the ledger accretes, writes announce themselves, esc rewinds."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from _fake import FakeBackend  # noqa: E402

pytestmark = pytest.mark.anyio


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _first_run(make_app, tmp_path=None):
    backend = FakeBackend(initialized=False)
    return make_app(backend), backend


def _text(app, selector: str) -> str:
    return str(app.screen.query_one(selector).render())


def _options(app, selector: str) -> str:
    widget = app.screen.query_one(selector)
    return "\n".join(
        str(widget.get_option_at_index(index).prompt) for index in range(widget.option_count)
    )


async def _create_vault(app, pilot, folder) -> None:
    await _settle(app, pilot)
    await pilot.press("enter")  # 'Create a fresh vault at ~/Exomem'
    await pilot.pause()
    path_input = app.screen.query_one("#path-input")
    path_input.value = str(folder)
    path_input.focus()
    await pilot.press("enter")
    await _settle(app, pilot)


async def test_opens_on_the_vault_question_with_the_intro(make_app):
    app, _backend = await _first_run(make_app)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "First run"
        intro = " ".join(_text(app, "#intro").split())
        assert "short conversation" in intro
        assert "esc rewinds one line" in intro
        assert "Nothing is written until a line says so" in intro
        assert "vault" in _text(app, "#question")
        assert "Where should your memory live?" in _text(app, "#question")
        choices = _options(app, "#choices")
        for expected in ("Create a fresh vault", "Use an existing vault", "Scan a folder first", "Skip for now"):
            assert expected in choices
        assert "28 files" in choices, "the count must come from the real scaffold"


async def test_the_preview_precedes_the_write(make_app, tmp_path):
    app, backend = await _first_run(make_app)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        preview = _text(app, "#preview")
        assert "Will create" in preview
        assert "Knowledge Base/" in preview
        assert "Sources/" in preview and "Notes/" in preview
        assert "owns nothing outside it" in preview
        assert not backend.calls or all(call[0] != "init_vault" for call in backend.calls), (
            "the preview must render before anything is written"
        )


async def test_creating_a_vault_writes_a_pinned_receipt(make_app, tmp_path):
    app, backend = await _first_run(make_app)
    async with app.run_test(size=(80, 24)) as pilot:
        await _create_vault(app, pilot, tmp_path / "new-vault")
        assert any(call[0] == "init_vault" for call in backend.calls)
        assert backend.runtime_started is True
        ledger = _text(app, "#ledger")
        assert "✓ vault" in ledger and "created" in ledger and "files, plain markdown" in ledger
        # the flow moves straight on to packs
        assert "packs" in _text(app, "#question")


async def test_create_on_an_existing_vault_connects_instead_of_failing(make_app, tmp_path):
    existing = tmp_path / "already-a-vault"
    backend = FakeBackend(initialized=False)
    backend.existing_vaults.add(str(existing))
    app = make_app(backend)
    async with app.run_test(size=(80, 24)) as pilot:
        await _create_vault(app, pilot, existing)
        assert not any(call[0] == "init_vault" for call in backend.calls), (
            "an existing vault must be connected, never re-initialized"
        )
        assert "connected" in _text(app, "#ledger")
        assert "already held a Knowledge Base" in _text(app, "#ledger")


async def test_a_write_failure_offers_recoveries_and_changes_nothing(make_app, tmp_path):
    backend = FakeBackend(initialized=False)
    backend.fail_next(
        "init_vault", code="VAULT_EXISTS", message="that folder already holds a Knowledge Base"
    )
    app = make_app(backend)
    async with app.run_test(size=(80, 24)) as pilot:
        await _create_vault(app, pilot, tmp_path / "collides")
        recovery = app.screen.query_one("#recovery")
        assert recovery.has_class("visible")
        head = str(recovery.query_one("#recovery-head").render())
        assert head.startswith("▲ already a vault")
        assert "force" not in head.lower(), "API language must never reach the screen"
        assert "Nothing was changed." in str(recovery.query_one("#recovery-fact").render())
        options = str(recovery.query_one("#recovery-options").get_option_at_index(0).prompt)
        assert "Connect to it" in options


async def test_packs_are_optional_and_skipping_leaves_a_line(make_app, tmp_path):
    app, _backend = await _first_run(make_app)
    async with app.run_test(size=(80, 24)) as pilot:
        await _create_vault(app, pilot, tmp_path / "vault")
        await pilot.press("s")  # skip packs
        await _settle(app, pilot)
        ledger = _text(app, "#ledger")
        assert "○ packs" in ledger and "skipped" in ledger
        assert "capture" in _text(app, "#question")


async def test_packs_selection_is_applied_and_receipted(make_app, tmp_path):
    app, backend = await _first_run(make_app)
    async with app.run_test(size=(80, 24)) as pilot:
        await _create_vault(app, pilot, tmp_path / "vault")
        assert app.screen.query_one("#packs-choice").display is True
        await pilot.press("enter")  # keep the pre-selected packs
        await _settle(app, pilot)
        applied = [call for call in backend.calls if call[0] == "apply_packs"]
        assert applied, "enter must confirm the selection, not toggle it"
        assert "✓ packs" in _text(app, "#ledger")


async def test_capture_then_ask_closes_with_a_citation(make_app, tmp_path):
    app, backend = await _first_run(make_app)
    async with app.run_test(size=(80, 24)) as pilot:
        await _create_vault(app, pilot, tmp_path / "vault")
        await pilot.press("enter")  # packs confirmed
        await _settle(app, pilot)
        app.screen.query_one("#capture-area").text = "the reranker cutoffs feel arbitrary"
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert backend.captured, "the first capture is an immutable Source"
        assert "✓ capture" in _text(app, "#ledger")
        ask_input = app.screen.query_one("#ask-input")
        ask_input.value = "queue"
        ask_input.focus()
        await pilot.press("enter")
        await _settle(app, pilot)
        ledger = _text(app, "#ledger")
        assert "✓ ask" in ledger
        assert "▸ retrieved" in ledger and "ms" in ledger
        closing = " ".join(_text(app, "#intro").split())
        assert "pointing at a file you own" in closing
        assert "Go to Home" in _options(app, "#choices")


async def test_esc_rewinds_a_skipped_step_but_stops_at_a_write(make_app, tmp_path):
    app, _backend = await _first_run(make_app)
    async with app.run_test(size=(80, 24)) as pilot:
        await _create_vault(app, pilot, tmp_path / "vault")
        await pilot.press("s")  # skip packs -> capture
        await _settle(app, pilot)
        assert "capture" in _text(app, "#question")
        await pilot.press("escape")  # rewind the packs line
        await _settle(app, pilot)
        assert "packs" in _text(app, "#question")
        assert "packs" not in _text(app, "#ledger")
        await pilot.press("escape")  # the vault line performed a write
        await _settle(app, pilot)
        assert "✓ vault" in _text(app, "#ledger"), "a write cannot be rewound"
        note = _text(app, "#note")
        assert "▲ pinned" in note, "a refused rewind must be as visible as any other state"
        assert "Settings (8)" in note, "and must name where the answer can still change"


async def test_esc_from_the_path_step_keeps_the_typing(make_app):
    app, _backend = await _first_run(make_app)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#path-input").value = "/tmp/somewhere"
        await pilot.press("escape")
        await pilot.pause()
        assert "Where should your memory live?" in _text(app, "#question")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#path-input").value == "/tmp/somewhere"


async def test_skip_for_now_lands_on_home_with_a_vaultless_state(make_app):
    app, backend = await _first_run(make_app)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        await pilot.press("down", "down", "down", "enter")  # 'Skip for now'
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "Home"
        assert backend.runtime_started is False
        assert "no vault" in str(app.screen.query_one("#home-status").render())


async def test_scan_first_reports_without_writing(make_app, tmp_path):
    folder = tmp_path / "old-notes"
    folder.mkdir()
    app, backend = await _first_run(make_app)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        await pilot.press("down", "down", "enter")  # 'Scan a folder first'
        await pilot.pause()
        path_input = app.screen.query_one("#path-input")
        path_input.value = str(folder)
        path_input.focus()
        await pilot.press("enter")
        await _settle(app, pilot)
        assert any(call[0] == "adopt_scan" for call in backend.calls)
        assert not any(call[0] == "init_vault" for call in backend.calls)
        ledger = _text(app, "#ledger")
        assert "✓ scanned" in ledger and "without writing anything" in ledger
        options = str(app.screen.query_one("#recovery-options").get_option_at_index(0).prompt)
        assert "Initialize the governed layer here" in options


async def test_first_run_never_reopens_once_a_vault_exists(make_app):
    app = make_app()  # initialized backend
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "Home"


async def test_an_applied_pack_selection_can_still_be_rewound(make_app, tmp_path):
    """Packs write a manifest, but that is not a one-way door.

    Pinning it refused a rewind the vault never actually forbids, which left
    the only way back through quitting the flow.
    """
    app, _backend = await _first_run(make_app)
    async with app.run_test(size=(80, 24)) as pilot:
        await _create_vault(app, pilot, tmp_path / "vault")
        await pilot.press("enter")  # apply the pre-selected packs
        await _settle(app, pilot)
        assert "✓ packs" in _text(app, "#ledger")
        assert "capture" in _text(app, "#question")
        await pilot.press("escape")
        await _settle(app, pilot)
        assert "packs" in _text(app, "#question"), "the packs answer must be changeable"
        assert "✓ packs" not in _text(app, "#ledger")
        assert app.screen.query_one("#packs-choice").display is True


async def test_up_arrow_walks_back_up_the_ledger(make_app):
    """The ledger and the active question are one column to the eye.

    `esc` was in the footer, but `up` is what people reach for, and wrapping
    to the bottom of the list made rewinding feel unreachable.
    """
    backend = FakeBackend(initialized=False)
    backend.existing_vaults.add("/data/sample-vault")
    app = make_app(backend)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        await pilot.press("down", "enter")  # 'Use an existing vault'
        await pilot.pause()
        path_input = app.screen.query_one("#path-input")
        path_input.value = "/data/sample-vault"
        path_input.focus()
        await pilot.press("enter")
        await _settle(app, pilot)
        packs = app.screen.query_one("#packs-choice")
        assert packs.highlighted == 0, "the list must open with a cursor to move"

        await pilot.press("up")  # already at the top row -> leave the block
        await _settle(app, pilot)
        assert "Where should your memory live?" in _text(app, "#question")
        assert "vault" not in _text(app, "#ledger")

        # ...but it never walks out of setup entirely
        await pilot.press("up")
        await pilot.press("up")
        await _settle(app, pilot)
        assert app.screen.SCREEN_TITLE == "First run"


async def test_connecting_to_an_existing_vault_is_not_pinned(make_app):
    """Connecting wrote nothing, so the answer is still changeable."""
    backend = FakeBackend(initialized=False)
    backend.existing_vaults.add("/data/sample-vault")
    app = make_app(backend)
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        await pilot.press("down", "enter")
        await pilot.pause()
        path_input = app.screen.query_one("#path-input")
        path_input.value = "/data/sample-vault"
        path_input.focus()
        await pilot.press("enter")
        await _settle(app, pilot)
        assert "✓ vault" in _text(app, "#ledger")
        await pilot.press("escape")
        await _settle(app, pilot)
        assert "vault" not in _text(app, "#ledger"), (
            "a connect performed no write and must rewind like any other answer"
        )
        assert "pinned" not in _text(app, "#note")
