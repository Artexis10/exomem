"""Review: the Epistemic Inbox as an actionable queue.

Exactly the triage the backend supports — dismiss, snooze (dated), reopen —
bound to each item's fingerprint so a stale action refreshes instead of
writing. Deeper proposal-first flows (supersede, compile, relations) belong to
the Review Studio; this screen says so instead of faking buttons.
"""

from __future__ import annotations

import datetime as _dt

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..backend import BackendError
from ..theme import STYLE_FAIL, STYLE_WARN
from ..widgets import AppHeader, EmptyState, ErrorNotice
from .ask import fit, short_path
from .base import ExomemScreen

_SEVERITY_STYLE = {"critical": STYLE_FAIL, "warning": STYLE_WARN, "info": "dim"}

STATE_VIEWS = ("open", "snoozed", "dismissed", "all")


def item_row(item: dict, glyphs: dict[str, str], budget: int = 76) -> Text:
    severity = str(item.get("severity") or "info").lower()
    categories = ", ".join(
        str(category).replace("_", " ") for category in item.get("categories") or []
    )
    reasons = item.get("reasons") or []
    detail = str(reasons[0].get("detail", "")) if reasons else ""
    row = Text()
    style = _SEVERITY_STYLE.get(severity, "dim")
    glyph = glyphs.get("warn", "!") if severity in ("critical", "warning") else glyphs.get("idle", "o")
    row.append(f"{glyph} ", style=style)
    row.append(fit(categories or "review", budget - 2), style="bold")
    if detail:
        row.append(f"\n   {fit(detail, budget - 3)}")
    path = str(item.get("path") or "")
    if path:
        row.append(f"\n   {fit(short_path(path), budget - 3)}", style="dim")
    return row


