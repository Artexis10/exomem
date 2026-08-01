"""Design tokens: color roles, glyph sets, and the app stylesheet.

Color is a set of roles, not a palette sprinkled around. Two rules do most of
the work:

**The accent boundary.** Amber lights *live* state only — the current step, a
`▸ retrieved` header, a live wikilink, the cursor, a focused input's border,
the selection bar. It is never decoration, never chrome, and never an error.

**Status is never color alone.** Every status renders as glyph + word
(`● ready`, `▲ warming`, `✗ not saved`), so a monochrome terminal, a
colorblind reader, and a screenshot all carry the same information. `NO_COLOR`
therefore degrades losslessly: the mono skin drops hue and keeps hierarchy in
dim/bold/reverse, and the ASCII glyph set keeps the same words.

Status hues deliberately use ANSI slots (green 2 / yellow 3 / red 1) rather
than hex, so they inherit the emulator's own palette instead of fighting it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from textual.theme import Theme

# -- color roles (truecolor values; 256/16 degrade through the terminal) ---- #
TEXT = "#ece9e2"
SECONDARY = "#a39e93"
DIM = "#5c574d"
STRUCK = "#6b655a"
CHROME_BG = "#1c1916"
BACKGROUND = "#0a0908"
SURFACE = "#14120f"
#: Phosphor amber, confirmed as Exomem's terminal accent in Substrate Design
#: System v2 (truecolor #ffb000, 256-color 214, 16-color yellow 3).
ACCENT = "#ffb000"

# Status hues are ANSI slots on purpose — see the module docstring.
OK = "green"
WARN = "yellow"
FAIL = "red"

GLYPHS_UNICODE: dict[str, str] = {
    "ok": "●",
    "idle": "○",
    "warn": "▲",
    "fail": "✗",
    "done": "✓",
    "pointer": "▸",
    "working": "…",
    "bar": "▌",
    "vrule": "│",
    "hrule": "─",
    "bullet": "·",
    "arrow": "→",
    "ellipsis": "…",
    "tree_mid": "├",
    "tree_end": "└",
}

GLYPHS_ASCII: dict[str, str] = {
    "ok": "*",
    "idle": "o",
    "warn": "!",
    "fail": "x",
    "done": "+",
    "pointer": ">",
    "working": "...",
    "bar": ">",
    "vrule": "|",
    "hrule": "-",
    "bullet": "-",
    "arrow": "->",
    "ellipsis": "...",
    "tree_mid": "+",
    "tree_end": "\\",
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


@dataclass(frozen=True)
class Skin:
    """Glyphs + rich-style strings for one rendering environment.

    Rich spans cannot resolve Textual CSS variables, so styled text is built
    from this object instead of from `$`-names. Pure render helpers take a
    skin, which is what makes the `NO_COLOR` path testable rather than
    aspirational.
    """

    glyphs: Mapping[str, str]
    text: str
    secondary: str
    dim: str
    struck: str
    accent: str
    ok: str
    warn: str
    fail: str
    color: bool = True

    def g(self, key: str) -> str:
        """One glyph, falling back to the ASCII set for unknown keys."""
        return self.glyphs.get(key, GLYPHS_ASCII.get(key, "?"))

    def status(self, state: str) -> tuple[str, str]:
        """(glyph, style) for a status word — the glyph always carries it."""
        table = {
            "ok": ("ok", self.ok),
            "warn": ("warn", self.warn),
            "fail": ("fail", self.fail),
            "done": ("done", self.ok),
            "idle": ("idle", self.dim),
            "working": ("working", self.dim),
            "retrieved": ("pointer", self.accent),
        }
        glyph_key, style = table.get(state, ("idle", self.dim))
        return self.g(glyph_key), style


def make_skin(glyphs: Mapping[str, str], *, color: bool = True) -> Skin:
    """The color skin, or the `NO_COLOR` one where hierarchy replaces hue."""
    if color:
        return Skin(
            glyphs=glyphs,
            text=TEXT,
            secondary=SECONDARY,
            dim=DIM,
            struck=f"{STRUCK} strike",
            accent=ACCENT,
            ok=OK,
            warn=WARN,
            fail=FAIL,
        )
    return Skin(
        glyphs=glyphs,
        text="",
        secondary="",
        dim="dim",
        struck="dim strike",
        accent="bold",
        ok="",
        warn="",
        fail="",
        color=False,
    )


def no_color_requested(environ: Mapping[str, str] | None = None) -> bool:
    """Honor the NO_COLOR convention: set at all, with any value, means off."""
    env = os.environ if environ is None else environ
    return "NO_COLOR" in env


EXOMEM_DARK = Theme(
    name="exomem-dark",
    primary=ACCENT,
    secondary=SECONDARY,
    accent=ACCENT,
    warning="yellow",
    error="red",
    success="green",
    foreground=TEXT,
    background=BACKGROUND,
    surface=SURFACE,
    panel=CHROME_BG,
    dark=True,
    variables={
        "dim": DIM,
        "chrome-bg": CHROME_BG,
        "block-cursor-background": ACCENT,
        "block-cursor-foreground": BACKGROUND,
        "input-selection-background": f"{ACCENT} 25%",
    },
)

EXOMEM_LIGHT = Theme(
    name="exomem-light",
    primary="#8a5a12",
    secondary="#5f5a51",
    accent="#8a5a12",
    warning="yellow",
    error="red",
    success="green",
    foreground="#1d1b17",
    background="#f6f4ef",
    surface="#ffffff",
    panel="#e7e2d8",
    dark=False,
    variables={
        "dim": "#7b7469",
        "chrome-bg": "#e7e2d8",
        "block-cursor-background": "#8a5a12",
        "block-cursor-foreground": "#f6f4ef",
    },
)

LIGHT_SKIN_OVERRIDES = {
    "text": "#1d1b17",
    "secondary": "#5f5a51",
    "dim": "#7b7469",
    "struck": "#8d867a strike",
    "accent": "#8a5a12",
}


# The stylesheet carries structure only — chrome rows, gutters, pane widths,
# breakpoint collapse. Anything that encodes meaning (status, receipts,
# selection bars) is built as styled text from the Skin so it survives
# NO_COLOR and ASCII terminals unchanged.
APP_CSS = """
Screen {
    background: $background;
    color: $foreground;
}

