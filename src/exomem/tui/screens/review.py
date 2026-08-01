"""Review: the epistemic inbox as a queue you can actually work.

Items surface; they never act on their own. Each row leads with what the
corpus *measured* — "a newer note reaches the opposite conclusion" — because a
severity chip alone tells you nothing you can act on.

Triage is exactly what the backend supports: dismiss, snooze, reopen, each
bound to the item's fingerprint so a stale action refreshes instead of writing
over something that changed. A triaged item stays where it was, struck
through, with `o reopens` on the same line — the queue does not silently
rearrange itself under your cursor. Deeper proposal-first flows (supersede,
compile, accept relations) live in the Review Studio, and this screen says so
rather than drawing a button that cannot work.
"""

from __future__ import annotations

import datetime as _dt

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, OptionList, Static

from ..backend import BackendError
from ..format import fit, truncate_path, wrap
from ..theme import Skin
from ..widgets import (
    AppFooter,
    AppHeader,
    BarOptionList,
    BarRow,
    ExomemModal,
    RecoveryPanel,
)
from .base import ExomemScreen

STATE_VIEWS = ("open", "snoozed", "dismissed", "all")

#: Category → (word, status) — the measured kind, in the user's language.
KINDS = {
    "corpus_contradictions": ("contradiction", "warn"),
    "contradiction": ("contradiction", "warn"),
    "unprocessed_source": ("unprocessed", "idle"),
    "stale_conclusion": ("stale", "idle"),
    "staleness": ("stale", "idle"),
}

#: Width of the kind field, measured from the selection-bar column.
KIND_FIELD = 17
#: Label field inside the context pane.
CONTEXT_FIELD = 10

STUDIO = (
    "Deeper flows — supersede, compile, accept relations — live in the "
    "Review Studio: exomem serve http, then /studio/."
)

EMPTY_DOCTRINE = (
    "Queues fill as the corpus is measured — contradictions, staleness, "
    "unprocessed sources. They surface; they never act on their own."
)


def item_kind(item: dict) -> tuple[str, str]:
    """The measured kind and its status, from the item's categories."""
    for category in item.get("categories") or []:
        mapped = KINDS.get(str(category))
        if mapped:
            return mapped
    severity = str(item.get("severity") or "info").lower()
    categories = item.get("categories") or []
    word = str(categories[0]).replace("_", " ") if categories else "review"
    return word, "warn" if severity in ("critical", "warning") else "idle"


def item_why(item: dict) -> str:
    reasons = item.get("reasons") or []
    if reasons and isinstance(reasons[0], dict):
        return str(reasons[0].get("detail") or "")
    return ""


def queue_rows(
    items: list[dict], triaged: dict[str, str], skin: Skin, budget: int
) -> list[BarRow]:
    """Two rows per item: kind + measured why, then the left-truncated path."""
    rows: list[BarRow] = []
    for index, item in enumerate(items):
        ref = str(item.get("ref") or "")
        action = triaged.get(ref)
        word, state = item_kind(item)
        why = item_why(item)
        if action:
            glyph, style = skin.status("done")
            head = Text(no_wrap=True)
            head.append(f"{glyph} {action:<{KIND_FIELD - 2}}", style=style)
            head.append(fit(why, budget - KIND_FIELD - 12), style=skin.struck)
            head.append(f" {skin.g('bullet')} o reopens", style=skin.dim)
            rows.append(BarRow(f"item-{index}", [(0, head)]))
            continue
        glyph, style = skin.status(state)
        head = Text(no_wrap=True)
        head.append(f"{glyph} {word:<{KIND_FIELD - 2}}", style=style)
        head.append(fit(why, budget - KIND_FIELD), style=skin.text)
        lines = [(0, head)]
        path = str(item.get("path") or "")
        if path:
            lines.append((4, Text(truncate_path(path, budget - 5), style=skin.dim)))
        rows.append(BarRow(f"item-{index}", lines))
    return rows