class SnoozeModal(ModalScreen[str | None]):
    """Snooze needs a date — suggested two weeks out, editable."""

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        suggested = (_dt.date.today() + _dt.timedelta(days=14)).isoformat()
        with Vertical(id="modal-box"):
            yield Label("Snooze until (YYYY-MM-DD)")
            yield Input(value=suggested, id="snooze-until")
            yield Static(Text("enter confirms   esc cancels", style="dim"))

    def on_mount(self) -> None:
        self.query_one("#snooze-until", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        try:
            _dt.date.fromisoformat(value)
        except ValueError:
            self.app.notify("Use a real YYYY-MM-DD date.", severity="warning")
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReviewScreen(ExomemScreen):
    SCREEN_TITLE = "Review"

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("d", "triage('dismiss')", "dismiss"),
        Binding("s", "triage('snooze')", "snooze"),
        Binding("o", "triage('reopen')", "reopen"),
        Binding("v", "cycle_state", "view"),
        Binding("u", "refresh", "refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._items: list[dict] = []
        self._state_view = "open"
        self._summary: dict = {}

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        yield Static(id="review-summary", classes="pane")
        error = ErrorNotice(id="review-error")
        error.display = False
        yield error
        with Horizontal(id="ask-body"):
            with Vertical(id="review-list-pane"):
                yield OptionList(id="review-list")
                yield EmptyState(
                    "Nothing needs attention in this view.",
                    "Contradictions, stale conclusions, and unprocessed sources appear here as they are measured.",
                    id="review-empty",
                )
            with VerticalScroll(id="ask-detail"):
                yield Static(id="review-detail-body")
        yield Static(
            Text(
                "Deeper flows (supersede, compile, accept relations) live in the Review Studio: "
                "serve http (`exomem`) and open /studio/ — a stdio-only setup has no Studio running.",
                style="dim",
            ),
            classes="pane",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#review-list", OptionList).display = False
        self.action_refresh()

    # ------------------------------------------------------------------ #
    def action_refresh(self) -> None:
        backend = self.app.backend
        self.query_one("#review-summary", Static).update(Text("loading the queue…", style="dim"))
        self.query_one("#review-error", ErrorNotice).show_error(None)
        state = self._state_view

        self.run_backend(
            lambda: backend.attention(limit=50, state=state),
            self._on_queue,
            self._on_error,
            group="review-load",
        )

    def action_cycle_state(self) -> None:
        index = STATE_VIEWS.index(self._state_view)
        self._state_view = STATE_VIEWS[(index + 1) % len(STATE_VIEWS)]
        self.action_refresh()

    def _on_queue(self, payload: dict) -> None:
        self._items = list(payload.get("items") or [])
        self._summary = payload
        options = self.query_one("#review-list", OptionList)
        empty = self.query_one("#review-empty", EmptyState)
        options.clear_options()
        summary = Text()
        shown = payload.get("shown", len(self._items))
        total = payload.get("total", len(self._items))
        all_total = payload.get("all_total", total)
        summary.append(f"view: {self._state_view}  ", style="bold")
        summary.append(f"{shown} shown of {total} in view, {all_total} total", style="dim")
        states = payload.get("state_summary") or {}
        hidden = [f"{states[name]} {name}" for name in ("snoozed", "dismissed") if states.get(name)]
        if hidden and self._state_view == "open":
            summary.append(f"  ({', '.join(hidden)} hidden — v cycles views)", style="dim")
        self.query_one("#review-summary", Static).update(summary)
        if self._items:
            budget = max(24, (options.size.width or self.size.width) - 4)
            for index, item in enumerate(self._items):
                options.add_option(Option(item_row(item, self.app.glyphs, budget), id=f"item-{index}"))
            options.display = True
            empty.display = False
            options.highlighted = 0
            options.focus()
        else:
            options.display = False
            empty.display = True
        self._show_detail(None)

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#review-summary", Static).update("")
        self.query_one("#review-error", ErrorNotice).show_error(error)

    # ------------------------------------------------------------------ #
    def _selected_item(self) -> dict | None:
        options = self.query_one("#review-list", OptionList)
        index = options.highlighted
        if index is None or not self._items or index >= len(self._items):
            return None
        return self._items[index]

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "review-list":
            return
        item = self._items[event.option_index] if event.option_index < len(self._items) else None
        if item is None:
            return
        ref = str(item.get("ref") or "")
        backend = self.app.backend

        def job() -> dict:
            return backend.item_context(ref)

        def done(context: dict) -> None:
            text = Text()
            text.append(str(item.get("path") or ref), style="dim")
            text.append("\n\n")
            body = str(context.get("body") or "")
            text.append(body[:3000])
            related = context.get("related") or []
            if related:
                text.append("\n\nRelated:\n", style="bold")
                for entry in related[:6]:
                    text.append(f"  {short_path(str(entry))}\n", style="dim")
            self._show_detail(text)

        self.run_backend(job, done, self._on_error, group="review-context")

    def _show_detail(self, text: Text | None) -> None:
        detail = self.query_one("#ask-detail")
        body = self.query_one("#review-detail-body", Static)
        if text is None:
            body.update("")
            detail.remove_class("has-content")
        elif self.has_class("-wide"):
            body.update(text)
            detail.add_class("has-content")
        else:
            from .ask import PreviewModal

            self.app.push_screen(PreviewModal("review item", text))

    # ------------------------------------------------------------------ #
    def action_triage(self, action: str) -> None:
        item = self._selected_item()
        if item is None:
            self.app.notify("Select an item first.", severity="warning")
            return
        if action == "snooze":
            def on_close(until: str | None) -> None:
                if until:
                    self._run_triage(item, "snooze", until=until)

            self.app.push_screen(SnoozeModal(), on_close)
            return
        self._run_triage(item, action)

    def _run_triage(self, item: dict, action: str, *, until: str | None = None) -> None:
        backend = self.app.backend
        ref = str(item.get("ref") or "")
        fingerprint = item.get("fingerprint")

        def job() -> dict:
            return backend.triage(
                ref,
                action,
                until=until,
                expected_fingerprint=str(fingerprint) if fingerprint else None,
            )

        def done(_result: dict) -> None:
            self.app.notify(f"Item {action}ed." if action != "snooze" else f"Snoozed until {until}.")
            self.action_refresh()

        def failed(error: BackendError) -> None:
            if error.code == "REVIEW_ITEM_CHANGED":
                self.app.notify(
                    "That item changed since it was listed — the queue was refreshed.",
                    severity="warning",
                )
                self.action_refresh()
                return
            self._on_error(error)

        self.run_backend(job, done, failed, group="review-triage")
