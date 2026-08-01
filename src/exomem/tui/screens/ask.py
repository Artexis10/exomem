"""Ask: measured recall, with evidence — never generated prose.

Exomem is a pure substrate: the server measures, the model reasons. So this
screen never writes an answer. It shows what was retrieved, how long it took,
which lanes were actually running, and a bounded read of the file behind each
hit. `y` hands the assembled context to a reasoning model elsewhere; that is
the only place synthesis is allowed to happen.

The empty state carries the doctrine rather than an apology: a miss means "not
found with these words", not "not there" — and it offers the three real ways
forward (rephrase, widen the scope to the whole vault, or capture what you
already know).
"""

from __future__ import annotations

import re
import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, OptionList, Static, TextArea

from ..backend import AskOutcome, BackendError, RelationReviewRequired
from ..format import fit, truncate_path, wrap
from ..theme import Skin
from ..widgets import (
    AppFooter,
    AppHeader,
    BarOptionList,
    BarRow,
    ConfirmModal,
    ExomemModal,
    RecoveryPanel,
    continuation,
    receipt,
)
from .base import ExomemScreen

WIKILINK = re.compile(r"\[\[[^\]]+\]\]")

DOCTRINE_WORKING = "Results are ranked measurements over your files — never generated."
DOCTRINE_EMPTY = (
    "Nothing above the relevance floor. Recall never invents — a miss means "
    '"not found with these words", not "not there".'
)
PREVIEW_TAIL = (
    "Bounded preview — the file is the truth. y copies the deep-context packet "
    "for a reasoning model."
)


def hit_rows(hits: list[dict], skin: Skin, budget: int) -> list[BarRow]:
    """Two rows per result: the title, then identity · date · path (dim)."""
    rows: list[BarRow] = []
    for index, hit in enumerate(hits):
        title = str(hit.get("title") or hit.get("path") or "(untitled)")
        meta_parts = [str(hit[key]) for key in ("type", "updated") if hit.get(key)]
        path = str(hit.get("path") or "")
        lines = [(1, Text(fit(title, budget - 2), style=skin.text))]
        if meta_parts or path:
            meta = f" {skin.g('bullet')} ".join(meta_parts)
            room = budget - 4 - len(meta) - (3 if meta and path else 0)
            if path and room > 12:
                meta = f"{meta} {skin.g('bullet')} {truncate_path(path, room)}" if meta else truncate_path(path, room)
            lines.append((3, Text(fit(meta, budget - 4), style=skin.dim)))
        rows.append(BarRow(f"hit-{index}", lines))
    return rows


def warming_lanes(outcome: AskOutcome) -> list[str]:
    """Which lanes were still loading when this result set was measured."""
    if not outcome.warming:
        return []
    components = outcome.warming.get("components") or outcome.warming.get("pending") or []
    return [str(component) for component in components]


def render_preview(
    path: str, page: dict, hit: dict, skin: Skin, budget: int, limit: int = 3000
) -> Text:
    """A bounded read of the file behind a hit; wikilinks are live paths."""
    body = str(page.get("body") or page.get("content") or "")
    text = Text()
    text.append(f"{skin.g('pointer')} ", style=skin.accent)
    text.append(truncate_path(path, budget - 2), style=skin.text)
    text.append("\n")

    meta_parts = [str(hit[key]) for key in ("type", "updated") if hit.get(key)]
    sources = page.get("compiled_from") or page.get("sources")
    if isinstance(sources, list) and sources:
        meta_parts.append(f"compiled from {len(sources)} sources")
    if meta_parts:
        text.append(f" {skin.g('bullet')} ".join(meta_parts), style=skin.dim)
        text.append("\n")
    text.append(skin.g("hrule") * max(8, min(budget, 52)), style=skin.dim)
    text.append("\n")

    # Wrap the page to the pane's own column so nothing folds mid-word, and
    # light the wikilinks: they are live retrieval paths, not decoration.
    for source_line in body[:limit].splitlines():
        for line in wrap(source_line, budget) or [""]:
            cursor = 0
            for match in WIKILINK.finditer(line):
                text.append(line[cursor : match.start()], style=skin.text)
                text.append(match.group(0), style=skin.accent)
                cursor = match.end()
            text.append(line[cursor:], style=skin.text)
            text.append("\n")
    if len(body) > limit:
        text.append(f"{skin.g('ellipsis')} bounded\n", style=skin.dim)
    text.append("\n")
    for line in wrap(PREVIEW_TAIL, budget):
        text.append(line + "\n", style=skin.dim)
    return text


