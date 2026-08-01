"""Shared widgets: chrome rows, receipt lines, bar lists, modals, recoveries.

Three ideas are shared by every screen and therefore live here.

**Receipts.** An action that happened leaves a line you can read back —
`✓ vault     created ~/Exomem — 14 files, plain markdown`. The same shape
renders setup steps, save confirmations, and session history, so the language
of the first run is the language of the daily loop.

**The bar list.** Selection is a `▌` in column zero plus a tinted row, not a
`❯` pointer glued to the label. Because the bar is part of the rendered
prompt, multi-line rows carry it down their whole height and it degrades to
`>` on ASCII terminals and to reverse video under `NO_COLOR`.

**The recovery panel.** Anything that fails, comes back empty, or degrades
renders as: status glyph + word, one line of what happened, a dim line stating
what was and was not changed, then a list of *selectable* next actions. The
screen stays operable; there is no prose-only dead end and no traceback.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .format import LABEL_FIELD, fit, label_field
from .theme import Skin


# --------------------------------------------------------------------------- #
# Chrome
# --------------------------------------------------------------------------- #
class AppHeader(Horizontal):
    """Row zero: `exomem  <Screen>` left, `<vault> · <mode>` right."""

    def __init__(self, screen_title: str):
        super().__init__(id="app-header")
        self._screen_title = screen_title

    def compose(self) -> ComposeResult:
        left = Text()
        left.append("exomem", style="bold")
        left.append("  ")
        left.append(self._screen_title)
        yield Static(left, classes="header-left")
        yield Static("", classes="header-right")

    def on_mount(self) -> None:
        self.watch(self.app, "context_label", self._update_context)

    def _update_context(self, value: str) -> None:
        self.query_one(".header-right", Static).update(Text(value))


class AppFooter(Horizontal):
    """The last row: this screen's keys, then `? help`; `^p palette` right.

    Written rather than derived from bindings so each state can say what is
    actually available right now — "esc cancel — query kept" while a search
    runs is worth more than a static list of every key the screen owns.
    """

    def __init__(self) -> None:
        super().__init__(id="app-footer")
        self._keys: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Static("", classes="footer-left")
        yield Static("", classes="footer-right")

    def on_mount(self) -> None:
        self._paint()

    def set_keys(self, keys: list[tuple[str, str]]) -> None:
        self._keys = list(keys)
        if self.is_mounted:
            self._paint()

    def _paint(self) -> None:
        skin: Skin = self.app.skin
        left = Text(no_wrap=True, overflow="ellipsis")
        for index, (key, verb) in enumerate([*self._keys, ("?", "help")]):
            if index:
                left.append("   ")
            left.append(key, style=skin.secondary)
            if verb:
                left.append(f" {verb}", style=skin.dim)
        right = Text(no_wrap=True)
        right.append("^p", style=skin.secondary)
        right.append(" palette", style=skin.dim)
        self.query_one(".footer-left", Static).update(left)
        self.query_one(".footer-right", Static).update(right)


# --------------------------------------------------------------------------- #
# Receipts
# --------------------------------------------------------------------------- #
def receipt(
    skin: Skin,
    state: str,
    word: str,
    detail: str = "",
    *,
    right: str = "",
    budget: int = 76,
    struck: bool = False,
    label_width: int = LABEL_FIELD,
) -> Text:
    """`● ready     retrieval warm · 132 notes`, with an optional right column.

    Status is glyph + word first, always; the detail is fitted to what is left
    of the budget so a receipt is exactly one row.
    """
    glyph, style = skin.status(state)
    line = Text(no_wrap=True)
    label = label_field(glyph, word, label_width)
    line.append(label, style=style)
    remaining = max(0, budget - len(label) - (len(right) + 2 if right else 0))
    if detail:
        line.append(fit(detail, remaining), style=skin.struck if struck else skin.text)
    if right:
        pad = max(1, budget - len(line.plain) - len(right))
        line.append(" " * pad)
        line.append(right, style=skin.dim)
    return line


def continuation(skin: Skin, text: str, *, indent: int = 5) -> Text:
    """`   → full recall returns automatically` — a dim what-happens-next line."""
    return Text(f"{' ' * indent}{skin.g('arrow')} {text}", style=skin.dim, no_wrap=True)


# --------------------------------------------------------------------------- #
# Bar list
# --------------------------------------------------------------------------- #
@dataclass
class BarRow:
    """One option: an id plus its already-styled lines and their indents.

    Indents are measured from the bar column, so line one of a prose-style row
    sits at 1 and its subline at 3 — matching the drawn frames exactly.
    """

    id: str
    lines: list[tuple[int, Text]] = field(default_factory=list)

    def render(self, skin: Skin, selected: bool) -> Text:
        bar = skin.g("bar") if selected else " "
        out = Text(no_wrap=True, overflow="ellipsis")
        for index, (indent, content) in enumerate(self.lines):
            if index:
                out.append("\n")
            out.append(bar, style=skin.accent if selected else "")
            out.append(" " * indent)
            out.append_text(lift(content, skin) if selected else content)
        return out


def lift(content: Text, skin: Skin) -> Text:
    """Promote `dim` spans one step for a row sitting on the selection fill.

    `dim` is calibrated to recede against the page background; against the
    lit row it recedes past legibility. Nothing else changes, so the row's
    internal hierarchy survives selection instead of flattening.
    """
    if not skin.color:
        return content
    lifted = content.copy()
    lifted.spans = [
        span._replace(style=skin.secondary) if span.style == skin.dim else span
        for span in lifted.spans
    ]
    if lifted.style == skin.dim:
        lifted.style = skin.secondary
    return lifted


def row(row_id: str, *lines: tuple[int, Text]) -> BarRow:
    return BarRow(row_id, list(lines))


class BarOptionList(OptionList):
    """An OptionList that keeps an accent bar in the selected row's first cell."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rows: list[BarRow] = []
        self._bar_at: int | None = None

    def set_rows(self, rows: list[BarRow], *, highlight: int | None = 0) -> None:
        skin: Skin = self.app.skin
        self._rows = list(rows)
        self.clear_options()
        if not self._rows:
            self._bar_at = None
            return
        target = None if highlight is None else max(0, min(highlight, len(self._rows) - 1))
        self._bar_at = target
        self.add_options(
            [
                Option(entry.render(skin, index == target), id=entry.id)
                for index, entry in enumerate(self._rows)
            ]
        )
        if target is not None:
            self.highlighted = target

    def row_id(self, index: int | None) -> str | None:
        if index is None or index >= len(self._rows):
            return None
        return self._rows[index].id

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        index = event.option_index
        if index == self._bar_at or not self._rows:
            return
        skin: Skin = self.app.skin
        previous = self._bar_at
        self._bar_at = index
        for position in {previous, index}:
            if position is None or position >= len(self._rows):
                continue
            self.replace_option_prompt_at_index(
                position, self._rows[position].render(skin, position == index)
            )