def context_lines(item: dict, context: dict, skin: Skin, budget: int) -> Text:
    """The context pane: what was measured, on which pages, and when."""
    word, state = item_kind(item)
    glyph, style = skin.status(state)
    text = Text()
    head = Text(no_wrap=True)
    head.append(f"{glyph} {word}", style=style)
    fingerprint = str(item.get("fingerprint") or "")
    if fingerprint:
        right = f"fingerprint {fingerprint[:8]}{skin.g('ellipsis')}"
        pad = max(1, budget - len(head.plain) - len(right))
        head.append(" " * pad)
        head.append(right, style=skin.dim)
    text.append_text(head)
    text.append("\n\n")

    def field(label: str, value: str, *, style_value: str | None = None) -> None:
        text.append(f"{label:<{CONTEXT_FIELD}}", style=skin.secondary)
        text.append(fit(value, budget - CONTEXT_FIELD), style=style_value or skin.text)
        text.append("\n")

    why = item_why(item)
    if why:
        for index, line in enumerate(wrap(why, budget - CONTEXT_FIELD)):
            field("what" if index == 0 else "", line)

    target = context.get("target") or {}
    target_path = str(target.get("path") or item.get("path") or "")
    if target_path:
        field("page", truncate_path(target_path, budget - CONTEXT_FIELD))
        title = str(target.get("title") or "")
        if title:
            field("", title, style_value=skin.dim)

    related = context.get("related") or []
    if isinstance(related, dict):
        related = related.get("items") or []
    for entry in list(related)[:2]:
        path = str(entry.get("path") or entry) if isinstance(entry, dict) else str(entry)
        field("related", truncate_path(path, budget - CONTEXT_FIELD))

    measured = str(item.get("measured_at") or item.get("observed_at") or target.get("mtime") or "")
    if measured:
        field("measured", measured[:10])

    text.append(skin.g("hrule") * max(8, min(budget, 52)), style=skin.dim)
    text.append("\n")
    field("here", f"d dismiss {skin.g('bullet')} s snooze — bound to the fingerprint", style_value=skin.dim)
    field("studio", f"supersede or reconcile {skin.g('arrow')} exomem serve http, /studio/", style_value=skin.dim)
    return text


class SnoozeModal(ExomemModal):
    """Dated presets plus a free date; every option maps to a real write.

    "After the next sweep" appears in no option list here: the triage contract
    takes a date, and an option the backend cannot honor would be exactly the
    fake affordance this screen refuses to draw.
    """

    def __init__(self) -> None:
        today = _dt.date.today()
        self._tomorrow = (today + _dt.timedelta(days=1)).isoformat()
        self._next_week = (today + _dt.timedelta(days=7)).isoformat()
        super().__init__(
            "Snooze until",
            [],
            [
                ("tomorrow", f"tomorrow           {self._tomorrow}"),
                ("next-week", f"next week          {self._next_week}"),
                ("custom", "a date…            YYYY-MM-DD"),
            ],
            "enter choose · esc back — nothing recorded yet",
        )

    def compose(self) -> ComposeResult:
        skin: Skin = self.app.skin
        with Vertical(id="modal-box"):
            yield Static(Text(self._title, style=f"bold {skin.text}"))
            yield BarOptionList(id="modal-options")
            date_input = Input(placeholder="YYYY-MM-DD", id="snooze-date", classes="line-input")
            date_input.display = False
            yield date_input
            yield Static(
                Text(
                    "Recorded against this item's fingerprint — if the underlying "
                    "files change, it returns early.",
                    style=skin.dim,
                )
            )
            yield Static(Text(self._hint, style=skin.dim))

    def choose(self, action: str) -> object:
        if action == "tomorrow":
            return self._tomorrow
        if action == "next-week":
            return self._next_week
        date_input = self.query_one("#snooze-date", Input)
        date_input.display = True
        date_input.focus()
        return self.KEEP_OPEN

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        try:
            _dt.date.fromisoformat(value)
        except ValueError:
            self.app.notify("Use a real YYYY-MM-DD date.", severity="warning")
            return
        self.dismiss(value)


