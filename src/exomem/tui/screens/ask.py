"""Ask: recall from the knowledge base, with evidence and honest markers.

Exomem is a pure substrate — the server measures, it does not reason — so Ask
presents ranked recall, not generated prose: results with source identity, a
bounded preview of the underlying page, degradation markers when lanes are
warming or skipped, and a copyable deep-context packet for reasoning
elsewhere. Writes back only through the governed `remember` command, behind an
explicit confirmation.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ..backend import AskOutcome, BackendError, RelationReviewRequired
from ..theme import STYLE_WARN
from ..widgets import AppHeader, ConfirmModal, EmptyState, ErrorNotice
from .base import ExomemScreen


def short_path(path: str, max_len: int = 56) -> str:
    """Vault-path tail for one-line display; full paths live in the preview."""
    if len(path) <= max_len:
        return path
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return path
    return "…/" + "/".join(parts[-2:])


def fit(text: str, budget: int) -> str:
    """Hard-truncate to a cell budget with a trailing ellipsis."""
    if budget <= 1 or len(text) <= budget:
        return text
    return text[: budget - 1] + "…"


def hit_row(hit: dict, glyphs: dict[str, str], budget: int = 76) -> Text:
    """One result: title line, then identity + freshness in dim underneath.

    Lines are pre-fitted to the pane's cell budget — OptionList soft-wraps
    whatever overflows, and a vault path folded mid-word is worse than a
    truncated one (the preview always shows the full path).
    """
    title = str(hit.get("title") or hit.get("path") or "(untitled)")
    row = Text()
    row.append(fit(title, budget))
    meta: list[str] = []
    if hit.get("type"):
        meta.append(str(hit["type"]))
    if hit.get("updated"):
        meta.append(str(hit["updated"]))
    if hit.get("path"):
        meta.append(short_path(str(hit["path"])))
    if meta:
        separator = f" {glyphs.get('bullet', '-')} "
        row.append(f"\n  {fit(separator.join(meta), budget - 2)}", style="dim")
    return row


def marker_summary(outcome: AskOutcome) -> str:
    """The degradation banner text, or '' when recall ran at full fidelity."""
    parts: list[str] = []
    if outcome.warming:
        components = outcome.warming.get("components") or []
        since = outcome.warming.get("since_s")
        detail = ", ".join(str(component) for component in components) or "search lanes"
        suffix = f" (warming {int(since)}s)" if isinstance(since, (int, float)) else ""
        parts.append(f"partial results — still warming: {detail}{suffix}")
    if outcome.degraded:
        parts.append("skipped lanes: " + ", ".join(outcome.degraded))
    return "; ".join(parts)


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


class WriteBackModal(ModalScreen[dict | None]):
    """Turn a conclusion into durable memory — explicit, editable, governed."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
        Binding("ctrl+s", "save", "save"),
    ]

    def __init__(self, suggested_title: str):
        super().__init__()
        self._suggested_title = suggested_title

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("Save as durable memory (remember)")
            yield Static(
                Text(
                    "An insight note through the governed path. The Observations "
                    "line is its semantic unit — edit it to say what you concluded.",
                    style="dim",
                )
            )
            yield Input(value=self._suggested_title, placeholder="title", id="writeback-title")
            yield TextArea(id="writeback-content")
            yield Static(Text("ctrl+s save   esc cancel", style="dim"))

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


class PreviewModal(ModalScreen[None]):
    """Narrow-layout source preview (wide layouts use the side panel)."""

    BINDINGS = [Binding("escape", "close", "close", show=False)]

    def __init__(self, title: str, body: Text):
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self._title)
            with VerticalScroll():
                yield Static(self._body)
            yield Static(Text("esc closes", style="dim"))

    def action_close(self) -> None:
        self.dismiss(None)