# --------------------------------------------------------------------------- #
# Recovery / empty / error panel
# --------------------------------------------------------------------------- #
class RecoveryPanel(Vertical):
    """Status line → dim fact → selectable next actions. Never a dead end."""

    DEFAULT_CSS = """
    RecoveryPanel { height: auto; display: none; }
    RecoveryPanel.visible { display: block; }
    RecoveryPanel > OptionList { padding: 1 0 0 0; height: auto; max-height: 10; }
    """

    class Chosen(Message):
        def __init__(self, panel: "RecoveryPanel", action: str) -> None:
            super().__init__()
            self.panel = panel
            self.action = action

        @property
        def control(self) -> "RecoveryPanel":
            return self.panel

    def compose(self) -> ComposeResult:
        yield Static(id="recovery-head")
        yield Static(id="recovery-fact", classes="dim")
        yield BarOptionList(id="recovery-options")

    def show(
        self,
        *,
        state: str,
        word: str,
        what: str,
        facts: list[str] | None = None,
        options: list[tuple[str, str, str]] | None = None,
        budget: int = 76,
        focus: bool = True,
    ) -> None:
        """Render one recovery block.

        `options` are `(id, label, sublabel)` triples, best first — the first
        is pre-selected so the recommended path is one `enter` away.
        """
        skin: Skin = self.app.skin
        self.query_one("#recovery-head", Static).update(
            receipt(skin, state, word, what, budget=budget)
        )
        fact_widget = self.query_one("#recovery-fact", Static)
        fact_text = Text(no_wrap=True)
        for index, line in enumerate(facts or []):
            if index:
                fact_text.append("\n")
            fact_text.append(f"   {fit(line, budget - 3)}", style=skin.dim)
        fact_widget.update(fact_text)
        fact_widget.display = bool(facts)
        option_list = self.query_one("#recovery-options", BarOptionList)
        rows: list[BarRow] = []
        for action, label, sublabel in options or []:
            lines = [(1, Text(fit(label, budget - 2), style=skin.text))]
            if sublabel:
                lines.append((3, Text(fit(sublabel, budget - 4), style=skin.secondary)))
            rows.append(BarRow(action, lines))
        option_list.set_rows(rows)
        option_list.display = bool(rows)
        self.add_class("visible")
        if rows and focus:
            option_list.focus()

    def hide(self) -> None:
        self.remove_class("visible")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_id:
            self.post_message(self.Chosen(self, event.option_id))


