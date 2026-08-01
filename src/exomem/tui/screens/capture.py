"""Capture: save a thought before it evaporates.

Two kinds in plain language over the exact governed paths. A *thought* keeps
your raw words as an immutable Source; an *insight* records a compiled
conclusion with provenance. No folder choice and no ontology — the title comes
from the first line and stays editable.

Saving an insight with no `[[link]]` raises the real governed question rather
than hiding it: the vault requires a relation disposition, so the modal asks,
and confirming *is* the recorded review. Nothing is ever committed on the
user's behalf, because a fabricated review is worse than a refused write.
"""

from __future__ import annotations

import datetime as _dt
import re

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static, TextArea

from ..backend import BackendError, RelationReviewRequired
from ..format import fit, truncate_path
from ..theme import Skin
from ..widgets import (
    AppFooter,
    AppHeader,
    BarOptionList,
    BarRow,
    ConfirmModal,
    RecoveryPanel,
    receipt,
)
from .base import ExomemScreen

PROMISE = "Your raw words are preserved as-is; nothing is rewritten."

KINDS = (
    ("thought", "thought", "the raw words, kept immutable"),
    ("insight", "insight", "a compiled conclusion with provenance"),
)

OBSERVATION = re.compile(r"(?m)^-\s*\[([^\]]+)\]\s*(.+)$")


def derive_title(content: str, limit: int = 80) -> str:
    """First non-empty line, trimmed to a title length."""
    for line in content.splitlines():
        candidate = line.strip().lstrip("#").strip()
        if candidate:
            if len(candidate) > limit:
                return candidate[: limit - 1].rstrip() + "…"
            return candidate
    return ""


def first_observation(content: str) -> tuple[str, str] | None:
    """`- [finding] text` → ("finding", "text") — the note's semantic unit."""
    match = OBSERVATION.search(content)
    if match is None:
        return None
    return match.group(1).strip(), match.group(2).strip()