class AskScreen(ExomemScreen):
    SCREEN_TITLE = "Ask"

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("escape", "cancel_or_back", "back", show=False, priority=True),
        Binding("e", "toggle_preview", "evidence"),
        Binding("y", "copy_context", "copy context"),
        Binding("w", "write_back", "save insight"),
        Binding("u", "refresh_caches", "refresh", show=False),
    ]

    PREVIEW_LIMIT = 4000

    def __init__(self) -> None:
        super().__init__()
        self._outcome: AskOutcome | None = None
        self._running_since: float | None = None
        self._timer = None
        self._preview_cache: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        yield Input(placeholder="What do you want to recall?", id="ask-input", classes="line-input")
        yield Static(id="ask-status")
        yield Static(id="ask-degraded")
        error = ErrorNotice(id="ask-error")
        error.display = False
        yield error
        with Horizontal(id="ask-body"):
            with Vertical(id="ask-results-pane"):
                yield OptionList(id="ask-results")
                yield EmptyState(
                    "Nothing asked yet.",
                    "Type a question above and press enter — results cite their source pages.",
                    id="ask-empty",
                )
            with VerticalScroll(id="ask-detail"):
                yield Static(id="ask-detail-body")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#ask-results", OptionList).display = False
        self.query_one("#ask-input", Input).focus()

    # ------------------------------------------------------------------ #
    # Query lifecycle
    # ------------------------------------------------------------------ #
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ask-input":
            query = event.value.strip()
            if query:
                self._submit(query)

    def _submit(self, query: str) -> None:
        backend = self.app.backend
        self._running_since = time.monotonic()
        self._set_status("searching")
        if self._timer is None:
            self._timer = self.set_interval(0.5, self._tick_status)
        else:
            self._timer.resume()
        self.query_one("#ask-error", ErrorNotice).show_error(None)
        self.run_backend(
            lambda: backend.ask(query, limit=20, deep=True),
            self._on_results,
            self._on_error,
            group="ask",
        )

    def _tick_status(self) -> None:
        if self._running_since is not None:
            elapsed = time.monotonic() - self._running_since
            self._set_status(f"searching  {elapsed:.1f}s — esc cancels")

    def _set_status(self, message: str) -> None:
        self.query_one("#ask-status", Static).update(Text(message, style="dim"))

    def _result_budget(self) -> int:
        results = self.query_one("#ask-results", OptionList)
        width = results.size.width or self.size.width or 80
        return max(24, width - 4)

    def _populate_results(self, keep_highlight: int | None = 0) -> None:
        results = self.query_one("#ask-results", OptionList)
        results.clear_options()
        if not self._outcome or not self._outcome.hits:
            return
        budget = self._result_budget()
        for index, hit in enumerate(self._outcome.hits):
            results.add_option(Option(hit_row(hit, self.app.glyphs, budget), id=f"hit-{index}"))
        if keep_highlight is not None:
            results.highlighted = min(keep_highlight, len(self._outcome.hits) - 1)

    def on_resize(self, _event) -> None:
        if self._outcome and self._outcome.hits:
            results = self.query_one("#ask-results", OptionList)
            self._populate_results(keep_highlight=results.highlighted or 0)

    def _on_results(self, outcome: AskOutcome) -> None:
        self._running_since = None
        if self._timer is not None:
            self._timer.pause()
        self._outcome = outcome
        results = self.query_one("#ask-results", OptionList)
        empty = self.query_one("#ask-empty", EmptyState)
        results.clear_options()
        if outcome.hits:
            self._populate_results(keep_highlight=0)
            results.display = True
            empty.display = False
            results.focus()
            self._set_status(f"{len(outcome.hits)} result(s) — enter previews, y copies context")
        else:
            results.display = False
            empty.display = True
            empty.update(
                Text("No matches for that question.\n", justify="left")
                + Text(
                    "Nothing above the relevance floor was found — recall never invents. "
                    "Try other words, or capture what you know first (3).",
                    style="dim",
                )
            )
            self._set_status("no results")
        banner = self.query_one("#ask-degraded", Static)
        glyphs = self.app.glyphs
        summary = marker_summary(outcome)
        if summary:
            banner.update(Text(f"{glyphs.get('warn', '!')} {summary}", style=STYLE_WARN))
            banner.add_class("visible")
        else:
            banner.remove_class("visible")
        self._show_detail(None)

    def _on_error(self, error: BackendError) -> None:
        self._running_since = None
        if self._timer is not None:
            self._timer.pause()
        self._set_status("failed")
        self.query_one("#ask-error", ErrorNotice).show_error(error)

    def action_cancel_or_back(self) -> None:
        if self._running_since is not None:
            self.supersede("ask")
            self._running_since = None
            if self._timer is not None:
                self._timer.pause()
            self._set_status("cancelled — the knowledge base was not modified")
            return
        self.app.back()

    # ------------------------------------------------------------------ #
    # Evidence
    # ------------------------------------------------------------------ #
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "ask-results":
            self._preview_hit(event.option_index)

    def action_toggle_preview(self) -> None:
        results = self.query_one("#ask-results", OptionList)
        index = results.highlighted
        self._preview_hit(0 if index is None else index)

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
            self._show_preview(path, cached)
            return
        backend = self.app.backend
        self._set_status("loading source preview")

        def job() -> tuple[str, str]:
            page = backend.read_page(path)
            body = str(page.get("body") or page.get("content") or "")
            return path, body

        def done(payload: tuple[str, str]) -> None:
            page_path, body = payload
            self._preview_cache[page_path] = body
            self._set_status("")
            self._show_preview(page_path, body)

        self.run_backend(job, done, self._on_error, group="preview")

    def _show_preview(self, path: str, body: str) -> None:
        bounded = body[: self.PREVIEW_LIMIT]
        if len(body) > self.PREVIEW_LIMIT:
            bounded += f"\n{self.app.glyphs.get('ellipsis', '...')} (bounded preview)"
        text = Text()
        header = Text(path, style="dim", no_wrap=True, overflow="ellipsis")
        text.append(header)
        text.append("\n\n")
        text.append(bounded)
        if self.has_class("-wide"):
            self._show_detail(text)
        else:
            self.app.push_screen(PreviewModal(path, text))

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
            lines = ["# Exomem recall"]
            lines += [f"- {hit.get('title')}  ({hit.get('path')})" for hit in self._outcome.hits]
            packet = "\n".join(lines)
        self.app.copy_to_clipboard(packet)
        self.app.notify("Context packet copied (needs terminal OSC 52 clipboard support).")

    def action_write_back(self) -> None:
        query = self.query_one("#ask-input", Input).value.strip()
        suggested = query[:80] if query else "New insight"

        def on_close(result: dict | None) -> None:
            if not result:
                return
            backend = self.app.backend
            self._set_status("saving insight")

            def done(saved: dict) -> None:
                path = saved.get("path") or saved.get("note") or "saved"
                self._set_status("")
                self.app.notify(f"Saved: {path}")

            def failed(error: BackendError) -> None:
                if isinstance(error, RelationReviewRequired):
                    self._confirm_unlinked(error.draft, done)
                    return
                self._set_status("")
                self._on_error(error)

            self.run_backend(
                lambda: backend.remember_note(result["content"], result["title"]),
                done,
                failed,
                group="writeback",
            )

        self.app.push_screen(WriteBackModal(suggested), on_close)

    def _confirm_unlinked(self, draft: dict, done) -> None:
        """The governed relation review, put to the user honestly."""
        backend = self.app.backend

        def on_close(confirmed: bool | None) -> None:
            if not confirmed:
                self._set_status("not saved — nothing was written")
                return
            self._set_status("saving insight (unlinked)")
            self.run_backend(
                lambda: backend.commit_unlinked_note(draft),
                done,
                self._on_error,
                group="writeback",
            )

        self.app.push_screen(
            ConfirmModal(
                "No typed relation connects this note yet.",
                "Save it unlinked? Your confirmation is recorded as the relation "
                "review; connect it later via `exomem connect` or the Review Studio.",
            ),
            on_close,
        )

    def action_refresh_caches(self) -> None:
        backend = self.app.backend
        self.run_backend(
            lambda: (backend.refresh_caches(), "ok")[1],
            lambda _result: self.app.notify("Caches refreshed."),
            self._on_error,
            group="refresh",
        )
