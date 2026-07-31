"""Golden snapshots for representative screens at narrow and wide sizes.

Rendered exclusively from the deterministic FakeBackend — synthetic content
and POSIX-style paths only, because the committed SVGs are scanned by the
fail-closed public-artifact privacy gate. Never regenerate these against a
real vault. Intentional regeneration:

    .venv/bin/python -m pytest tests/tui/test_snapshots.py --snapshot-update
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from _fake import FakeBackend  # noqa: E402


def _app(backend: FakeBackend | None = None):
    from exomem.tui.app import ExomemTuiApp
    from exomem.tui.theme import GLYPHS_UNICODE

    return ExomemTuiApp(backend or FakeBackend(), glyphs=GLYPHS_UNICODE)


async def _settle(pilot) -> None:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def _ask_flow(pilot, query: str = "queue") -> None:
    await _settle(pilot)
    pilot.app.goto("ask")
    await pilot.pause()
    for character in query:
        await pilot.press(character)
    await pilot.press("enter")
    await _settle(pilot)


def test_home_narrow(snap_compare):
    assert snap_compare(_app(), terminal_size=(80, 24), run_before=_settle)


def test_home_wide(snap_compare):
    assert snap_compare(_app(), terminal_size=(120, 40), run_before=_settle)


def test_home_first_run(snap_compare):
    assert snap_compare(
        _app(FakeBackend(initialized=False)), terminal_size=(80, 24), run_before=_settle
    )


def test_home_warming(snap_compare):
    assert snap_compare(
        _app(FakeBackend(warming=True)), terminal_size=(100, 30), run_before=_settle
    )


def test_ask_results_wide_with_preview(snap_compare):
    async def run_before(pilot) -> None:
        await _ask_flow(pilot)
        await pilot.press("enter")
        await _settle(pilot)

    assert snap_compare(_app(), terminal_size=(120, 40), run_before=run_before)


def test_ask_results_narrow(snap_compare):
    assert snap_compare(_app(), terminal_size=(80, 24), run_before=_ask_flow)


def test_ask_degraded_marker(snap_compare):
    assert snap_compare(
        _app(FakeBackend(warming=True)), terminal_size=(100, 30), run_before=_ask_flow
    )


def test_ask_empty_result(snap_compare):
    async def run_before(pilot) -> None:
        await _ask_flow(pilot, query="nothing")

    assert snap_compare(_app(), terminal_size=(80, 24), run_before=run_before)
