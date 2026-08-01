#!/usr/bin/env python
"""Render TUI frames as plain text for visual review.

Drives the deterministic test backend through named states and prints each
frame as a character grid, so layout can be checked against the design frames
without a terminal. Development aid only — the committed goldens live in
`tests/tui/__snapshots__/`.

    .venv/bin/python scripts/tui_frames.py            # every frame
    .venv/bin/python scripts/tui_frames.py home ask   # a subset
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "tui"))

from _fake import FakeBackend  # noqa: E402

from exomem.tui.app import ExomemTuiApp  # noqa: E402
from exomem.tui.theme import GLYPHS_ASCII, GLYPHS_UNICODE  # noqa: E402

SAMPLE_VAULT = "/data/sample-vault"


def build(backend: FakeBackend | None = None, *, color: bool = True) -> ExomemTuiApp:
    app = ExomemTuiApp(
        backend or FakeBackend(),
        glyphs=GLYPHS_UNICODE if color else GLYPHS_ASCII,
        color=color,
    )
    app.fixed_elapsed_ms = 38.0
    return app


async def settle(pilot) -> None:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def goto(pilot, name: str) -> None:
    await settle(pilot)
    pilot.app.goto(name)
    await settle(pilot)


async def ask(pilot, query: str = "queue") -> None:
    await goto(pilot, "ask")
    for character in query:
        await pilot.press(character)
    await pilot.press("enter")
    await settle(pilot)


def grid(app) -> str:
    lines = []
    for strip in app.screen._compositor.render_strips():
        lines.append(strip.text.rstrip())
    return "\n".join(lines)


async def show(name: str, app, size, run) -> None:
    async with app.run_test(size=size) as pilot:
        await run(pilot)
        print(f"\n{'=' * size[0]}\n{name}  ({size[0]}x{size[1]})\n{'=' * size[0]}")
        print(grid(app))


def first_run_backend() -> FakeBackend:
    backend = FakeBackend(initialized=False)
    backend.existing_vaults.add(SAMPLE_VAULT)
    return backend


async def connect_sample(pilot) -> None:
    await settle(pilot)
    await pilot.press("down", "enter")
    await pilot.pause()
    path_input = pilot.app.screen.query_one("#path-input")
    path_input.value = SAMPLE_VAULT
    path_input.focus()
    await pilot.press("enter")
    await settle(pilot)


async def first_run_mid(pilot) -> None:
    await connect_sample(pilot)
    await pilot.press("enter")
    await settle(pilot)
    pilot.app.screen.query_one("#capture-area").text = (
        "the reranker cutoffs feel arbitrary — needs a measured floor"
    )
    await pilot.pause()


async def first_run_done(pilot) -> None:
    await first_run_mid(pilot)
    await pilot.press("ctrl+s")
    await settle(pilot)
    ask_input = pilot.app.screen.query_one("#ask-input")
    ask_input.value = "reranker cutoffs"
    ask_input.focus()
    await pilot.press("enter")
    await settle(pilot)


async def capture_compose(pilot) -> None:
    await goto(pilot, "capture")
    pilot.app.screen.query_one("#capture-content").text = (
        "the reranker cutoffs feel arbitrary — needs a measured floor\n"
        "before we trust top-k at all"
    )
    await pilot.pause()


FRAMES = {
    "home": lambda: ("Home ready", build(), (80, 24), settle),
    "home-wide": lambda: ("Home wide", build(), (120, 40), settle),
    "home-warming": lambda: ("Home warming", build(FakeBackend(warming=True)), (100, 30), settle),
    "home-no-color": lambda: ("Home NO_COLOR", build(color=False), (80, 24), settle),
    "first-run": lambda: ("First run welcome", build(first_run_backend()), (80, 24), settle),
    "first-run-mid": lambda: ("First run ledger", build(first_run_backend()), (80, 24), first_run_mid),
    "first-run-done": lambda: ("First run done", build(first_run_backend()), (80, 24), first_run_done),
    "ask": lambda: ("Ask results", build(), (80, 24), ask),
    "ask-wide": lambda: (
        "Ask + preview",
        build(),
        (120, 40),
        lambda pilot: _preview(pilot),
    ),
    "ask-empty": lambda: ("Ask empty", build(), (80, 24), lambda pilot: ask(pilot, "nothing")),
    "capture": lambda: ("Capture compose", build(), (80, 24), capture_compose),
    "review": lambda: ("Review queue", build(), (80, 24), lambda pilot: goto(pilot, "review")),
    "review-wide": lambda: ("Review context", build(), (120, 40), _review_context),
    "packs": lambda: ("Packs", build(), (100, 30), lambda pilot: goto(pilot, "packs")),
    "status": lambda: ("Status", build(), (100, 30), lambda pilot: goto(pilot, "status")),
    "settings": lambda: ("Settings", build(), (100, 30), lambda pilot: goto(pilot, "settings")),
    "adopt": lambda: ("Adopt", build(), (100, 30), _adopt),
    "continue": lambda: ("Continue", build(), (80, 24), lambda pilot: goto(pilot, "continue")),
}


async def _preview(pilot) -> None:
    await ask(pilot)
    await pilot.press("enter")
    await settle(pilot)


async def _review_context(pilot) -> None:
    await goto(pilot, "review")
    await pilot.press("enter")
    await settle(pilot)


async def _adopt(pilot) -> None:
    await goto(pilot, "adopt")
    pilot.app.screen.query_one("#adopt-path").value = "/"
    await pilot.press("enter")
    await settle(pilot)


async def main(names: list[str]) -> None:
    for name in names or list(FRAMES):
        factory = FRAMES.get(name)
        if factory is None:
            print(f"unknown frame: {name}", file=sys.stderr)
            continue
        title, app, size, run = factory()
        await show(title, app, size, run)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
