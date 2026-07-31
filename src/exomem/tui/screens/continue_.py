"""Continue: pick up recent work from local continuation checkpoints.

Read-only over the client hooks' checkpoint store, via the public reader in
`install_hook`. A selected checkpoint renders as the exact resume packet the
hooks themselves emit, ready to copy into a fresh session. Honest empty state
when hooks are not installed or nothing was recorded.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from ..backend import BackendError
from ..widgets import AppHeader, EmptyState, ErrorNotice
from .ask import fit
from .base import ExomemScreen


def checkpoint_age(observed_at_ns: int | None, now_ns: int | None = None) -> str:
    if not observed_at_ns:
        return "unknown age"
    now = now_ns if now_ns is not None else time.time_ns()
    seconds = max(0, (now - observed_at_ns) // 1_000_000_000)
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def checkpoint_row(entry: dict, budget: int = 76) -> Text:
    client = str(entry.get("client") or "?")
    session = str(entry.get("session") or "?")
    status = str(entry.get("status") or "")
    row = Text()
    row.append(fit(f"{client}  {session}", budget), style="bold")
    detail = checkpoint_age(entry.get("observed_at_ns"))
    if status and status != "valid":
        detail += f" · {status}"
    row.append(f"\n  {fit(detail, budget - 2)}", style="dim")
    return row


class ContinueScreen(ExomemScreen):
    SCREEN_TITLE = "Continue"

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("y", "copy_packet", "copy packet"),
        Binding("u", "refresh", "refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[dict] = []
        self._packet: str | None = None

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        yield Static(
            Text(
                "Checkpoints recorded by the client hooks at compaction and session end. "
                "Structural pointers, not memory — resume from evidence.",
                style="dim",
            ),
            classes="pane",
        )
        error = ErrorNotice(id="continue-error")
        error.display = False
        yield error
        with Horizontal(id="ask-body"):
            with Vertical(id="continue-list-pane"):
                yield OptionList(id="continue-list")
                yield EmptyState(
                    "No continuation checkpoints on this machine.",
                    "They appear once the client hooks are installed: exomem install-hook",
                    id="continue-empty",
                )
            with VerticalScroll(id="ask-detail"):
                yield Static(id="continue-detail-body")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#continue-list", OptionList).display = False

    def on_screen_resume(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        backend = self.app.backend
        self.run_backend(backend.continuations, self._on_entries, self._on_error, group="continue")

    def _on_entries(self, entries: list[dict]) -> None:
        self._entries = list(entries or [])
        options = self.query_one("#continue-list", OptionList)
        empty = self.query_one("#continue-empty", EmptyState)
        options.clear_options()
        if self._entries:
            budget = max(24, (options.size.width or self.size.width) - 4)
            for index, entry in enumerate(self._entries):
                options.add_option(Option(checkpoint_row(entry, budget), id=f"ckpt-{index}"))
            options.display = True
            empty.display = False
            options.highlighted = 0
            options.focus()
        else:
            options.display = False
            empty.display = True

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#continue-error", ErrorNotice).show_error(error)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "continue-list":
            return
        if event.option_index >= len(self._entries):
            return
        entry = self._entries[event.option_index]
        backend = self.app.backend

        def job() -> str:
            return backend.continuation_packet(entry)

        def done(packet: str) -> None:
            self._packet = packet
            text = Text()
            text.append("resume packet — y copies it\n\n", style="dim")
            text.append(packet)
            detail = self.query_one("#ask-detail")
            body = self.query_one("#continue-detail-body", Static)
            if self.has_class("-wide"):
                body.update(text)
                detail.add_class("has-content")
            else:
                from .ask import PreviewModal

                self.app.push_screen(PreviewModal("resume packet", text))

        self.run_backend(job, done, self._on_error, group="continue-packet")

    def action_copy_packet(self) -> None:
        if not self._packet:
            self.app.notify("Open a checkpoint first (enter).", severity="warning")
            return
        self.app.copy_to_clipboard(self._packet)
        self.app.notify("Resume packet copied to the clipboard.")
