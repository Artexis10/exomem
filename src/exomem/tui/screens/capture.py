"""Capture: save a thought before it evaporates.

Two kinds, in friendly language over the exact governed paths: a *thought*
keeps your raw words as an immutable source (`capture_source`); an *insight*
records a compiled conclusion (`remember`). No folder choice, no ontology —
the title is derived from the first line and stays editable.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Input, RadioButton, RadioSet, Static, TextArea

from ..backend import BackendError, RelationReviewRequired
from ..theme import STYLE_OK
from ..widgets import AppHeader, ConfirmModal, ErrorNotice
from .base import ExomemScreen


def derive_title(content: str, limit: int = 80) -> str:
    """First non-empty line, trimmed to a title length."""
    for line in content.splitlines():
        candidate = line.strip().lstrip("#").strip()
        if candidate:
            if len(candidate) > limit:
                return candidate[: limit - 1].rstrip() + "…"
            return candidate
    return ""


class CaptureScreen(ExomemScreen):
    SCREEN_TITLE = "Capture"

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("escape", "guarded_back", "back", show=False, priority=True),
        Binding("ctrl+s", "save", "save"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._title_touched = False

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        yield Static(
            Text(
                "Your raw words are preserved as-is; nothing is rewritten.",
                style="dim",
            ),
            classes="pane",
        )
        yield TextArea(id="capture-content")
        yield Input(placeholder="title (derived from the first line — edit freely)", id="capture-title", classes="line-input")
        with RadioSet(id="capture-kind"):
            yield RadioButton("Thought — keep the raw words (immutable source)", value=True, id="kind-thought")
            yield RadioButton("Insight — a compiled conclusion (governed note)", id="kind-insight")
        yield Static(id="capture-result", classes="pane")
        error = ErrorNotice(id="capture-error")
        error.display = False
        yield error
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#capture-content", TextArea).focus()

    # Auto-title tracks the first line until the user edits the title field.
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "capture-content" or self._title_touched:
            return
        title_input = self.query_one("#capture-title", Input)
        title_input.value = derive_title(event.text_area.text)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "capture-title":
            return
        derived = derive_title(self.query_one("#capture-content", TextArea).text)
        if event.value != derived:
            self._title_touched = True

    def _kind(self) -> str:
        chosen = self.query_one("#capture-kind", RadioSet).pressed_button
        return "insight" if chosen is not None and chosen.id == "kind-insight" else "thought"

    def action_save(self) -> None:
        content = self.query_one("#capture-content", TextArea).text.strip()
        title = self.query_one("#capture-title", Input).value.strip()
        if not content:
            self.app.notify("Nothing to capture yet.", severity="warning")
            return
        if not title:
            title = derive_title(content) or "Untitled capture"
        kind = self._kind()
        backend = self.app.backend
        self.query_one("#capture-error", ErrorNotice).show_error(None)
        self.query_one("#capture-result", Static).update(Text("saving…", style="dim"))

        def job() -> dict:
            if kind == "insight":
                return backend.remember_note(content, title)
            return backend.capture_thought(content, title)

        def done(result: dict) -> None:
            path = str(result.get("path") or result.get("source_path") or "saved")
            confirmation = Text()
            confirmation.append(f"{self.app.glyphs.get('ok', '*')} ", style=STYLE_OK)
            confirmation.append("Saved  ", style="bold")
            confirmation.append(path, style="dim")
            self.query_one("#capture-result", Static).update(confirmation)
            self.query_one("#capture-content", TextArea).text = ""
            self.query_one("#capture-title", Input).value = ""
            self._title_touched = False
            self.query_one("#capture-content", TextArea).focus()

        def failed(error: BackendError) -> None:
            if isinstance(error, RelationReviewRequired):
                self._confirm_unlinked(error.draft, done)
                return
            # The typed content stays exactly where it is — only the status
            # changes, so nothing is lost on failure.
            self.query_one("#capture-result", Static).update("")
            self.query_one("#capture-error", ErrorNotice).show_error(error)

        self.run_backend(job, done, failed, group="capture-save")

    def _confirm_unlinked(self, draft: dict, done) -> None:
        backend = self.app.backend

        def on_close(confirmed: bool | None) -> None:
            if not confirmed:
                self.query_one("#capture-result", Static).update(
                    Text("not saved — your words are still here", style="dim")
                )
                return
            self.run_backend(
                lambda: backend.commit_unlinked_note(draft),
                done,
                lambda error: self.query_one("#capture-error", ErrorNotice).show_error(error),
                group="capture-save",
            )

        self.app.push_screen(
            ConfirmModal(
                "No typed relation connects this note yet.",
                "Save it unlinked? Your confirmation is recorded as the relation "
                "review; connect it later via `exomem connect` or the Review Studio.",
            ),
            on_close,
        )

    def action_guarded_back(self) -> None:
        content = self.query_one("#capture-content", TextArea).text.strip()
        if not content:
            self.app.back()
            return

        def on_close(confirmed: bool | None) -> None:
            if confirmed:
                self.query_one("#capture-content", TextArea).text = ""
                self.query_one("#capture-title", Input).value = ""
                self._title_touched = False
                self.app.back()

        self.app.push_screen(
            ConfirmModal(
                "Discard this unsaved capture?",
                "It has not been written anywhere yet. ctrl+s saves it instead.",
            ),
            on_close,
        )
