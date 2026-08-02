"""Golden frames for every state the design pins, at the design's sizes.

Rendered exclusively from the deterministic FakeBackend — synthetic content
and POSIX-style paths only, because the committed goldens are scanned by the
fail-closed public-artifact privacy gate. Never regenerate these against a
real vault. Retrieval timing is pinned (`fixed_elapsed_ms`) so a golden never
diffs on a millisecond. Intentional regeneration:

    .venv/bin/python -m pytest tests/tui/test_snapshots.py --snapshot-update
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from _fake import FakeBackend  # noqa: E402

#: A synthetic vault that already exists, so first-run frames never touch disk.
SAMPLE_VAULT = "/data/sample-vault"


def _app(backend: FakeBackend | None = None, *, color: bool = True):
    from exomem.tui.app import ExomemTuiApp
    from exomem.tui.theme import GLYPHS_ASCII, GLYPHS_UNICODE

    app = ExomemTuiApp(
        backend or FakeBackend(),
        glyphs=GLYPHS_UNICODE if color else GLYPHS_ASCII,
        color=color,
        # Pinned, not detected: the palette adapts to what the terminal can
        # render, so leaving this to the machine would make every stored frame
        # depend on whether CI advertised COLORTERM. Goldens capture the
        # authored design; the 256-colour substitutions are asserted directly
        # in the view-model tests instead.
        truecolor=True,
    )
    app.fixed_elapsed_ms = 38.0
    return app


async def _settle(pilot) -> None:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def _goto(pilot, name: str) -> None:
    await _settle(pilot)
    pilot.app.goto(name)
    await _settle(pilot)


async def _ask_flow(pilot, query: str = "queue") -> None:
    await _goto(pilot, "ask")
    for character in query:
        await pilot.press(character)
    await pilot.press("enter")
    await _settle(pilot)


def _first_run_backend() -> FakeBackend:
    backend = FakeBackend(initialized=False)
    backend.existing_vaults.add(SAMPLE_VAULT)
    return backend


async def _connect_sample_vault(pilot) -> None:
    """Walk first run to a connected vault without writing to a real disk."""
    await _settle(pilot)
    await pilot.press("down", "enter")  # 'Use an existing vault'
    await pilot.pause()
    path_input = pilot.app.screen.query_one("#path-input")
    path_input.value = SAMPLE_VAULT
    path_input.focus()
    await pilot.press("enter")
    await _settle(pilot)


# -- Home ------------------------------------------------------------------- #
def test_home_narrow(snap_compare):
    assert snap_compare(_app(), terminal_size=(80, 24), run_before=_settle)


def test_home_wide(snap_compare):
    async def run_before(pilot) -> None:
        await _settle(pilot)
        pilot.app.record_receipt("done", "captured", '"bounded queues shed load…"')
        pilot.app.record_receipt("retrieved", "asked", "queue backpressure · 3 results")
        await pilot.pause()

    assert snap_compare(_app(), terminal_size=(120, 40), run_before=run_before)


def test_home_warming(snap_compare):
    assert snap_compare(
        _app(FakeBackend(warming=True)), terminal_size=(100, 30), run_before=_settle
    )


def test_home_degraded(snap_compare):
    backend = FakeBackend()
    backend.fail_next("readiness", code="PREFLIGHT_FAILED", message="semantic models failed to load")
    assert snap_compare(_app(backend), terminal_size=(80, 24), run_before=_settle)


def test_home_no_color(snap_compare):
    assert snap_compare(_app(color=False), terminal_size=(80, 24), run_before=_settle)


# -- First run -------------------------------------------------------------- #
def test_first_run_welcome(snap_compare):
    assert snap_compare(
        _app(_first_run_backend()), terminal_size=(80, 24), run_before=_settle
    )


def test_first_run_preview(snap_compare):
    async def run_before(pilot) -> None:
        await _settle(pilot)
        await pilot.press("enter")  # 'Create a fresh vault at ~/Exomem'
        await pilot.pause()
        pilot.app.screen.query_one("#path-input").value = "~/Exomem"
        await pilot.pause()

    assert snap_compare(_app(_first_run_backend()), terminal_size=(80, 24), run_before=run_before)


def test_first_run_ledger_mid(snap_compare):
    async def run_before(pilot) -> None:
        await _connect_sample_vault(pilot)
        await pilot.press("enter")  # confirm the pre-selected packs
        await _settle(pilot)
        pilot.app.screen.query_one("#capture-area").text = (
            "the reranker cutoffs feel arbitrary — needs a measured floor"
        )
        await pilot.pause()

    assert snap_compare(_app(_first_run_backend()), terminal_size=(80, 24), run_before=run_before)


def test_first_run_done(snap_compare):
    async def run_before(pilot) -> None:
        await _connect_sample_vault(pilot)
        await pilot.press("enter")  # packs
        await _settle(pilot)
        pilot.app.screen.query_one("#capture-area").text = (
            "the reranker cutoffs feel arbitrary — needs a measured floor"
        )
        await pilot.press("ctrl+s")
        await _settle(pilot)
        ask_input = pilot.app.screen.query_one("#ask-input")
        ask_input.value = "reranker cutoffs"
        ask_input.focus()
        await pilot.press("enter")
        await _settle(pilot)

    assert snap_compare(_app(_first_run_backend()), terminal_size=(80, 24), run_before=run_before)


# -- Ask -------------------------------------------------------------------- #
def test_ask_results_narrow(snap_compare):
    assert snap_compare(_app(), terminal_size=(80, 24), run_before=_ask_flow)


def test_ask_results_wide_with_preview(snap_compare):
    async def run_before(pilot) -> None:
        await _ask_flow(pilot)
        await pilot.press("enter")
        await _settle(pilot)

    assert snap_compare(_app(), terminal_size=(120, 40), run_before=run_before)


def test_ask_partial_while_warming(snap_compare):
    assert snap_compare(
        _app(FakeBackend(warming=True)), terminal_size=(100, 30), run_before=_ask_flow
    )


def test_ask_empty_result(snap_compare):
    async def run_before(pilot) -> None:
        await _ask_flow(pilot, query="nothing")

    assert snap_compare(_app(), terminal_size=(80, 24), run_before=run_before)


def test_ask_error_recovery(snap_compare):
    backend = FakeBackend()
    backend.fail_next("ask", code="MUTATION_WARMING", message="warm-up in progress")
    assert snap_compare(_app(backend), terminal_size=(80, 24), run_before=_ask_flow)


# -- Capture ---------------------------------------------------------------- #
def test_capture_narrow(snap_compare):
    async def run_before(pilot) -> None:
        await _goto(pilot, "capture")
        pilot.app.screen.query_one("#capture-content").text = (
            "the reranker cutoffs feel arbitrary — needs a measured floor\n"
            "before we trust top-k at all"
        )
        await pilot.pause()

    assert snap_compare(_app(), terminal_size=(80, 24), run_before=run_before)


def test_capture_governed_question(snap_compare):
    backend = FakeBackend()
    backend.require_relation_review = True

    async def run_before(pilot) -> None:
        await _goto(pilot, "capture")
        pilot.app.screen.query_one("#capture-content").text = (
            "bounded queues shed load predictably under pressure"
        )
        await pilot.press("tab")  # thought -> insight
        await pilot.press("ctrl+s")
        await _settle(pilot)

    assert snap_compare(_app(backend), terminal_size=(80, 24), run_before=run_before)


def test_capture_receipt(snap_compare):
    async def run_before(pilot) -> None:
        await _goto(pilot, "capture")
        pilot.app.screen.query_one("#capture-content").text = (
            "bounded queues shed load predictably\n\n"
            "## Observations\n- [finding] queues need explicit limits\n"
        )
        await pilot.press("ctrl+s")
        await _settle(pilot)

    assert snap_compare(_app(), terminal_size=(80, 24), run_before=run_before)


# -- Review ----------------------------------------------------------------- #
def test_review_narrow(snap_compare):
    async def run_before(pilot) -> None:
        await _goto(pilot, "review")

    assert snap_compare(_app(), terminal_size=(80, 24), run_before=run_before)


def test_review_triaged_receipt(snap_compare):
    async def run_before(pilot) -> None:
        await _goto(pilot, "review")
        await pilot.press("d")
        await _settle(pilot)

    assert snap_compare(_app(), terminal_size=(80, 24), run_before=run_before)


def test_review_wide(snap_compare):
    async def run_before(pilot) -> None:
        await _goto(pilot, "review")
        await pilot.press("enter")
        await _settle(pilot)

    assert snap_compare(_app(), terminal_size=(120, 40), run_before=run_before)


def test_review_empty(snap_compare):
    async def run_before(pilot) -> None:
        await _goto(pilot, "review")

    assert snap_compare(
        _app(FakeBackend(attention={"items": [], "shown": 0, "total": 0})),
        terminal_size=(80, 24),
        run_before=run_before,
    )


# -- Supporting screens ------------------------------------------------------ #
def test_adopt_report(snap_compare):
    async def run_before(pilot) -> None:
        await _goto(pilot, "adopt")
        path_input = pilot.app.screen.query_one("#adopt-path")
        path_input.value = "/"
        await pilot.press("enter")
        await _settle(pilot)

    assert snap_compare(_app(), terminal_size=(100, 30), run_before=run_before)


def test_status_standard(snap_compare):
    async def run_before(pilot) -> None:
        await _goto(pilot, "status")

    assert snap_compare(_app(), terminal_size=(100, 30), run_before=run_before)


def test_packs_standard(snap_compare):
    async def run_before(pilot) -> None:
        await _goto(pilot, "packs")

    assert snap_compare(_app(), terminal_size=(100, 30), run_before=run_before)


def test_settings_standard(snap_compare):
    async def run_before(pilot) -> None:
        await _goto(pilot, "settings")

    assert snap_compare(_app(), terminal_size=(100, 30), run_before=run_before)


def test_continue_empty_narrow(snap_compare):
    async def run_before(pilot) -> None:
        await _goto(pilot, "continue")

    assert snap_compare(_app(), terminal_size=(80, 24), run_before=run_before)
