"""Home: what can I do now, and is Exomem healthy?

The left column is the destination list; the right column is actionable
status only — every warning names its next action. At 80 columns the status
column yields to the destinations (full status lives on the Status screen).
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from ..theme import STYLE_FAIL, STYLE_OK, STYLE_WARN
from ..widgets import AppHeader
from .base import ExomemScreen

DESTINATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("continue", "1", "Continue", "pick up recent work and checkpoints"),
    ("ask", "2", "Ask", "recall what you know, with evidence"),
    ("capture", "3", "Capture", "save a thought before it evaporates"),
    ("review", "4", "Review", "what needs your attention, and why"),
    ("adopt", "5", "Adopt", "scan an existing folder of notes, safely"),
    ("packs", "6", "Packs", "domains that guide interpretation"),
    ("status", "7", "Status", "engine health and diagnostics"),
    ("settings", "8", "Settings", "mode, appearance, vault"),
)


def summarize_overview(sections: dict[str, Any], glyphs: dict[str, str]) -> list[Text]:
    """Pure renderer for the Home status column (unit-tested directly)."""

    lines: list[Text] = []

    def status_line(state: str, label: str, detail: str, action: str = "") -> None:
        glyph_key = {"ok": "ok", "warn": "warn", "fail": "fail"}.get(state, "idle")
        style = {"ok": STYLE_OK, "warn": STYLE_WARN, "fail": STYLE_FAIL}.get(state, "dim")
        line = Text()
        line.append(f"{glyphs.get(glyph_key, '*')} ", style=style)
        line.append(f"{label}  ", style="bold")
        line.append(detail)
        lines.append(line)
        if action:
            lines.append(Text(f"   {glyphs.get('arrow', '->')} {action}", style="dim"))

    def section(name: str) -> dict | None:
        entry = sections.get(name)
        if not isinstance(entry, dict):
            return None
        if entry.get("ok"):
            data = entry.get("data")
            return data if isinstance(data, dict) else {"value": data}
        error = entry.get("error") or {}
        status_line(
            "warn",
            name.capitalize(),
            f"unavailable ({error.get('code', 'INTERNAL')})",
            "open Status for details",
        )
        return None

    mode = section("mode")
    if mode is not None:
        status_line("ok", "Mode", f"{mode.get('mode', 'normal')} compute")

    readiness = section("readiness")
    if readiness is not None:
        if readiness.get("warming"):
            components = ", ".join(readiness.get("pending") or readiness.get("components") or [])
            status_line(
                "warn",
                "Retrieval",
                f"warming ({components or 'search lanes'}) — results may be partial",
                "full recall returns automatically when warm",
            )
        else:
            status_line("ok", "Retrieval", "ready")

    attention = section("attention")
    if attention is not None:
        total = attention.get("all_total", attention.get("total", 0)) or 0
        if total:
            status_line("warn", "Review", f"{total} item(s) need attention", "press 4 to review")
        else:
            status_line("ok", "Review", "nothing waiting")

    packs = section("packs")
    if packs is not None:
        selected = packs.get("selected") or []
        if selected:
            status_line("ok", "Packs", ", ".join(selected))
        else:
            status_line("idle", "Packs", "defaults in effect", "press 6 to choose")

    hooks = section("hooks")
    if hooks is not None:
        if hooks.get("success"):
            status_line("ok", "Hooks", "capture + retrieval hooks installed")
        else:
            status_line(
                "warn",
                "Hooks",
                "automatic capture/retrieval not fully wired",
                "run: exomem install-hook",
            )

    if not lines:
        lines.append(Text("Connecting to your knowledge base…", style="dim"))
    return lines


def overview_strip(sections: dict[str, Any], glyphs: dict[str, str]) -> Text:
    """One-line health summary for narrow layouts (pure, unit-tested)."""
    parts: list[tuple[str, str]] = []

    def data(name: str) -> dict:
        entry = sections.get(name) or {}
        payload = entry.get("data") if entry.get("ok") else None
        return payload if isinstance(payload, dict) else {}

    readiness = data("readiness")
    if readiness.get("warming"):
        parts.append((f"{glyphs.get('warn', '!')} warming", STYLE_WARN))
    else:
        parts.append((f"{glyphs.get('ok', '*')} ready", STYLE_OK))

    attention = data("attention")
    total = attention.get("all_total", attention.get("total", 0)) or 0
    if total:
        parts.append((f"{total} to review", STYLE_WARN))
    else:
        parts.append(("review clear", "dim"))

    selected = data("packs").get("selected") or []
    parts.append(("packs " + (", ".join(selected) if selected else "default"), "dim"))

    line = Text(no_wrap=True, overflow="ellipsis")
    for index, (fragment, style) in enumerate(parts):
        if index:
            line.append(f"  {glyphs.get('bullet', '-')}  ", style="dim")
        line.append(fragment, style=style)
    return line


class HomeScreen(ExomemScreen):
    SCREEN_TITLE = "Home"

    BINDINGS = [
        *[
            Binding(key, f"open('{name}')", title, show=False)
            for name, key, title, _ in DESTINATIONS
        ],
        Binding("u", "refresh", "refresh"),
        Binding("q", "app.back", "quit", show=False),
        Binding("question_mark", "app.help", "help"),
        Binding("escape", "app.back", "quit", show=False),
        Binding("f1", "app.help", "help", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        with Horizontal(id="home-columns"):
            with VerticalScroll(id="home-destinations"):
                yield Static("Do", classes="pane-title")
                yield OptionList(*self._options(), id="home-menu")
                yield Static(id="home-strip")
            with VerticalScroll(id="home-status"):
                yield Static("Now", classes="pane-title")
                yield Static(id="home-status-body", classes="pane")
        yield Footer()

    @staticmethod
    def _options() -> list[Option]:
        options: list[Option] = []
        for name, key, title, description in DESTINATIONS:
            prompt = Text()
            prompt.append(f"{key}  ", style="dim")
            prompt.append(f"{title:<9}", style="bold")
            prompt.append(f" {description}", style="dim")
            options.append(Option(prompt, id=name))
        return options

    def on_mount(self) -> None:
        self.query_one("#home-menu", OptionList).focus()
        self.show_loading()

    def show_loading(self) -> None:
        body = self.query_one("#home-status-body", Static)
        body.update(Text("Connecting to your knowledge base…", style="dim"))

    def show_first_run(self, detail: str) -> None:
        glyphs = self.app.glyphs
        text = Text()
        text.append(f"{glyphs.get('warn', '!')} ", style=STYLE_WARN)
        text.append("No vault yet  ", style="bold")
        text.append(detail or "nothing is configured")
        text.append(
            f"\n   {glyphs.get('arrow', '->')} press 5 to scan a folder, or 8 to point at one",
            style="dim",
        )
        self.query_one("#home-status-body", Static).update(text)
        # Narrow layouts hide the status column — the strip must carry it too.
        strip = Text()
        strip.append(f"{glyphs.get('warn', '!')} no vault yet ", style=STYLE_WARN)
        strip.append(
            f"{glyphs.get('arrow', '->')} 5 scans a folder, 8 points at one", style="dim"
        )
        self.query_one("#home-strip", Static).update(strip)

    def apply_overview(self, sections: dict[str, Any]) -> None:
        rendered = summarize_overview(sections, self.app.glyphs)
        joined = Text("\n").join(rendered) if rendered else Text("")
        self.query_one("#home-status-body", Static).update(joined)
        self.query_one("#home-strip", Static).update(
            overview_strip(sections, self.app.glyphs)
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id:
            self.app.goto(event.option_id)

    def action_open(self, name: str) -> None:
        self.app.goto(name)

    def action_refresh(self) -> None:
        self.show_loading()
        self.app.reload_overview()