class EmptyState(Static):
    """A deliberate nothing-here line plus the doctrine that explains it."""

    def __init__(self, message: str, hint: str = "", **kwargs):
        super().__init__(**kwargs)
        self._message = message
        self._hint = hint

    def on_mount(self) -> None:
        skin: Skin = self.app.skin
        text = Text(self._message, style=skin.text)
        if self._hint:
            text.append(f"\n{self._hint}", style=skin.dim)
        self.update(text)


# --------------------------------------------------------------------------- #
# Modals
# --------------------------------------------------------------------------- #
class ExomemModal(ModalScreen[str | None]):
    """Bordered box: title, body, options, and a hint saying what is NOT saved.

    `esc` always closes with no effect — the hint line says so in words, so
    the guarantee is visible rather than folklore.
    """

    BINDINGS = [Binding("escape", "cancel", "back", show=False)]

    def __init__(
        self,
        title: str,
        body: list[str],
        options: list[tuple[str, str]],
        hint: str,
    ):
        super().__init__()
        self._title = title
        self._body = body
        self._options = options
        self._hint = hint

    def compose(self) -> ComposeResult:
        skin: Skin = self.app.skin
        with Vertical(id="modal-box"):
            yield Static(Text(self._title, style=f"bold {skin.text}"))
            for line in self._body:
                yield Static(Text(line, style=skin.secondary))
            yield BarOptionList(id="modal-options")
            yield Static(Text(self._hint, style=skin.dim))

    def on_mount(self) -> None:
        # Subclasses may compose their own body without an option list; this
        # handler runs for them too (Textual dispatches down the whole MRO).
        found = self.query("#modal-options")
        if not found:
            return
        skin: Skin = self.app.skin
        options = found.first(BarOptionList)
        options.set_rows(
            [
                BarRow(action, [(1, Text(label, style=skin.text))])
                for action, label in self._options
            ]
        )
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """The single dismissal point.

        Textual dispatches a handler to every class in the MRO, so subclasses
        override `choose` rather than this handler — two handlers both calling
        `dismiss` would pop the screen underneath as well.
        """
        event.stop()
        result = self.choose(str(event.option_id or ""))
        if result is self.KEEP_OPEN:
            return
        self.dismiss(result)

    #: Returned by `choose` when the modal needs more input before it closes.
    KEEP_OPEN: object = object()

    def choose(self, action: str) -> object:
        return action

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ExomemModal):
    """A two-way governed question; returns "confirm" only on explicit consent."""

    def __init__(self, title: str, body: str, confirm: str, cancel: str, hint: str):
        super().__init__(
            title,
            [line for line in body.split("\n") if line],
            [("confirm", confirm), ("cancel", cancel)],
            hint,
        )

    def choose(self, action: str) -> object:
        return "confirm" if action == "confirm" else None


class HelpModal(ModalScreen[None]):
    """The `?` overlay: what this screen's keys do, then the global ones."""

    BINDINGS = [
        Binding("escape", "dismiss_help", "close"),
        Binding("question_mark", "dismiss_help", "close", show=False),
    ]

    def __init__(self, title: str, rows: list[tuple[str, str]]):
        super().__init__()
        self._title = title
        self._rows = rows

    def compose(self) -> ComposeResult:
        skin: Skin = self.app.skin
        with Vertical(id="modal-box"):
            yield Static(Text(f"Keys — {self._title}", style=f"bold {skin.text}"))
            with VerticalScroll():
                for key, description in self._rows:
                    line = Text()
                    line.append(f"{key:>10}  ", style=skin.secondary)
                    line.append(description, style=skin.dim)
                    yield Static(line)
            yield Static(Text("esc closes this overlay — nothing here changes state", style=skin.dim))

    def action_dismiss_help(self) -> None:
        self.dismiss(None)