def pack_to_text(pack: dict) -> str:
    """Flatten a deep-context pack into a paste-ready plain-text packet."""
    lines: list[str] = ["# Exomem context packet"]
    for key, value in pack.items():
        lines.append("")
        lines.append(f"## {key}")
        if isinstance(value, list):
            for item in value:
                lines.append(f"- {item}")
        else:
            lines.append(str(value))
    return "\n".join(lines)


class WriteBackModal(ExomemModal):
    """Turn a conclusion into durable memory — explicit, editable, governed."""

    BINDINGS = [
        Binding("escape", "cancel", "back", show=False),
        Binding("ctrl+s", "save", "save"),
    ]

    def __init__(self, suggested_title: str):
        super().__init__("Save as an insight", [], [], "")
        self._suggested_title = suggested_title

    def compose(self) -> ComposeResult:
        skin: Skin = self.app.skin
        with Vertical(id="modal-box"):
            yield Static(Text("Save as an insight", style=f"bold {skin.text}"))
            yield Static(
                Text(
                    "A compiled conclusion, written through the governed path. The "
                    "Observations line is its semantic unit — say what you concluded.",
                    style=skin.secondary,
                )
            )
            yield Input(value=self._suggested_title, placeholder="title", id="writeback-title")
            yield TextArea(id="writeback-content")
            yield Static(Text("^s save · esc back — nothing saved yet", style=skin.dim))

    def on_mount(self) -> None:
        area = self.query_one("#writeback-content", TextArea)
        if not area.text:
            area.text = f"\n\n## Observations\n- [conclusion] {self._suggested_title}\n"
        area.focus()

    def action_save(self) -> None:
        title = self.query_one("#writeback-title", Input).value.strip()
        content = self.query_one("#writeback-content", TextArea).text.strip()
        if not title or not content:
            self.app.notify("A title and content are both needed.", severity="warning")
            return
        self.dismiss({"title": title, "content": content})

    def action_cancel(self) -> None:
        self.dismiss(None)


class DetailModal(ExomemModal):
    """Full-screen detail for layouts too narrow to hold a side pane."""

    BINDINGS = [Binding("escape", "cancel", "back", show=False)]

    def __init__(self, title: str, body: Text):
        super().__init__(title, [], [], "esc returns — nothing here changes state")
        self._body = body

    def compose(self) -> ComposeResult:
        skin: Skin = self.app.skin
        with Vertical(id="modal-box"):
            yield Static(Text(self._title, style=f"bold {skin.text}"))
            with VerticalScroll():
                yield Static(self._body)
            yield Static(Text(self._hint, style=skin.dim))

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()