class CaptureScreen(ExomemScreen):
    SCREEN_TITLE = "Capture"

    FOOTER_KEYS = (
        ("^s", "save"),
        ("tab", "kind"),
        ("e", "edit title"),
        ("esc", "back, typing kept"),
    )

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("escape", "guarded_back", "back", show=False, priority=True),
        Binding("ctrl+s", "save", "save", priority=True),
        Binding("tab", "cycle_kind", "kind", show=False, priority=True),
        Binding("shift+tab", "focus_title", "edit title", show=False, priority=True),
        Binding("e", "edit_title", "edit title", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._kind = "thought"
        self._title_touched = False
        self._saved = False

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        with Vertical(id="body", classes="-flush"):
            yield Static(id="capture-promise")
            yield TextArea(id="capture-content", classes="compose-area")
            yield Static(id="capture-title-line")
            title = Input(id="capture-title", classes="line-input")
            title.display = False
            yield title
            yield Static(id="capture-kind")
            yield Static(id="capture-receipt")
            yield RecoveryPanel(id="capture-recovery")
            yield BarOptionList(id="capture-next")
        yield AppFooter()

    def on_mount(self) -> None:
        skin = self.app.skin
        self.query_one("#capture-promise", Static).update(Text(f"  {PROMISE}", style=skin.dim))
        self.query_one("#capture-next", BarOptionList).display = False
        self.query_one("#capture-receipt", Static).display = False
        self._render_title()
        self._render_kind()
        self.query_one("#capture-content", TextArea).focus()

    # `e` must type an "e" while the text area has focus; everywhere else it
    # is the shortcut the footer advertises.
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "edit_title":
            content = self.query_one("#capture-content", TextArea)
            title = self.query_one("#capture-title", Input)
            if self.focused is content or self.focused is title:
                return False
        return True

    # ------------------------------------------------------------------ #
    # Compose surface
    # ------------------------------------------------------------------ #
    HINT = "— derived from the first line; e edits"

    def _render_title(self) -> None:
        skin = self.app.skin
        budget = self.content_budget()
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append("  title  ", style=skin.secondary)
        room = budget - 9 - len(self.HINT) - 1
        line.append(fit(self._title() or "(from the first line)", max(12, room)), style=skin.text)
        if room >= 12:
            line.append(f" {self.HINT}", style=skin.dim)
        self.query_one("#capture-title-line", Static).update(line)

    def _render_kind(self) -> None:
        skin = self.app.skin
        block = Text()
        for index, (key, label, description) in enumerate(KINDS):
            if index:
                block.append("\n")
            chosen = key == self._kind
            block.append("  (")
            block.append(skin.g("ok") if chosen else skin.g("idle"), style=skin.accent if chosen else skin.dim)
            block.append(") ")
            block.append(label, style=skin.text if chosen else skin.dim)
            block.append(f" — {description}", style=skin.dim)
        self.query_one("#capture-kind", Static).update(block)

    def _title(self) -> str:
        override = self.query_one("#capture-title", Input).value.strip()
        if self._title_touched and override:
            return override
        return derive_title(self.query_one("#capture-content", TextArea).text)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "capture-content":
            return
        if not self._title_touched:
            self._render_title()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "capture-title":
            self._title_touched = bool(event.value.strip())
            self._render_title()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "capture-title":
            event.input.display = False
            self.query_one("#capture-content", TextArea).focus()

    def action_cycle_kind(self) -> None:
        keys = [key for key, _label, _description in KINDS]
        self._kind = keys[(keys.index(self._kind) + 1) % len(keys)]
        self._render_kind()

    def action_edit_title(self) -> None:
        self.action_focus_title()

    def action_focus_title(self) -> None:
        title = self.query_one("#capture-title", Input)
        if not title.value:
            title.value = self._title()
        title.display = True
        title.focus()

    # ------------------------------------------------------------------ #
    # Save
    # ------------------------------------------------------------------ #
    def action_save(self) -> None:
        content = self.query_one("#capture-content", TextArea).text.strip()
        if not content:
            self.app.notify("Nothing to capture yet.", severity="warning")
            return
        title = self._title() or "Untitled capture"
        kind = self._kind
        backend = self.app.backend
        self.query_one("#capture-recovery", RecoveryPanel).hide()
        self._set_receipt([receipt(self.app.skin, "working", "saving", f"writing a governed {kind}")])

        def job() -> dict:
            if kind == "insight":
                return backend.remember_note(content, title)
            return backend.capture_thought(content, title)

        def done(result: dict) -> None:
            self._on_saved(kind, content, result, reviewed_unlinked=False)

        def failed(error: BackendError) -> None:
            if isinstance(error, RelationReviewRequired):
                self._confirm_unlinked(error.draft, kind, content)
                return
            self._on_save_error(error)

        self.run_backend(job, done, failed, group="capture-save")

    def _confirm_unlinked(self, draft: dict, kind: str, content: str) -> None:
        backend = self.app.backend

        def on_close(choice: str | None) -> None:
            if choice != "confirm":
                self._set_receipt(
                    [Text("not saved — your words are still here", style=self.app.skin.dim)]
                )
                self.query_one("#capture-content", TextArea).focus()
                return
            self.run_backend(
                lambda: backend.commit_unlinked_note(draft),
                lambda result: self._on_saved(kind, content, result, reviewed_unlinked=True),
                self._on_save_error,
                group="capture-save",
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

    def _on_saved(self, kind: str, content: str, result: dict, *, reviewed_unlinked: bool) -> None:
        skin = self.app.skin
        budget = self.content_budget()
        path = str(result.get("path") or result.get("source_path") or "saved")
        lines = [receipt(skin, "done", "saved", f"{kind} {skin.g('arrow')} {truncate_path(path, budget - 20)}", budget=budget)]
        observation = first_observation(content)
        if observation is not None:
            category, unit = observation
            detail = Text(no_wrap=True)
            detail.append("     observation  ", style=skin.secondary)
            detail.append(fit(f"[{category}] {unit}", budget - 18), style=skin.text)
            lines.append(detail)
        if reviewed_unlinked:
            stamp = _dt.datetime.now().strftime("%H:%M")
            review = Text(no_wrap=True)
            review.append("     review       ", style=skin.secondary)
            review.append(f"unlinked — confirmed by you {skin.g('bullet')} {stamp}", style=skin.text)
            lines.append(review)
        self._set_receipt(lines)
        self._saved = True
        self.app.record_receipt("done", "captured", f'"{fit(derive_title(content), 34)}"')

        self.query_one("#capture-content").display = False
        self.query_one("#capture-title-line").display = False
        self.query_one("#capture-kind").display = False
        self.query_one("#capture-title").display = False
        self.query_one("#capture-promise").display = False
        options = self.query_one("#capture-next", BarOptionList)
        options.display = True
        options.set_rows(
            [
                BarRow("ask", [(1, Text("Ask for it — see it cited", style=skin.text))]),
                BarRow("again", [(1, Text("Capture another", style=skin.text))]),
                BarRow("home", [(1, Text("Go to Home", style=skin.text))]),
            ]
        )
        options.focus()
        self.set_footer([("enter", "choose")])

    def _on_save_error(self, error: BackendError) -> None:
        skin = self.app.skin
        self._set_receipt(None)
        self.query_one("#capture-recovery", RecoveryPanel).show(
            state="fail",
            word="not saved",
            what=error.message,
            facts=["Your text is kept below. Nothing was partially written."]
            + ([error.remediation] if error.remediation else []),
            options=[
                ("retry", "Retry", "the same words, unchanged"),
                ("copy", "Copy the text to the clipboard", "so it survives whatever happens next"),
                ("status", "Open Status", "doctor checks vault permissions — 7"),
            ],
            budget=self.content_budget(),
        )
        _ = skin
        self.set_footer([("enter", "choose"), ("esc", "keep editing")])

    def _set_receipt(self, lines: list[Text] | None) -> None:
        widget = self.query_one("#capture-receipt", Static)
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
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "capture-next":
            return
        event.stop()
        if event.option_id == "ask":
            self.app.ask_for(self._title())
        elif event.option_id == "again":
            self._reset()
        else:
            self.app.goto("home")

    def on_recovery_panel_chosen(self, event: RecoveryPanel.Chosen) -> None:
        event.stop()
        if event.action == "retry":
            self.query_one("#capture-recovery", RecoveryPanel).hide()
            self.action_save()
        elif event.action == "copy":
            self.app.copy_to_clipboard(self.query_one("#capture-content", TextArea).text)
            self.app.notify("Your text was copied (needs terminal OSC 52 clipboard support).")
        elif event.action == "status":
            self.app.goto("status")

    def _reset(self) -> None:
        self._saved = False
        self._title_touched = False
        area = self.query_one("#capture-content", TextArea)
        area.text = ""
        area.display = True
        self.query_one("#capture-title", Input).value = ""
        for widget_id in ("capture-promise", "capture-title-line", "capture-kind"):
            self.query_one(f"#{widget_id}").display = True
        self.query_one("#capture-next", BarOptionList).display = False
        self._set_receipt(None)
        self._render_title()
        self._render_kind()
        area.focus()
        self.set_footer(list(self.FOOTER_KEYS))

    def on_screen_resume(self) -> None:
        if self._saved:
            self._reset()

    def action_guarded_back(self) -> None:
        content = self.query_one("#capture-content", TextArea).text.strip()
        if not content or self._saved:
            self.app.back()
            return

        def on_close(choice: str | None) -> None:
            if choice == "confirm":
                self.query_one("#capture-content", TextArea).text = ""
                self._title_touched = False
                self.app.back()

        self.app.push_screen(
            ConfirmModal(
                "Discard this capture?",
                "It has not been written anywhere yet.",
                "Discard it — the words are gone",
                "Keep editing — ^s saves instead",
                "enter choose · esc back — nothing saved yet",
            ),
            on_close,
        )

    def refresh_data(self) -> None:
        """Nothing to re-read — this screen holds only what you have typed."""

    def on_resize(self, _event) -> None:
        self._render_title()