/* -- chrome: the first and last row, full width ------------------------- */
AppHeader {
    dock: top;
    height: 1;
    background: $panel;
    padding: 0 1;
}
AppHeader > .header-left { width: 1fr; color: $secondary; }
AppHeader > .header-right { width: auto; color: $text-muted; }

AppFooter {
    dock: bottom;
    height: 1;
    background: $panel;
    padding: 0 1;
}
AppFooter > .footer-left { width: 1fr; }
AppFooter > .footer-right { width: auto; }

/* -- body: one blank row under the header, 2-cell content gutter -------- */
#body {
    padding: 1 2 0 2;
    height: 1fr;
}
#body.-flush {
    padding: 1 0 0 0;
}

.gutter { padding: 0 0 0 2; }
.block { padding: 0 0 1 0; }
.dim { color: $text-muted; }
.prose { color: $foreground; }

/* -- option lists: the selection bar lives in the prompt's first cell --- */
OptionList {
    background: transparent;
    border: none;
    padding: 0;
    scrollbar-size-vertical: 1;
}
OptionList:focus { border: none; }
OptionList > .option-list--option-highlighted {
    background: $accent 15%;
    color: $foreground;
}
OptionList:focus > .option-list--option-highlighted {
    background: $accent 22%;
    color: $foreground;
}
Screen.-no-color OptionList > .option-list--option-highlighted,
Screen.-no-color OptionList:focus > .option-list--option-highlighted {
    background: transparent;
    text-style: reverse;
}

SelectionList {
    background: transparent;
    border: none;
    padding: 0;
}
SelectionList > .selection-list--option-highlighted,
SelectionList > .selection-list--option-highlighted-selected {
    background: $accent 22%;
    color: $foreground;
}

/* -- text entry: the amber cursor is the focus marker -------------------- *
 * The drawn frames put typed text flush at the gutter with no chrome around
 * it — the block cursor (accent, from the theme) is what says "you are here".
 * A border here would be decoration, which the accent boundary forbids. */
.line-input {
    height: 1;
    border: none;
    background: transparent;
    padding: 0;
}
.line-input:focus {
    border: none;
    background: transparent;
}

TextArea {
    border: none;
    background: transparent;
    padding: 0;
    scrollbar-size-vertical: 1;
}
TextArea:focus { border: none; }
.compose-area { height: 5; }

RadioSet {
    border: none;
    background: transparent;
    height: auto;
    padding: 0 0 0 2;
}
RadioSet:focus { border: none; }
RadioButton { background: transparent; }

/* -- split panes: only above the side-pane breakpoint ------------------- */
.split { height: 1fr; }
.split-left { width: 1fr; }
.split-right {
    width: 58;
    min-width: 40;
    padding: 0 0 0 2;
    border-left: solid $panel;
    display: none;
}
Screen.-standard .split-right.has-content,
Screen.-wide .split-right.has-content { display: block; }

/* -- modals: centered, bordered, with the "not yet saved" hint inside --- */
ExomemModal { align: center middle; background: $background 75%; }
#modal-box {
    background: $surface;
    border: round $secondary;
    padding: 1 2;
    width: 62;
    max-width: 90%;
    height: auto;
    max-height: 80%;
}
Screen.-narrow #modal-box { width: 90%; }
#modal-box OptionList { padding: 1 0 0 0; }
"""