class AskScreen(ExomemScreen):
    SCREEN_TITLE = "Ask"

    FOOTER_KEYS = (
        ("enter", "preview"),
        ("e", "evidence"),
        ("y", "copy context"),
        ("w", "save insight"),
    )

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("escape", "step_back", "back", show=False, priority=True),
        Binding("e", "toggle_preview", "evidence"),
        Binding("y", "copy_context", "copy context"),
        Binding("w", "write_back", "save insight"),
    ]

    PREVIEW_LIMIT = 3000
    #: Stop re-checking a warming engine after this many polls (~1 minute).
    WARM_POLL_LIMIT = 20

    def __init__(self) -> None:
        super().__init__()
        self._outcome: AskOutcome | None = None
        self._elapsed_ms: float = 0.0
        self._last_query = ""
        self._scope = "kb"
        self._searching = False
        self._preview_cache: dict[str, dict] = {}
        self._warm_timer = None
        self._warm_polls = 0

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        with Vertical(id="body", classes="-flush"):
            yield Input(placeholder="what do you want to recall?", id="ask-input", classes="line-input")
            yield Static(id="ask-banner")
            yield Static(id="ask-header")
            with Horizontal(classes="split"):
                with Vertical(classes="split-left"):
                    yield BarOptionList(id="ask-results")
                with VerticalScroll(classes="split-right", id="ask-detail"):
                    yield Static(id="ask-detail-body")
            yield RecoveryPanel(id="ask-recovery")
        yield AppFooter()

    def on_mount(self) -> None:
        self.query_one("#ask-results", BarOptionList).display = False
        self.query_one("#ask-input", Input).focus()
        self._set_banner(None)
        self._set_header(None)
        self.set_footer([("enter", "ask"), ("esc", "back")])

    # ------------------------------------------------------------------ #
    # Query lifecycle
    # ------------------------------------------------------------------ #
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "ask-input":
            return
        query = event.value.strip()
        if query:
            self._scope = "kb"
            self._submit(query)

    def _submit(self, query: str) -> None:
        backend = self.app.backend
        scope = self._scope
        self._last_query = query
        self._searching = True
        self._cancel_warm_poll()
        self.query_one("#ask-recovery", RecoveryPanel).hide()
        self.query_one("#ask-results", BarOptionList).display = False
        skin = self.app.skin
        self._set_banner(
            [
                receipt(skin, "working", "working", "measuring recall — lexical + semantic lanes", budget=self.content_budget()),
                Text(f"  {DOCTRINE_WORKING}", style=skin.dim),
            ]
        )
        self._set_header(None)
        self.set_footer([("esc", "cancel — query kept")])

        def job() -> tuple[AskOutcome, float]:
            started = time.perf_counter()
            return backend.ask(query, limit=20, deep=True, scope=scope), started

        self.run_backend(job, self._on_results, self._on_error, group="ask")

    def _on_results(self, payload: tuple[AskOutcome, float]) -> None:
        outcome, started = payload
        elapsed_ms = self.app.elapsed_ms(started)
        self._searching = False
        self._outcome = outcome
        self._elapsed_ms = elapsed_ms
        skin = self.app.skin
        budget = self.content_budget()
        lanes = warming_lanes(outcome)

        if lanes:
            self._set_banner(
                [
                    receipt(
                        skin,
                        "warn",
                        "partial",
                        f"still warming: {', '.join(lanes)} — lexical results below",
                        budget=budget,
                    ),
                    continuation(skin, "re-runs by itself when warm; u re-runs now", indent=2),
                ]
            )
            self._start_warm_poll()
        else:
            self._set_banner(None)

        results = self.query_one("#ask-results", BarOptionList)
        recovery = self.query_one("#ask-recovery", RecoveryPanel)
        count = len(outcome.hits)
        suffix = " · lexical lane only" if lanes else ""
        header_right = "enter previews · y copies context" if count else ""
        self._set_header(
            self._retrieved_line(
                f"{count} result{'s' if count != 1 else ''} · {elapsed_ms:.0f} ms{suffix}",
                header_right,
            )
        )

        if count:
            recovery.hide()
            results.set_rows(hit_rows(outcome.hits, skin, self.list_budget()))
            results.display = True
            results.focus()
            self.set_footer(
                [("enter", "preview"), ("u", "re-run"), ("y", "copy context"), ("w", "save insight")]
            )
            self._show_detail(None)
            return

        results.display = False
        options = [
            ("rephrase", "Rephrase", "the query above stays editable"),
            ("capture", "Capture what you know now", "opens Capture — 3"),
        ]
        if self._scope != "vault":
            options.insert(
                1, ("scope-vault", "Search the whole vault, not just compiled notes", "includes raw sources")
            )
        recovery.show(
            state="idle",
            word="no match",
            what=DOCTRINE_EMPTY.split(" Recall never")[0].strip(),
            facts=wrap("Recall never invents — " + DOCTRINE_EMPTY.split("Recall never invents — ")[1], budget - 3),
            options=options,
            budget=budget,
        )
        self.set_footer([("enter", "choose"), ("esc", "edit query")])

    def _retrieved_line(self, detail: str, right: str = "") -> list[Text]:
        skin = self.app.skin
        budget = self.content_budget()
        line = Text(no_wrap=True)
        line.append(f"{skin.g('pointer')} retrieved   ", style=skin.accent)
        line.append(fit(detail, budget - 14 - (len(right) + 2 if right else 0)), style=skin.text)
        if right:
            pad = max(1, budget - len(line.plain) - len(right))
            line.append(" " * pad)
            line.append(right, style=skin.dim)
        return [line]

    def _on_error(self, error: BackendError) -> None:
        self._searching = False
        self._cancel_warm_poll()
        self._set_banner(None)
        self._set_header(None)
        self.query_one("#ask-results", BarOptionList).display = False
        self.query_one("#ask-recovery", RecoveryPanel).show(
            state="fail",
            word="recall failed",
            what=error.message,
            facts=["Nothing was changed. Your query is still above."]
            + ([error.remediation] if error.remediation else []),
            options=[
                ("retry", "Try again", "re-runs the same query"),
                ("status", "Open Status", "the doctor report names the failing lane — 7"),
                ("rephrase", "Edit the query", "your typing is kept"),
            ],
            budget=self.content_budget(),
        )
        self.set_footer([("enter", "choose"), ("esc", "edit query")])

    # ------------------------------------------------------------------ #
    # Warming: re-run once the lanes finish, without polling forever.
    # ------------------------------------------------------------------ #
    def _start_warm_poll(self) -> None:
        self._warm_polls = 0
        if self._warm_timer is None:
            self._warm_timer = self.set_interval(3.0, self._poll_warm)
        else:
            self._warm_timer.resume()

    def _cancel_warm_poll(self) -> None:
        if self._warm_timer is not None:
            self._warm_timer.pause()

    def _poll_warm(self) -> None:
        self._warm_polls += 1
        if self._warm_polls > self.WARM_POLL_LIMIT or not self._last_query:
            self._cancel_warm_poll()
            return
        backend = self.app.backend

        def done(state: dict) -> None:
            if not state.get("warming"):
                self._cancel_warm_poll()
                self._submit(self._last_query)

        self.run_backend(backend.readiness, done, lambda _error: self._cancel_warm_poll(), group="warm-poll")

    # ------------------------------------------------------------------ #
    # Chrome helpers
    # ------------------------------------------------------------------ #
    def _set_banner(self, lines: list[Text] | None) -> None:
        self._set_block("#ask-banner", lines)

    def _set_header(self, lines: list[Text] | None) -> None:
        self._set_block("#ask-header", lines)

    def _set_block(self, selector: str, lines: list[Text] | None) -> None:
        widget = self.query_one(selector, Static)
        if not lines:
            widget.update("")
            widget.display = False
            return
        block = Text()
        for index, line in enumerate(lines):
            if index:
                block.append("\n")
            block.append("  ")
            block.append_text(line)
        widget.update(block)
        widget.display = True

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def action_step_back(self) -> None:
        """esc unwinds one layer at a time and never discards the query."""
        if self._searching:
            self.supersede("ask")
            self._searching = False
            self._set_banner(None)
            self._set_header(
                [Text("cancelled — nothing was changed; your query is still above", style=self.app.skin.dim)]
            )
            self.query_one("#ask-input", Input).focus()
            self.set_footer([("enter", "ask"), ("esc", "back")])
            return
        results = self.query_one("#ask-results", BarOptionList)
        recovery = self.query_one("#ask-recovery", RecoveryPanel)
        if (results.display and self.focused is results) or recovery.has_class("visible"):
            self.query_one("#ask-input", Input).focus()
            self.set_footer([("enter", "ask"), ("esc", "back")])
            return
        self.app.back()

    def prefill(self, query: str) -> None:
        """Open with a question already asked — used by Capture's receipt."""
        self.query_one("#ask-input", Input).value = query
        self._scope = "kb"
        self._submit(query)

    def refresh_data(self) -> None:
        if self._last_query:
            self._submit(self._last_query)

    def on_recovery_panel_chosen(self, event: RecoveryPanel.Chosen) -> None:
        event.stop()
        if event.action == "rephrase":
            self.query_one("#ask-recovery", RecoveryPanel).hide()
            self.query_one("#ask-input", Input).focus()
            self.set_footer([("enter", "ask"), ("esc", "back")])
        elif event.action == "scope-vault":
            self._scope = "vault"
            self._submit(self._last_query)
        elif event.action == "capture":
            self.app.goto("capture")
        elif event.action == "status":
            self.app.goto("status")
        elif event.action == "retry":
            self._submit(self._last_query)

    # ------------------------------------------------------------------ #
    # Evidence
    # ------------------------------------------------------------------ #
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "ask-results":
            return
        event.stop()
        self._preview_hit(event.option_index)

    def action_toggle_preview(self) -> None:
        results = self.query_one("#ask-results", BarOptionList)
        self._preview_hit(results.highlighted if results.highlighted is not None else 0)

    def _preview_hit(self, index: int | None) -> None:
        if not self._outcome or not self._outcome.hits:
            return
        if index is None or index >= len(self._outcome.hits):
            return
        hit = self._outcome.hits[index]
        path = str(hit.get("path") or "")
        if not path:
            return
        cached = self._preview_cache.get(path)
        if cached is not None:
            self._show_preview(path, cached, hit)
            return
        backend = self.app.backend

        def job() -> dict:
            return backend.read_page(path)

        def done(page: dict) -> None:
            self._preview_cache[path] = page
            self._show_preview(path, page, hit)

        self.run_backend(job, done, self._on_error, group="preview")

    def _show_preview(self, path: str, page: dict, hit: dict) -> None:
        text = render_preview(
            path, page, hit, self.app.skin, self.detail_budget(), self.PREVIEW_LIMIT
        )
        if self.side_pane_open():
            self._show_detail(text)
        else:
            self.app.push_screen(DetailModal(truncate_path(path, 46), text))

    def _show_detail(self, text: Text | None) -> None:
        detail = self.query_one("#ask-detail")
        body = self.query_one("#ask-detail-body", Static)
        if text is None:
            body.update("")
            detail.remove_class("has-content")
        else:
            body.update(text)
            detail.add_class("has-content")

    # ------------------------------------------------------------------ #
    # Context packet + write-back
    # ------------------------------------------------------------------ #
    def action_copy_context(self) -> None:
        if not self._outcome:
            self.app.notify("Ask something first.", severity="warning")
            return
        if self._outcome.pack:
            packet = pack_to_text(self._outcome.pack)
        else:
            lines = ["# Exomem recall", f"query: {self._last_query}"]
            lines += [f"- {hit.get('title')}  ({hit.get('path')})" for hit in self._outcome.hits]
            packet = "\n".join(lines)
        self.app.copy_to_clipboard(packet)
        self.app.notify("Context packet copied (needs terminal OSC 52 clipboard support).")

    def action_write_back(self) -> None:
        suggested = self._last_query[:80] if self._last_query else "New insight"

        def on_close(result: dict | None) -> None:
            if not result:
                return
            backend = self.app.backend

            def done(saved: dict) -> None:
                path = str(saved.get("path") or "saved")
                self.app.record_receipt("done", "saved", truncate_path(path, 40))
                self._set_header(
                    self._retrieved_line(f"{len(self._outcome.hits) if self._outcome else 0} results")
                    + [receipt(self.app.skin, "done", "saved", truncate_path(path, 44), budget=self.content_budget())]
                )
                self.app.notify(f"Saved: {path}")

            def failed(error: BackendError) -> None:
                if isinstance(error, RelationReviewRequired):
                    self._confirm_unlinked(error.draft, done)
                    return
                self._on_error(error)

            self.run_backend(
                lambda: backend.remember_note(result["content"], result["title"]),
                done,
                failed,
                group="writeback",
            )

        self.app.push_screen(WriteBackModal(suggested), on_close)

    def _confirm_unlinked(self, draft: dict, done) -> None:
        """The governed relation review, put to the user rather than assumed."""
        backend = self.app.backend

        def on_close(choice: str | None) -> None:
            if choice != "confirm":
                self._set_header([Text("not saved — nothing was written", style=self.app.skin.dim)])
                return
            self.run_backend(
                lambda: backend.commit_unlinked_note(draft), done, self._on_error, group="writeback"
            )

        self.app.push_screen(
            ConfirmModal(
                "Save unlinked?",
                "No typed relation connects this note to the vault.\n"
                "Confirming is a governed review — recorded, not skipped.",
                "Save unlinked — records your review (reviewed_none)",
                "Back to editing — add a [[link]] in the text",
                "enter choose · esc back — nothing saved yet",
            ),
            on_close,
        )

    def on_resize(self, _event) -> None:
        if self._outcome and self._outcome.hits:
            results = self.query_one("#ask-results", BarOptionList)
            results.set_rows(
                hit_rows(self._outcome.hits, self.app.skin, self.list_budget()),
                highlight=results.highlighted or 0,
            )
