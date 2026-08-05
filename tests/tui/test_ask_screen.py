"""Ask: results, empty recovery, partial markers, errors, cancel, write-back."""

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


def _text(app, selector: str) -> str:
    return str(app.screen.query_one(selector).render())


async def test_results_open_with_a_retrieved_header(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        results = app.screen.query_one("#ask-results")
        assert results.display is True
        assert results.option_count == 3
        header = _text(app, "#ask-header")
        assert "▸ retrieved" in header
        assert "3 results" in header and "ms" in header
        assert "enter previews" in header
        assert app.screen.query_one("#ask-banner").display is False


async def test_empty_result_recovers_instead_of_apologising(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "nothing")
        assert app.screen.query_one("#ask-results").display is False
        assert "0 results" in _text(app, "#ask-header")
        recovery = app.screen.query_one("#ask-recovery")
        assert recovery.has_class("visible")
        options = recovery.query_one("#recovery-options")
        labels = "\n".join(
            str(options.get_option_at_index(index).prompt) for index in range(options.option_count)
        )
        assert "Rephrase" in labels
        assert "whole vault" in labels
        assert "Capture" in labels
        assert "never invents" in _text(app, "#recovery-fact")


async def test_empty_state_can_widen_the_scope_to_the_whole_vault(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "nothing")
        await pilot.press("down")  # Rephrase -> Search the whole vault
        await pilot.press("enter")
        await _settle(app, pilot)
        scopes = [call[1].get("scope") for call in fake_backend.calls if call[0] == "ask"]
        assert "vault" in scopes, "the recovery must actually widen the scope"


async def test_partial_results_say_which_lanes_are_warming(make_app):
    app = make_app(FakeBackend(warming=True))
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        banner = _text(app, "#ask-banner")
        assert "▲ partial" in banner
        assert "embeddings" in banner and "reranker" in banner
        assert "u re-runs now" in banner
        assert "lexical lane only" in _text(app, "#ask-header")


async def test_error_renders_the_recovery_template_not_a_traceback(make_app, fake_backend):
    fake_backend.fail_next("ask", code="MUTATION_WARMING", message="warm-up in progress")
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        recovery = app.screen.query_one("#ask-recovery")
        assert recovery.has_class("visible")
        assert "✗ recall failed" in str(recovery.query_one("#recovery-head").render())
        assert "Nothing was changed" in str(recovery.query_one("#recovery-fact").render())
        assert recovery.query_one("#recovery-options").option_count >= 2


async def test_enter_previews_the_selected_source_in_the_side_pane(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        await pilot.press("enter")
        await _settle(app, pilot)
        detail = app.screen.query_one("#ask-detail")
        assert detail.has_class("has-content")
        body = _text(app, "#ask-detail-body")
        assert "▸ " in body
        assert "the file is the truth" in body
        assert any(call[0] == "read_page" for call in fake_backend.calls)


async def test_narrow_layout_opens_the_preview_full_screen(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await _ask(app, pilot, "queue")
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app.screen.__class__.__name__ == "DetailModal"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Ask"


async def test_escape_unwinds_to_the_query_before_leaving(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        assert app.focused.id == "ask-results"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Ask", "the first esc returns to the query"
        assert app.focused.id == "ask-input"
        assert app.screen.query_one("#ask-input").value == "queue", "typing is kept"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.SCREEN_TITLE == "Home"


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
        assert app.screen.query_one("#ask-results").display is False, (
            "late results must not appear after cancel"
        )
        assert "cancelled" in _text(app, "#ask-header")
        assert app.screen.SCREEN_TITLE == "Ask"


async def test_unrelated_group_does_not_cancel_search(make_app, fake_backend):
    # Regression: generations are per worker group — an unrelated backend call
    # while a search is in flight must not drop the search results.
    release = fake_backend.hold()
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_ask(app, pilot)
        for character in "queue":
            await pilot.press(character)
        await pilot.press("enter")
        await pilot.pause()
        app.screen.run_backend(
            lambda: "unrelated", lambda _r: None, lambda _e: None, group="preview"
        )
        release.set()
        await _settle(app, pilot)
        results = app.screen.query_one("#ask-results")
        assert results.display is True
        assert results.option_count == 3


async def test_write_back_asks_the_governed_question_before_saving(make_app, fake_backend):
    fake_backend.require_relation_review = True
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        await pilot.press("w")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "WriteBackModal"
        app.screen.query_one("#writeback-content").text = "Bounded queues shed load predictably."
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        assert app.screen.__class__.__name__ == "ConfirmModal", (
            "an unlinked note must put the relation review to the user"
        )
        assert not fake_backend.remembered, "nothing may be written before the answer"
        await pilot.press("enter")  # 'Save unlinked — records your review'
        await _settle(app, pilot)
        assert fake_backend.remembered and fake_backend.remembered[0]["unlinked"] is True


async def test_write_back_declined_writes_nothing(make_app, fake_backend):
    fake_backend.require_relation_review = True
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        await pilot.press("w")
        await pilot.pause()
        app.screen.query_one("#writeback-content").text = "Bounded queues shed load."
        await pilot.press("ctrl+s")
        await _settle(app, pilot)
        await pilot.press("escape")  # esc closes the governed question with no effect
        await _settle(app, pilot)
        assert not fake_backend.remembered
        assert "not saved" in _text(app, "#ask-header")


async def test_refresh_reruns_the_last_query(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        await _ask(app, pilot, "queue")
        before = len([call for call in fake_backend.calls if call[0] == "ask"])
        await pilot.press("u")
        await _settle(app, pilot)
        after = len([call for call in fake_backend.calls if call[0] == "ask"])
        assert after == before + 1
