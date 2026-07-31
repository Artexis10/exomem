"""Visual constants for the TUI: themes, glyphs, and shared CSS.

One accent (warm amber) reserved for focus/selection/identity — never for
status. Status always pairs a color with a glyph and a word, so nothing is
conveyed by color alone, and every glyph has an ASCII fallback for terminals
whose encoding cannot render the Unicode set.
"""

from __future__ import annotations

from textual.theme import Theme

ACCENT = "#c9963f"

# Rich-Text span styles (CSS variables only resolve in stylesheets, not in
# rich.Text spans) — mid-tone literals readable on both themes.
STYLE_ACCENT = ACCENT
STYLE_OK = "#6a9955"
STYLE_WARN = "#c19a1b"
STYLE_FAIL = "#c05b4d"

EXOMEM_DARK = Theme(
    name="exomem-dark",
    primary=ACCENT,
    secondary="#8a97a6",
    accent=ACCENT,
    warning="#d4a017",
    error="#c05b4d",
    success="#6a9955",
    foreground="#d4d4d4",
    background="#14161a",
    surface="#1c1f24",
    panel="#22262c",
    dark=True,
)

EXOMEM_LIGHT = Theme(
    name="exomem-light",
    primary="#8a5a12",
    secondary="#5a6572",
    accent="#8a5a12",
    warning="#8a6d00",
    error="#a03b2e",
    success="#3f6b33",
    foreground="#24292f",
    background="#f4f2ee",
    surface="#ffffff",
    panel="#ece9e2",
    dark=False,
)

GLYPHS_UNICODE = {
    "ok": "●",
    "idle": "○",
    "warn": "▲",
    "fail": "×",
    "bullet": "·",
    "arrow": "→",
    "pointer": "▸",
    "ellipsis": "…",
}

GLYPHS_ASCII = {
    "ok": "*",
    "idle": "o",
    "warn": "!",
    "fail": "x",
    "bullet": "-",
    "arrow": "->",
    "pointer": ">",
    "ellipsis": "...",
}


def pick_glyphs(encoding: str | None) -> dict[str, str]:
    """The Unicode glyph set when the terminal encoding can render it."""
    if not encoding:
        return GLYPHS_ASCII
    try:
        for char in GLYPHS_UNICODE.values():
            char.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return GLYPHS_ASCII
    return GLYPHS_UNICODE


# App-wide stylesheet. Breakpoint classes (-narrow/-standard/-wide) are set by
# the App's HORIZONTAL_BREAKPOINTS; screens simplify at 80 columns by hiding
# persistent side panels (detail moves into overlays).
APP_CSS = """
Screen {
    background: $background;
}

#app-header {
    dock: top;
    height: 1;
    background: $panel;
    color: $secondary;
    padding: 0 1;
}
#app-header .screen-name {
    color: $foreground;
    text-style: bold;
}

.pane {
    padding: 0 1;
}
.pane-title {
    color: $secondary;
    text-style: bold;
    padding: 1 1 0 1;
}

.status-ok { color: $success; }
.status-warn { color: $warning; }
.status-fail { color: $error; }
.dim { color: $secondary; }

.next-action {
    color: $secondary;
    padding: 0 0 0 2;
}

.empty-state {
    color: $secondary;
    padding: 1 2;
}

#home-columns {
    height: 1fr;
}
#home-destinations {
    width: 34;
    min-width: 28;
    border-right: solid $panel;
}
Screen.-narrow #home-destinations {
    width: 1fr;
    border-right: none;
}
Screen.-narrow #home-status {
    display: none;
}
#home-strip {
    display: none;
    padding: 1 1 0 1;
    color: $secondary;
}
Screen.-narrow #home-strip {
    display: block;
}
#home-status {
    width: 1fr;
    padding: 0 1;
}

#ask-input {
    margin: 1 1 0 1;
    height: 1;
    border: none;
    background: $surface;
    padding: 0 1;
}
#ask-input:focus {
    background: $surface;
    border: none;
    border-left: thick $accent;
    padding: 0 1 0 0;
}
#ask-status {
    height: 1;
    padding: 0 2;
    color: $secondary;
}
#ask-degraded {
    display: none;
    padding: 0 2;
    color: $warning;
}
#ask-degraded.visible {
    display: block;
}
#ask-body {
    height: 1fr;
}
#ask-results {
    width: 1fr;
}
#ask-detail {
    width: 44%;
    min-width: 30;
    border-left: solid $panel;
    padding: 0 1;
    display: none;
}
Screen.-wide #ask-detail.has-content {
    display: block;
}

OptionList {
    background: $background;
    border: none;
    padding: 0 1;
}
OptionList:focus {
    border: none;
}
OptionList > .option-list--option-highlighted {
    background: $panel;
    color: $foreground;
    text-style: bold;
}

#modal-box {
    background: $surface;
    border: round $secondary;
    padding: 1 2;
    width: 72;
    max-width: 90%;
    max-height: 80%;
}
Screen.-narrow #modal-box {
    width: 90%;
}
"""
