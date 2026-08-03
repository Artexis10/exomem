"""Home: what is true right now, and what can I do about it.

Health reads as receipts — one line each, glyph and word first — so the state
of the engine is scannable before the eye reaches the menu. The menu itself is
one row per destination, never wrapped: a wrapped menu row destroys the column
alignment that makes eight choices readable at a glance.

Nothing here is decorative. Every warning either names its own next action or,
when the failure is real, becomes a selectable recovery list above the menu —
the same template every other screen uses for errors.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import OptionList, Static

from ..format import fit
from ..theme import Skin
from ..widgets import (
    AppFooter,
    AppHeader,
    BarOptionList,
    BarRow,
    RecoveryPanel,
    continuation,
    receipt,
)
from .base import ExomemScreen

DESTINATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("continue", "1", "Continue", "pick up recent work and checkpoints"),
    ("ask", "2", "Ask", "recall what you know, with evidence"),
    ("capture", "3", "Capture", "save a thought before it evaporates"),
    ("review", "4", "Review", "what needs your attention, and why"),
    ("adopt", "5", "Adopt", "scan a folder of notes, safely"),
    ("packs", "6", "Packs", "domains that guide interpretation"),
    ("status", "7", "Status", "engine health and diagnostics"),
    ("settings", "8", "Settings", "mode, appearance, vault"),
)

#: Width of the destination-title column in the Do list.
TITLE_FIELD = 11
#: Label field for the Now pane, which has room for longer words.
NOW_FIELD = 14


def section_data(sections: dict[str, Any], name: str) -> dict | None:
    entry = sections.get(name)
    if not isinstance(entry, dict) or not entry.get("ok"):
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else {"value": data}


def failed_sections(sections: dict[str, Any]) -> list[tuple[str, str]]:
    """(name, message) for every part of the overview that did not load."""
    broken: list[tuple[str, str]] = []
    for name, entry in sections.items():
        if isinstance(entry, dict) and entry.get("ok") is False:
            error = entry.get("error") or {}
            broken.append((name, str(error.get("message") or error.get("code") or "unavailable")))
    return broken


def health_lines(sections: dict[str, Any], skin: Skin, budget: int) -> list[Text]:
    """The full health block, one receipt per lane (Now pane, wide layouts)."""
    lines: list[Text] = []

    mode = section_data(sections, "mode")
    if mode is not None:
        lines.append(
            receipt(skin, "ok", "mode", f"{mode.get('mode', 'normal')} compute", budget=budget, label_width=NOW_FIELD)
        )

    readiness = section_data(sections, "readiness")
    if readiness is not None:
        if readiness.get("warming"):
            pending = ", ".join(readiness.get("pending") or readiness.get("components") or [])
            lines.append(
                receipt(
                    skin,
                    "warn",
                    "retrieval",
                    f"warming: {pending or 'search lanes'} — results may be partial",
                    budget=budget,
                    label_width=NOW_FIELD,
                )
            )
        else:
            lines.append(
                receipt(skin, "ok", "retrieval", "warm — full recall available", budget=budget, label_width=NOW_FIELD)
            )

    attention = section_data(sections, "attention")
    if attention is not None:
        total = attention.get("all_total", attention.get("total", 0)) or 0
        states = attention.get("state_summary") or {}
        right = f"{states.get('open', total)} open" if total else ""
        if total:
            lines.append(
                receipt(
                    skin, "warn", "review", f"{total} items need attention", right=right, budget=budget, label_width=NOW_FIELD
                )
            )
        else:
            lines.append(
                receipt(skin, "ok", "review", "nothing needs attention", budget=budget, label_width=NOW_FIELD)
            )

    packs = section_data(sections, "packs")
    if packs is not None:
        selected = packs.get("selected") or []
        lines.append(
            receipt(
                skin,
                "ok" if selected else "idle",
                "packs",
                ", ".join(selected) if selected else "defaults in effect — 6 chooses them",
                budget=budget,
                label_width=NOW_FIELD,
            )
        )

    hooks = section_data(sections, "hooks")
    if hooks is not None:
        if hooks.get("success"):
            lines.append(
                receipt(skin, "ok", "hooks", "capture, retrieval, continuation", budget=budget, label_width=NOW_FIELD)
            )
        else:
            lines.append(
                receipt(skin, "warn", "hooks", "not wired for your agents yet", budget=budget, label_width=NOW_FIELD)
            )
            lines.append(continuation(skin, "run: exomem install-hook", indent=2))

    return lines


def status_lines(sections: dict[str, Any], skin: Skin, budget: int) -> list[Text]:
    """The narrow-layout summary: worst status first, then review. Max 2 blocks."""
    lines: list[Text] = []
    readiness = section_data(sections, "readiness")
    packs = section_data(sections, "packs")
    corpus = section_data(sections, "corpus")

    if readiness is not None and readiness.get("warming"):
        pending = ", ".join(readiness.get("pending") or readiness.get("components") or [])
        since = readiness.get("since_s")
        suffix = f" {int(since)}s" if isinstance(since, (int, float)) else ""
        lines.append(
            receipt(
                skin,
                "warn",
                "warming",
                f"{pending or 'search lanes'} loading — results may be partial{suffix}",
                budget=budget,
            )
        )
        lines.append(continuation(skin, "full recall returns automatically; nothing to do"))
    elif readiness is not None:
        detail_parts = ["retrieval warm"]
        if corpus and corpus.get("known"):
            detail_parts.append(f"{corpus.get('notes', 0)} notes")
        if packs and packs.get("selected"):
            detail_parts.append("packs " + ", ".join(packs["selected"]))
        lines.append(
            receipt(skin, "ok", "ready", f" {skin.g('bullet')} ".join(detail_parts), budget=budget)
        )

    attention = section_data(sections, "attention")
    if attention is not None:
        total = attention.get("all_total", attention.get("total", 0)) or 0
        if total:
            states = attention.get("state_summary") or {}
            lines.append(
                receipt(
                    skin,
                    "warn",
                    "review",
                    f"{total} items need attention",
                    right=f"{states.get('open', total)} open",
                    budget=budget,
                )
            )
    return lines


class HomeScreen(ExomemScreen):
    SCREEN_TITLE = "Home"

    FOOTER_KEYS = (("1-8", "open"), ("enter", "open"), ("u", "refresh"), ("q", "quit"))

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        *[
            Binding(key, f"open('{name}')", title, show=False)
            for name, key, title, _ in DESTINATIONS
        ],
        Binding("escape", "app.confirm_quit", "quit", show=False),
        Binding("q", "app.confirm_quit", "quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._sections: dict[str, Any] = {}
        self._first_run_detail = ""

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        with Vertical(id="body", classes="-flush"):
            yield Static(id="home-status")
            yield RecoveryPanel(id="home-recovery")
            with Horizontal(classes="split"):
                with Vertical(classes="split-left"):
                    yield Static(id="home-do-label")
                    yield BarOptionList(id="home-menu")
                with VerticalScroll(classes="split-right", id="home-now"):
                    yield Static(id="home-now-body")
        yield AppFooter()

    def on_mount(self) -> None:
        skin = self.app.skin
        self.query_one("#home-do-label", Static).update(Text("  Do", style=skin.secondary))
        menu = self.query_one("#home-menu", BarOptionList)
        menu.set_rows(self._rows())
        menu.focus()
        self.query_one("#home-status", Static).update(
            Text(f"  {skin.g('working')} connecting to your knowledge base", style=skin.dim)
        )
        if self._first_run_detail:
            self._paint_first_run()
        elif self._sections:
            self._paint()

    def _rows(self) -> list[BarRow]:
        skin = self.app.skin
        budget = self.content_budget()
        rows: list[BarRow] = []
        for name, key, title, description in DESTINATIONS:
            line = Text(no_wrap=True)
            line.append(f"{key}  ", style=skin.secondary)
            line.append(f"{title:<{TITLE_FIELD}}", style=skin.text)
            line.append(fit(description, max(8, budget - TITLE_FIELD - 5)), style=skin.secondary)
            rows.append(BarRow(name, [(0, line)]))
        return rows

    # ------------------------------------------------------------------ #
    def show_first_run(self, detail: str) -> None:
        """No vault yet: say so, and offer the paths that actually exist.

        Startup resolves the vault on a worker, which can land before this
        screen has finished composing; the detail is therefore stored and
        painted on mount when that happens.
        """
        self._first_run_detail = detail
        if not self.is_mounted:
            return
        self._paint_first_run()

    def _paint_first_run(self) -> None:
        detail = self._first_run_detail
        skin = self.app.skin
        self.query_one("#home-status", Static).update(
            Text("  ", style=skin.dim).append_text(
                receipt(skin, "warn", "no vault", detail or "nothing is configured yet", budget=self.content_budget())
            )
        )
        self.query_one("#home-now-body", Static).update(
            Text("Connect or create one from Settings (8), or scan a folder first (5).", style=skin.dim)
        )
        self.query_one("#home-now").add_class("has-content")

    def apply_overview(self, sections: dict[str, Any]) -> None:
        self._sections = dict(sections)
        self._paint()

    def refresh_session_receipts(self) -> None:
        """Re-render after the app logged a receipt, without re-reading data."""
        if self._sections:
            self._paint()

    def repaint(self) -> None:
        """Rebuild every styled line from the current skin (theme switch)."""
        if not self.is_mounted:
            return
        skin = self.app.skin
        self.query_one("#home-do-label", Static).update(Text("  Do", style=skin.secondary))
        menu = self.query_one("#home-menu", BarOptionList)
        menu.set_rows(self._rows(), highlight=menu.highlighted or 0)
        if self._first_run_detail and not self._sections:
            self._paint_first_run()
        else:
            self._paint()

    def _paint(self) -> None:
        sections = self._sections
        if not sections or not self.is_mounted:
            return
        skin = self.app.skin
        budget = self.content_budget()

        status = self.query_one("#home-status", Static)
        block = Text()
        for index, line in enumerate(status_lines(sections, skin, budget)):
            if index:
                block.append("\n")
            block.append("  ")
            block.append_text(line)
        status.update(block)
        status.display = not self.side_pane_open()

        now_body = self.query_one("#home-now-body", Static)
        pane = Text()
        pane.append("Now\n", style=skin.secondary)
        for line in health_lines(sections, skin, self.detail_budget()):
            pane.append_text(line)
            pane.append("\n")
        receipts = self.app.session_receipts
        pane.append("\nThis session\n", style=skin.secondary)
        if receipts:
            for entry in receipts[-6:]:
                pane.append_text(
                    receipt(skin, entry.state, entry.word, entry.detail, budget=self.detail_budget())
                )
                pane.append("\n")
        else:
            pane.append("nothing yet — every action here leaves a line\n", style=skin.dim)
        pane.append(
            "\nReceipts are session-local; the files live in your vault.", style=skin.dim
        )
        now_body.update(pane)
        self.query_one("#home-now").add_class("has-content")

        broken = failed_sections(sections)
        panel = self.query_one("#home-recovery", RecoveryPanel)
        if broken:
            names = ", ".join(name for name, _ in broken)
            panel.show(
                state="fail",
                word="preflight",
                what=f"{names} failed to load — recall falls back to lexical-only",
                facts=["Nothing was changed. Ask still works; ranking is weaker."],
                options=[
                    ("dismiss", "Continue lexical-only", "full-text recall stays available"),
                    ("status", "Open Status", "the doctor report names the failing lane — 7"),
                    ("retry", "Retry loading", "re-reads engine state"),
                ],
                budget=budget,
                focus=False,
            )
        else:
            panel.hide()

    def on_resize(self, _event) -> None:
        menu = self.query_one("#home-menu", BarOptionList)
        menu.set_rows(self._rows(), highlight=menu.highlighted or 0)
        if self._sections:
            self._paint()

    # ------------------------------------------------------------------ #
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "home-menu":
            return
        event.stop()
        if event.option_id:
            self.app.goto(event.option_id)

    def on_recovery_panel_chosen(self, event: RecoveryPanel.Chosen) -> None:
        event.stop()
        if event.action == "status":
            self.app.goto("status")
        elif event.action == "retry":
            self.refresh_data()
        else:
            self.query_one("#home-recovery", RecoveryPanel).hide()
            self.query_one("#home-menu", BarOptionList).focus()

    def action_open(self, name: str) -> None:
        self.app.goto(name)

    def refresh_data(self) -> None:
        skin = self.app.skin
        self.query_one("#home-status", Static).update(
            Text(f"  {skin.g('working')} re-reading engine state", style=skin.dim)
        )
        self.app.reload_overview()