class ReviewScreen(ExomemScreen):
    SCREEN_TITLE = "Review"

    FOOTER_KEYS = (
        ("enter", "context"),
        ("d", "dismiss"),
        ("s", "snooze"),
        ("o", "reopen"),
        ("v", "view"),
    )

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("d", "triage('dismiss')", "dismiss"),
        Binding("s", "triage('snooze')", "snooze"),
        Binding("o", "triage('reopen')", "reopen"),
        Binding("v", "cycle_state", "view"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._items: list[dict] = []
        self._state_view = "open"
        self._triaged: dict[str, str] = {}
        self._payload: dict = {}

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        with Vertical(id="body", classes="-flush"):
            yield Static(id="review-view")
            with Horizontal(classes="split"):
                with Vertical(classes="split-left"):
                    yield BarOptionList(id="review-list")
                with VerticalScroll(classes="split-right", id="review-detail"):
                    yield Static(id="review-detail-body")
            yield RecoveryPanel(id="review-recovery")
            yield Static(id="review-studio")
        yield AppFooter()

    def on_mount(self) -> None:
        self.query_one("#review-list", BarOptionList).display = False
        skin = self.app.skin
        studio = Text()
        for index, line in enumerate(wrap(STUDIO, self.content_budget() - 2)):
            if index:
                studio.append("\n")
            studio.append(f"  {line}", style=skin.dim)
        self.query_one("#review-studio", Static).update(studio)

    def on_screen_resume(self) -> None:
        self.refresh_data()

    # ------------------------------------------------------------------ #
    def refresh_data(self) -> None:
        backend = self.app.backend
        skin = self.app.skin
        self.query_one("#review-recovery", RecoveryPanel).hide()
        self.query_one("#review-view", Static).update(
            Text(f"  {skin.g('working')} measuring the queue", style=skin.dim)
        )
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
        self.refresh_data()

    def _render_view_line(self) -> None:
        skin = self.app.skin
        budget = self.content_budget()
        shown = self._payload.get("shown", len(self._items))
        total = self._payload.get("total", len(self._items))
        detail = f"{shown} of {total} shown"
        if self._triaged:
            detail += f" {skin.g('bullet')} {len(self._triaged)} triaged this session"
        line = Text(no_wrap=True)
        line.append(f"view {self._state_view:<7}", style=skin.secondary)
        line.append(fit(detail, budget - 20), style=skin.text)
        right = "v cycles"
        pad = max(1, budget - len(line.plain) - len(right))
        line.append(" " * pad)
        line.append(right, style=skin.dim)
        block = Text("  ")
        block.append_text(line)
        self.query_one("#review-view", Static).update(block)

    def _on_queue(self, payload: dict) -> None:
        self._payload = payload
        self._items = list(payload.get("items") or [])
        self._render_view_line()
        options = self.query_one("#review-list", BarOptionList)
        recovery = self.query_one("#review-recovery", RecoveryPanel)
        skin = self.app.skin
        budget = self.content_budget()
        if self._items:
            recovery.hide()
            options.set_rows(queue_rows(self._items, self._triaged, skin, self.list_budget()))
            options.display = True
            options.focus()
            self.set_footer(list(self.FOOTER_KEYS))
        else:
            options.display = False
            recovery.show(
                state="ok",
                word="clear",
                what="nothing needs attention",
                facts=wrap(EMPTY_DOCTRINE, budget - 3),
                options=[
                    ("refresh", "Re-measure now", "u does the same thing"),
                    ("cycle", "Show snoozed and dismissed items", "v cycles the view"),
                ],
                budget=budget,
            )
            self.set_footer([("u", "refresh"), ("v", "view")])
        self._show_detail(None)

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#review-view", Static).update("")
        self.query_one("#review-list", BarOptionList).display = False
        self.query_one("#review-recovery", RecoveryPanel).show(
            state="fail",
            word="queue unavailable",
            what=error.message,
            facts=["Nothing was changed."] + ([error.remediation] if error.remediation else []),
            options=[
                ("refresh", "Try again", ""),
                ("status", "Open Status", "the doctor report names the failing lane — 7"),
            ],
            budget=self.content_budget(),
        )

    def on_recovery_panel_chosen(self, event: RecoveryPanel.Chosen) -> None:
        event.stop()
        if event.action == "refresh":
            self.refresh_data()
        elif event.action == "cycle":
            self.action_cycle_state()
        elif event.action == "status":
            self.app.goto("status")

    # ------------------------------------------------------------------ #
    def _selected_item(self) -> dict | None:
        options = self.query_one("#review-list", BarOptionList)
        index = options.highlighted
        if index is None or not self._items or index >= len(self._items):
            return None
        return self._items[index]

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "review-list":
            return
        event.stop()
        if event.option_index >= len(self._items):
            return
        item = self._items[event.option_index]
        backend = self.app.backend
        ref = str(item.get("ref") or "")

        def done(context: dict) -> None:
            self._show_detail(
                context_lines(item, context, self.app.skin, self.detail_budget())
            )

        self.run_backend(lambda: backend.item_context(ref), done, self._on_error, group="review-context")

    def _show_detail(self, text: Text | None) -> None:
        detail = self.query_one("#review-detail")
        body = self.query_one("#review-detail-body", Static)
        if text is None:
            body.update("")
            detail.remove_class("has-content")
            return
        if self.side_pane_open():
            body.update(text)
            detail.add_class("has-content")
        else:
            from .ask import DetailModal

            self.app.push_screen(DetailModal("Review item", text))

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
            if action == "reopen":
                self._triaged.pop(ref, None)
            else:
                self._triaged[ref] = "dismissed" if action == "dismiss" else "snoozed"
            self.app.record_receipt(
                "done", f"{action}ed", truncate_path(str(item.get("path") or ref), 34)
            )
            self._rerender_rows()

        def failed(error: BackendError) -> None:
            if error.code == "REVIEW_ITEM_CHANGED":
                self.app.notify(
                    "That item changed since it was listed — the queue was re-measured.",
                    severity="warning",
                )
                self.refresh_data()
                return
            self._on_error(error)

        self.run_backend(job, done, failed, group="review-triage")

    def _rerender_rows(self) -> None:
        """Triaged items stay put, struck — the queue never jumps mid-triage."""
        options = self.query_one("#review-list", BarOptionList)
        options.set_rows(
            queue_rows(self._items, self._triaged, self.app.skin, self.list_budget()),
            highlight=options.highlighted or 0,
        )
        self._render_view_line()

    def on_resize(self, _event) -> None:
        if self._items:
            self._rerender_rows()
