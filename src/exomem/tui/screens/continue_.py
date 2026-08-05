"""Continue: pick up recent work from local continuation checkpoints.

Read-only over the client hooks' checkpoint store. A selected checkpoint
renders as the exact resume packet the hooks emit — structural pointers, not
remembered content — ready to paste into a fresh session. When no hooks are
installed the empty state says so and names the one command that changes it.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import OptionList, Static

from ..backend import BackendError
from ..format import fit, wrap
from ..theme import Skin
from ..widgets import AppFooter, AppHeader, BarOptionList, BarRow, RecoveryPanel
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


def checkpoint_rows(entries: list[dict], skin: Skin, budget: int) -> list[BarRow]:
    rows: list[BarRow] = []
    for index, entry in enumerate(entries):
        client = str(entry.get("client") or "?")
        session = str(entry.get("session") or "?")
        status = str(entry.get("status") or "")
        head = Text(no_wrap=True)
        head.append(fit(f"{client}  {session}", budget - 2), style=skin.text)
        detail = checkpoint_age(entry.get("observed_at_ns"))
        if status and status != "valid":
            detail += f" {skin.g('bullet')} {status}"
        rows.append(
            BarRow(
                f"ckpt-{index}",
                [(1, head), (3, Text(fit(detail, budget - 4), style=skin.dim))],
            )
        )
    return rows


class ContinueScreen(ExomemScreen):
    SCREEN_TITLE = "Continue"

    FOOTER_KEYS = (("enter", "open"), ("y", "copy packet"), ("u", "refresh"))

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("y", "copy_packet", "copy packet"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[dict] = []
        self._packet: str | None = None

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        with Vertical(id="body", classes="-flush"):
            yield Static(id="continue-intro")
            with Horizontal(classes="split"):
                with Vertical(classes="split-left"):
                    yield BarOptionList(id="continue-list")
                with VerticalScroll(classes="split-right", id="continue-detail"):
                    yield Static(id="continue-detail-body")
            yield RecoveryPanel(id="continue-recovery")
        yield AppFooter()

    def on_mount(self) -> None:
        skin = self.app.skin
        intro = Text()
        for line in wrap(
            "Checkpoints recorded by the client hooks at compaction and session end. "
            "Structural pointers, not memory — resume from evidence.",
            self.content_budget() - 2,
        ):
            intro.append(f"  {line}\n", style=skin.dim)
        self.query_one("#continue-intro", Static).update(intro)
        self.query_one("#continue-list", BarOptionList).display = False

    def on_screen_resume(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        backend = self.app.backend
        self.query_one("#continue-recovery", RecoveryPanel).hide()
        self.run_backend(backend.continuations, self._on_entries, self._on_error, group="continue")

    def _on_entries(self, entries: list[dict]) -> None:
        self._entries = list(entries or [])
        options = self.query_one("#continue-list", BarOptionList)
        recovery = self.query_one("#continue-recovery", RecoveryPanel)
        if self._entries:
            recovery.hide()
            options.set_rows(checkpoint_rows(self._entries, self.app.skin, self.list_budget()))
            options.display = True
            options.focus()
        else:
            options.display = False
            recovery.show(
                state="idle",
                word="no checkpoints",
                what="nothing has been recorded on this machine yet",
                facts=[
                    "They appear once the client hooks are installed and a session ends.",
                ],
                options=[
                    ("hooks", "Copy the install command", "exomem install-hook"),
                    ("refresh", "Look again", "u does the same thing"),
                ],
                budget=self.content_budget(),
            )

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#continue-recovery", RecoveryPanel).show(
            state="fail",
            word="not readable",
            what=error.message,
            facts=["Nothing was changed."],
            options=[("refresh", "Try again", "")],
            budget=self.content_budget(),
        )

    def on_recovery_panel_chosen(self, event: RecoveryPanel.Chosen) -> None:
        event.stop()
        if event.action == "hooks":
            self.app.copy_to_clipboard("exomem install-hook")
            self.app.notify("`exomem install-hook` copied — run it in a terminal.")
        else:
            self.refresh_data()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "continue-list":
            return
        event.stop()
        if event.option_index >= len(self._entries):
            return
        entry = self._entries[event.option_index]
        backend = self.app.backend

        def done(packet: str) -> None:
            skin = self.app.skin
            self._packet = packet
            text = Text()
            text.append(f"{skin.g('pointer')} resume packet", style=skin.accent)
            text.append("  y copies it\n\n", style=skin.dim)
            text.append(packet, style=skin.text)
            detail = self.query_one("#continue-detail")
            body = self.query_one("#continue-detail-body", Static)
            if self.side_pane_open():
                body.update(text)
                detail.add_class("has-content")
            else:
                from .ask import DetailModal

                self.app.push_screen(DetailModal("Resume packet", text))

        self.run_backend(
            lambda: backend.continuation_packet(entry), done, self._on_error, group="continue-packet"
        )

    def action_copy_packet(self) -> None:
        if not self._packet:
            self.app.notify("Open a checkpoint first (enter).", severity="warning")
            return
        self.app.copy_to_clipboard(self._packet)
        self.app.notify("Resume packet copied to the clipboard.")
