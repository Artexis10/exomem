"""Shared TUI widgets: header, status text, errors, help, confirmation."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from .backend import BackendError
from .theme import STYLE_ACCENT, STYLE_FAIL


class AppHeader(Horizontal):
    """One-line chrome: product + screen on the left, vault + mode right."""

    DEFAULT_CSS = """
    AppHeader { dock: top; height: 1; background: $panel; padding: 0 1; }
    AppHeader > .header-left { width: 1fr; color: $secondary; }
    AppHeader > .header-right { width: auto; color: $secondary; }
    """

    def __init__(self, screen_title: str):
        super().__init__(id="app-header")
        self._screen_title = screen_title

    def compose(self) -> ComposeResult:
        left = Text()
        left.append("exomem", style=f"bold {STYLE_ACCENT}")
        left.append("  ")
        left.append(self._screen_title)
        yield Static(left, classes="header-left")
        yield Static("", classes="header-right")

    def on_mount(self) -> None:
        self.watch(self.app, "context_label", self._update_context)

    def _update_context(self, value: str) -> None:
        self.query_one(".header-right", Static).update(Text(value, style="dim"))


class ErrorNotice(Static):
    """A structured failure: glyph + code — message, remediation underneath."""

    DEFAULT_CSS = """
    ErrorNotice { padding: 0 1; }
    """

    def show_error(self, error: BackendError | None) -> None:
        if error is None:
            self.update("")
            self.display = False
            return
        glyphs = getattr(self.app, "glyphs", {})
        text = Text()
        text.append(f"{glyphs.get('fail', 'x')} ", style=STYLE_FAIL)
        text.append(f"{error.code}", style=f"bold {STYLE_FAIL}")
        text.append(f"  {error.message}")
        if error.remediation:
            text.append(f"\n  {glyphs.get('arrow', '->')} {error.remediation}", style="dim")
        self.update(text)
        self.display = True


class EmptyState(Static):
    """An intentional nothing-here message with the next step."""

    def __init__(self, message: str, hint: str = "", **kwargs):
        super().__init__(**kwargs)
        self.add_class("empty-state")
        self._message = message
        self._hint = hint

    def on_mount(self) -> None:
        text = Text(self._message)
        if self._hint:
            text.append(f"\n{self._hint}", style="dim")
        self.update(text)


class ConfirmModal(ModalScreen[bool]):
    """Keyboard confirmation: y confirms, n/escape declines."""

    BINDINGS = [
        Binding("y", "confirm", "yes"),
        Binding("n", "decline", "no"),
        Binding("escape", "decline", "cancel", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmModal { align: center middle; background: $background 60%; }
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__()
        self._message = message
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self._message)
            if self._detail:
                yield Static(Text(self._detail, style="dim"))
            yield Static(Text("y confirm   n cancel", style="dim"))

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_decline(self) -> None:
        self.dismiss(False)


class HelpModal(ModalScreen[None]):
    """The `?` overlay: active key bindings for the screen underneath."""

    BINDINGS = [
        Binding("escape", "dismiss_help", "close"),
        Binding("question_mark", "dismiss_help", "close", show=False),
    ]

    DEFAULT_CSS = """
    HelpModal { align: center middle; background: $background 60%; }
    HelpModal .help-title { text-style: bold; padding: 0 0 1 0; }
    """

    def __init__(self, title: str, rows: list[tuple[str, str]]):
        super().__init__()
        self._title = title
        self._rows = rows

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(f"Keys — {self._title}", classes="help-title")
            with VerticalScroll():
                for key, description in self._rows:
                    line = Text()
                    line.append(f"{key:>10}  ", style="bold")
                    line.append(description, style="dim")
                    yield Static(line)
            yield Static(Text("esc closes this overlay", style="dim"))

    def action_dismiss_help(self) -> None:
        self.dismiss(None)
