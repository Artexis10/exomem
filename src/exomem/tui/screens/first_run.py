"""First run as a ledger: one screen that accretes answers you can read back.

Setup is a short conversation rather than a wizard. Each answered question
collapses into a `✓` receipt line at the top; the active question owns the
rows below it. That shape does three jobs a stepped wizard cannot:

* **It is inspectable.** The transcript IS the progress indicator, so there is
  no rail widget claiming a state the screen cannot prove.
* **It is honest about writes.** A line appears only after the thing happened,
  and it names what happened ("created ~/Exomem — 28 files"). `esc` rewinds one
  line, and stops at any line that performed a write — because you cannot
  un-create a folder, and a UI that pretends otherwise is lying.
* **It ends in a demo built from the user's own words.** The closing receipt
  cites the note they just captured, retrieved by the query they just typed.

Every write here goes through the same governed seam the CLI uses; nothing on
this screen has a private path into the vault.
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, OptionList, SelectionList, Static, TextArea
from textual.widgets.selection_list import Selection

from ..backend import AskOutcome, BackendError, VaultState
from ..format import LABEL_FIELD, first_words, fit, truncate_path, wrap
from ..widgets import (
    AppFooter,
    AppHeader,
    BarOptionList,
    BarRow,
    RecoveryPanel,
    continuation,
    receipt,
)
from .base import ExomemScreen

INTRO = (
    "Setup is a short conversation. Every answer becomes a line you can",
    "read back; esc rewinds one line. Nothing is written until a line says so.",
)

WELCOME_OPTIONS = (
    ("create", "Create a fresh vault at {path}", "a new folder, {n} files; nothing outside it is touched"),
    ("existing", "Use an existing vault", "connect to a folder that already holds a Knowledge Base"),
    ("scan", "Scan a folder first", "a read-only report before you decide anything"),
    ("skipall", "Skip for now", "everything stays reachable from Home"),
)

NEXT_OPTIONS = (
    ("home", "Go to Home", ""),
    ("ask", "Ask something else", ""),
    ("hooks", "Install agent hooks", "checkpoints and continuation for your agents — exomem install-hook"),
)

CLOSE = (
    "This screen was the product: capture, then ask, with every claim",
    "pointing at a file you own.",
)

#: How many receipt lines stay visible before the ledger elides its head.
LEDGER_WINDOW = 6


@dataclass
class LedgerLine:
    """One answered step: its receipt, plus whether a write pinned it down."""

    step: str
    lines: list[Text] = field(default_factory=list)
    pinned: bool = False


class PacksChoice(SelectionList):
    """A SelectionList where space toggles and enter means "done choosing".

    The inherited `enter` binding also toggles, which would leave the step with
    no way to say "that's my answer"; overriding it keeps space as the toggle
    and gives enter the meaning the footer promises.
    """

    BINDINGS = [Binding("enter", "confirm_packs", "continue", show=False)]

    class Confirmed(Message):
        """The user is done toggling packs."""

    def action_confirm_packs(self) -> None:
        self.post_message(PacksChoice.Confirmed())


class FirstRunScreen(ExomemScreen):
    SCREEN_TITLE = "First run"

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("escape", "rewind", "rewind", show=False, priority=True),
        Binding("s", "skip", "skip", show=False),
        Binding("ctrl+s", "save_capture", "save and continue", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._ledger: list[LedgerLine] = []
        self._stage = "welcome"
        self._vault_root: Path | None = None
        self._scaffold_files = 0
        self._path_draft = ""
        self._scan_folder: Path | None = None
        self._capture_title = ""

    # ------------------------------------------------------------------ #
    # Composition — every block exists once and is shown per stage.
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        with Vertical(id="body", classes="-flush"):
            yield Static(id="ledger")
            yield Static(id="intro")
            yield Static(id="question")
            yield BarOptionList(id="choices")
            path_input = Input(id="path-input", classes="line-input")
            path_input.display = False
            yield path_input
            yield Static(id="preview")
            packs = PacksChoice(id="packs-choice")
            packs.display = False
            yield packs
            area = TextArea(id="capture-area", classes="compose-area")
            area.display = False
            yield area
            yield Static(id="note")
            ask_input = Input(id="ask-input", classes="line-input")
            ask_input.display = False
            yield ask_input
            yield RecoveryPanel(id="recovery")
        yield AppFooter()

    def on_mount(self) -> None:
        backend = self.app.backend
        self.run_backend(
            backend.scaffold_file_count,
            self._on_scaffold_count,
            lambda _error: self._enter_welcome(),
            group="first-run-meta",
        )

    def _on_scaffold_count(self, count: int) -> None:
        self._scaffold_files = int(count or 0)
        self._enter_welcome()

    # ------------------------------------------------------------------ #
    # Ledger rendering
    # ------------------------------------------------------------------ #
    def _default_path(self) -> Path:
        return Path.home() / "Exomem"

    def _display_path(self, path: Path) -> str:
        try:
            return "~/" + str(path.relative_to(Path.home()))
        except ValueError:
            return str(path)

    def _render_ledger(self) -> None:
        target = self.query_one("#ledger", Static)
        skin = self.app.skin
        flat: list[Text] = []
        visible = self._ledger[-LEDGER_WINDOW:]
        elided = len(self._ledger) - len(visible)
        if elided > 0:
            flat.append(Text(f"  {skin.g('ellipsis')} {elided} earlier lines", style=skin.dim))
        for entry in visible:
            flat.extend(entry.lines)
        if not flat:
            target.update("")
            target.display = False
            return
        block = Text()
        for index, line in enumerate(flat):
            if index:
                block.append("\n")
            block.append("  ")
            block.append_text(line)
        block.append("\n")
        target.update(block)
        target.display = True

    def _add_line(self, step: str, lines: list[Text], *, pinned: bool) -> None:
        self._ledger.append(LedgerLine(step, lines, pinned))
        self._render_ledger()

    # ------------------------------------------------------------------ #
    # Stage plumbing
    # ------------------------------------------------------------------ #
    def _reset_blocks(self) -> None:
        for widget_id in ("intro", "question", "preview", "note"):
            widget = self.query_one(f"#{widget_id}", Static)
            widget.update("")
            widget.display = False
        for widget_id in ("choices", "path-input", "packs-choice", "capture-area", "ask-input"):
            self.query_one(f"#{widget_id}").display = False
        self.query_one("#recovery", RecoveryPanel).hide()

    def _set_static(self, widget_id: str, lines: list[Text], *, gutter: int = 2) -> None:
        widget = self.query_one(f"#{widget_id}", Static)
        block = Text()
        for index, line in enumerate(lines):
            if index:
                block.append("\n")
            block.append(" " * gutter)
            block.append_text(line)
        widget.update(block)
        widget.display = bool(lines)

    def _question(self, step: str, text: str) -> None:
        skin = self.app.skin
        line = Text(no_wrap=True)
        line.append(f"{step:<9}", style=f"bold {skin.accent}")
        line.append(" " + fit(text, self.content_budget() - 10), style=skin.text)
        self._set_static("question", [Text(""), line, Text("")])

    def _prose(self, widget_id: str, text: str, *, gutter: int = 2) -> None:
        skin = self.app.skin
        lines = [
            Text(line, style=skin.dim)
            for line in wrap(text, self.content_budget() - gutter)
        ]
        self._set_static(widget_id, [Text(""), *lines], gutter=gutter)

    # ------------------------------------------------------------------ #
    # Stage: welcome
    # ------------------------------------------------------------------ #
    def _enter_welcome(self) -> None:
        self._stage = "welcome"
        self._reset_blocks()
        skin = self.app.skin
        self._set_static("intro", [Text(line, style=skin.text) for line in INTRO])
        self._question("vault", "Where should your memory live?")
        default = self._display_path(self._default_path())
        rows: list[BarRow] = []
        for action, label, sub in WELCOME_OPTIONS:
            label = label.format(path=default)
            sub = sub.format(n=self._scaffold_files or "a few")
            lines = [(1, Text(label, style=skin.text))]
            if sub:
                lines.append((3, Text(sub, style=skin.dim)))
            rows.append(BarRow(action, lines))
        choices = self.query_one("#choices", BarOptionList)
        choices.display = True
        choices.set_rows(rows)
        choices.focus()
        self.set_footer([("enter", "answer"), ("esc", "rewind"), ("s", "skip")])

    # ------------------------------------------------------------------ #
    # Stage: vault path (create / connect) with the pre-write preview
    # ------------------------------------------------------------------ #
    def _enter_path(self, mode: str) -> None:
        self._stage = f"path-{mode}"
        self._reset_blocks()
        skin = self.app.skin
        self._question("vault", "Where?")
        path_input = self.query_one("#path-input", Input)
        path_input.display = True
        if mode == "create":
            path_input.value = self._path_draft or self._display_path(self._default_path())
            path_input.placeholder = "folder to create"
            self._render_preview(path_input.value)
            self.set_footer(
                [("enter", "create"), ("tab", "complete"), ("esc", "back, typing kept")]
            )
        else:
            path_input.value = self._path_draft
            path_input.placeholder = "folder that already holds a Knowledge Base"
            self._prose("note", "Nothing is written — the folder is opened as it stands.")
            self.set_footer(
                [("enter", "connect"), ("tab", "complete"), ("esc", "back, typing kept")]
            )
        path_input.focus()

    def _render_preview(self, raw: str) -> None:
        skin = self.app.skin
        shown = raw.strip() or self._display_path(self._default_path())
        budget = self.content_budget()
        count = self._scaffold_files or 0
        rows = [
            Text(""),
            Text("Will create", style=skin.secondary),
            self._preview_row(
                f"  {shown}/Knowledge Base/",
                f"the governed layer, {count} files" if count else "the governed layer",
                budget,
            ),
            self._preview_row(f"  {skin.g('tree_mid')} Sources/", "raw captures, immutable", budget),
            self._preview_row(
                f"  {skin.g('tree_end')} Notes/", "compiled conclusions with provenance", budget
            ),
            Text(""),
        ]
        rows += [
            Text(line, style=skin.dim)
            for line in wrap(
                "Plain markdown, Obsidian-compatible. Delete the folder and the vault is "
                "gone — Exomem owns nothing outside it.",
                budget - 2,
            )
        ]
        self._set_static("preview", rows)

    def _preview_row(self, left: str, right: str, budget: int) -> Text:
        skin = self.app.skin
        column = min(38, max(20, budget - len(right) - 2))
        line = Text(no_wrap=True)
        line.append(fit(left, column - 1).ljust(column), style=skin.text)
        line.append(fit(right, budget - column), style=skin.dim)
        return line

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "path-input" and self._stage == "path-create":
            self._path_draft = event.value
            self._render_preview(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        if event.input.id == "path-input":
            if not raw:
                return
            folder = self._resolve(raw)
            if self._stage == "path-create":
                self._create_vault(folder)
            elif self._stage == "path-existing":
                self._connect_vault(folder)
            elif self._stage == "path-scan":
                self._run_scan(folder)
        elif event.input.id == "ask-input" and raw:
            self._run_ask(raw)

    @staticmethod
    def _resolve(raw: str) -> Path:
        folder = Path(raw).expanduser()
        return folder if folder.is_absolute() else Path.cwd() / folder

    # ------------------------------------------------------------------ #
    # Vault writes
    # ------------------------------------------------------------------ #
    def _create_vault(self, folder: Path) -> None:
        backend = self.app.backend
        self._reset_blocks()
        self._question("vault", "Where?")
        self._set_static(
            "note", [Text(""), receipt(self.app.skin, "working", "creating", str(folder))]
        )
        self.set_footer([("esc", "back, typing kept")])

        def job():
            state = backend.adopt_vault_root(folder)
            if state.initialized:
                return ("exists", state, {})
            folder.mkdir(parents=True, exist_ok=True)
            result = backend.init_vault(folder)
            return ("created", backend.adopt_vault_root(folder), result)

        self.run_backend(job, self._on_vault_created, self._on_vault_error, group="first-run-vault")

    def _connect_vault(self, folder: Path) -> None:
        backend = self.app.backend

        def job():
            state = backend.adopt_vault_root(folder)
            if not state.initialized:
                raise BackendError(
                    "NOT_A_VAULT",
                    f"{folder} holds no Knowledge Base yet",
                    "create one here, or scan the folder first",
                )
            return ("connected", state, {})

        self.run_backend(job, self._on_vault_created, self._on_vault_error, group="first-run-vault")

    def _receipt_detail(self, verb: str, path: Path | None, tail: str) -> str:
        """`created …/Exomem — 28 files` — the path yields, the tail never does.

        A receipt is one row, so something has to give when the path is long.
        The tail states what happened and must survive; the path truncates
        from the left, which keeps the folder you actually recognize.
        """
        shown = self._display_path(path) if path is not None else "the vault"
        room = self.content_budget() - LABEL_FIELD - len(verb) - len(tail) - 2
        return f"{verb} {truncate_path(shown, max(8, room))} {tail}".strip()

    def _on_vault_created(self, payload) -> None:
        kind, state, result = payload
        skin = self.app.skin
        self._vault_root = state.root
        if kind == "created":
            count = len(result.get("created") or []) or self._scaffold_files
            detail = self._receipt_detail("created", state.root, f"— {count} files, plain markdown")
        elif kind == "exists":
            detail = self._receipt_detail(
                "connected", state.root, "— it already held a Knowledge Base"
            )
        else:
            detail = self._receipt_detail("connected", state.root, "— opened as it stands")
        self._add_line(
            "vault",
            [receipt(skin, "done", "vault", detail, budget=self.content_budget())],
            pinned=True,
        )
        self.app.record_receipt("done", "vault", detail)
        self.app.on_vault_ready()
        self._enter_packs()

    def _on_vault_error(self, error: BackendError) -> None:
        self._reset_blocks()
        skin = self.app.skin
        if error.code in ("VAULT_EXISTS", "NOT_A_VAULT"):
            state, word = ("warn", "already a vault") if error.code == "VAULT_EXISTS" else (
                "warn",
                "not a vault yet",
            )
            options = [
                ("connect", "Connect to it (recommended)", "open the existing vault as-is"),
                ("scan", "Scan it first", "a read-only report before connecting"),
                ("relocate", "Choose another location", "back to the path, your typing is kept"),
            ]
            if error.code == "NOT_A_VAULT":
                options = [
                    ("create-here", "Create the governed layer here", "adds Knowledge Base/ — your files are untouched"),
                    ("scan", "Scan it first", "a read-only report before writing anything"),
                    ("relocate", "Choose another location", "back to the path, your typing is kept"),
                ]
        else:
            state, word = "fail", "not created"
            options = [
                ("relocate", "Choose another location", "back to the path, your typing is kept"),
                ("retry", "Try again", "re-run with the same path"),
            ]
        self._stage = "vault-error"
        panel = self.query_one("#recovery", RecoveryPanel)
        panel.show(
            state=state,
            word=word,
            what=error.message,
            facts=["Nothing was changed."]
            + ([error.remediation] if error.remediation and state == "fail" else []),
            options=options,
            budget=self.content_budget(),
        )
        self._question("vault", "What next?")
        self.set_footer([("enter", "choose"), ("esc", "back")])
        _ = skin

    # ------------------------------------------------------------------ #
    # Stage: scan (inserted before vault init when asked for)
    # ------------------------------------------------------------------ #
    def _enter_scan(self) -> None:
        self._stage = "path-scan"
        self._reset_blocks()
        self._question("scan", "Which folder should I read?")
        self._prose("note", "Read-only. Nothing is written, moved, or rewritten.")
        path_input = self.query_one("#path-input", Input)
        path_input.display = True
        path_input.value = self._path_draft
        path_input.placeholder = "folder of notes to read"
        path_input.focus()
        self.set_footer([("enter", "scan"), ("esc", "back, typing kept")])

    def _run_scan(self, folder: Path) -> None:
        backend = self.app.backend
        self._scan_folder = folder
        self._set_static(
            "note", [Text(""), receipt(self.app.skin, "working", "reading", str(folder))]
        )
        self.run_backend(
            lambda: backend.adopt_scan(folder),
            self._on_scan,
            self._on_scan_error,
            group="first-run-scan",
        )

    def _on_scan(self, report: dict) -> None:
        skin = self.app.skin
        totals = (report.get("summary") or {}).get("totals") or {}
        governance = report.get("governance") or {}
        folder = self._scan_folder or Path(".")
        detail = (
            f"{totals.get('files', 0)} files · {totals.get('markdown', 0)} markdown · "
            f"{totals.get('dirs', 0)} folders · governed layer: "
            + ("present" if governance.get("kb_present") else "none")
        )
        self._add_line(
            "scan",
            [
                receipt(
                    skin,
                    "done",
                    "scanned",
                    self._receipt_detail("", folder, "— read without writing anything"),
                    budget=self.content_budget(),
                ),
                Text(f"    {fit(detail, self.content_budget() - 4)}", style=skin.dim),
            ],
            pinned=False,
        )
        self._reset_blocks()
        self._stage = "scan-report"
        packs = report.get("pack_suggestions") or []
        suggested = ", ".join(str(pack.get("name") or pack.get("id")) for pack in packs[:3])
        panel = self.query_one("#recovery", RecoveryPanel)
        panel.show(
            state="idle",
            word="decide",
            what="the scan changed nothing — what should happen next?",
            facts=[f"Likely packs: {suggested}"] if suggested else None,
            options=[
                ("init-here", "Initialize the governed layer here", "adds Knowledge Base/ alongside your notes — originals untouched"),
                ("elsewhere", "Create a fresh vault somewhere else", "leaves this folder exactly as it is"),
                ("scan-other", "Scan another folder", "still read-only"),
            ],
            budget=self.content_budget(),
        )
        self.set_footer([("enter", "choose"), ("esc", "rewind")])

    def _on_scan_error(self, error: BackendError) -> None:
        self._reset_blocks()
        self._stage = "scan-error"
        self.query_one("#recovery", RecoveryPanel).show(
            state="fail",
            word="not scanned",
            what=error.message,
            facts=["Nothing was changed.", error.remediation or "check the path and try again"],
            options=[
                ("scan-other", "Choose another folder", "your typing is kept"),
                ("relocate", "Create a vault instead", "back to the vault question"),
            ],
            budget=self.content_budget(),
        )
        self.set_footer([("enter", "choose"), ("esc", "rewind")])

    # ------------------------------------------------------------------ #
    # Stage: packs
    # ------------------------------------------------------------------ #
    def _enter_packs(self) -> None:
        self._stage = "packs"
        self._reset_blocks()
        self._question("packs", "Which domains should guide interpretation?")
        self._prose("note", "Packs guide interpretation and retrieval defaults. They never impose folders or an ontology.")
        self.set_footer(
            [("space", "toggle"), ("enter", "continue"), ("s", "skip"), ("esc", "rewind")]
        )
        backend = self.app.backend
        self.run_backend(backend.packs_state, self._on_packs_state, self._on_packs_error, group="first-run-packs")

    def _on_packs_state(self, state: dict) -> None:
        skin = self.app.skin
        selection = self.query_one("#packs-choice", PacksChoice)
        selection.clear_options()
        selected = set(state.get("selected") or [])
        for pack in state.get("catalog") or []:
            pack_id = str(pack.get("id") or "")
            prompt = Text(no_wrap=True)
            prompt.append(str(pack.get("name") or pack_id), style=skin.text)
            description = str(pack.get("description") or "")
            if description:
                prompt.append(f" — {description}", style=skin.dim)
            selection.add_option(Selection(prompt, pack_id, pack_id in selected))
        selection.display = True
        selection.focus()

    def _on_packs_error(self, error: BackendError) -> None:
        self._skip_step("packs", f"unavailable — {error.message}")

    def on_packs_choice_confirmed(self, event) -> None:
        event.stop()
        if self._stage != "packs":
            return
        chosen = [str(value) for value in self.query_one("#packs-choice", PacksChoice).selected]
        if not chosen:
            self._skip_step("packs", "none chosen — defaults stay in effect")
            return
        backend = self.app.backend
        self.run_backend(
            lambda: backend.apply_packs(chosen),
            lambda _result: self._packs_saved(chosen),
            self._on_packs_error,
            group="first-run-packs-apply",
        )

    def _packs_saved(self, chosen: list[str]) -> None:
        detail = ", ".join(chosen)
        self._add_line(
            "packs",
            [receipt(self.app.skin, "done", "packs", detail, budget=self.content_budget())],
            pinned=True,
        )
        self.app.record_receipt("done", "packs", detail)
        self._enter_capture()

    # ------------------------------------------------------------------ #
    # Stage: capture
    # ------------------------------------------------------------------ #
    def _enter_capture(self) -> None:
        self._stage = "capture"
        self._reset_blocks()
        self._question("capture", "One real thought — anything you'd want back in a month.")
        area = self.query_one("#capture-area", TextArea)
        area.display = True
        area.focus()
        self._prose("note", "Saved as an immutable Source — your words are never rewritten.")
        self.set_footer(
            [("^s", "save and continue"), ("esc", "rewind"), ("s", "skip")]
        )

    def action_save_capture(self) -> None:
        if self._stage != "capture":
            return
        area = self.query_one("#capture-area", TextArea)
        content = area.text.strip()
        if not content:
            self._skip_step("capture", "nothing written yet — capture any time from Home (3)")
            return
        title = first_words(content, 70) or "Captured thought"
        self._capture_title = title
        backend = self.app.backend
        self._set_static(
            "note", [Text(""), receipt(self.app.skin, "working", "saving", "writing an immutable Source")]
        )
        self.run_backend(
            lambda: backend.capture_thought(content, title),
            lambda result: self._capture_saved(content, result),
            self._on_capture_error,
            group="first-run-capture",
        )

    def _capture_saved(self, content: str, result: dict) -> None:
        skin = self.app.skin
        path = str(result.get("path") or result.get("source_path") or "")
        folder = "/".join(path.split("/")[:-1]) if path else ""
        if not folder:
            today = _dt.date.today()
            folder = f"Sources/{today.year}/{today.month:02d}"
        room = self.content_budget() - LABEL_FIELD
        opening = f'"{first_words(content, min(34, room // 2))}"'
        detail = (
            f"{opening} {skin.g('arrow')} "
            f"{truncate_path(folder, max(12, room - len(opening) - 3))}"
        )
        self._add_line(
            "capture",
            [receipt(skin, "done", "capture", detail, budget=self.content_budget())],
            pinned=True,
        )
        self.app.record_receipt("done", "captured", f'"{first_words(content, 34)}"')
        self._enter_ask()

    def _on_capture_error(self, error: BackendError) -> None:
        self._reset_blocks()
        self._stage = "capture-error"
        self.query_one("#recovery", RecoveryPanel).show(
            state="fail",
            word="not saved",
            what=error.message,
            facts=["Your text is kept. Nothing was partially written."],
            options=[
                ("retry-capture", "Try again", "the same words, unchanged"),
                ("skip-capture", "Skip this step", "capture any time from Home (3)"),
            ],
            budget=self.content_budget(),
        )
        self.set_footer([("enter", "choose"), ("esc", "rewind")])

    # ------------------------------------------------------------------ #
    # Stage: ask
    # ------------------------------------------------------------------ #
    def _enter_ask(self) -> None:
        self._stage = "ask"
        self._reset_blocks()
        self._question("ask", "Ask for what you just captured. Recall is measured — never invented.")
        ask_input = self.query_one("#ask-input", Input)
        ask_input.display = True
        ask_input.value = ""
        ask_input.placeholder = self._capture_title[:60] or "what did you just write about?"
        ask_input.focus()
        self.set_footer([("enter", "ask"), ("esc", "rewind"), ("s", "skip")])

    def _run_ask(self, query: str) -> None:
        backend = self.app.backend
        self._set_static(
            "note",
            [
                Text(""),
                receipt(self.app.skin, "working", "working", "measuring recall — lexical + semantic lanes"),
            ],
        )

        def job() -> tuple[AskOutcome, float]:
            started = time.perf_counter()
            return backend.ask(query, limit=5), started

        self.run_backend(
            job, lambda payload: self._ask_done(query, payload), self._on_ask_error, group="first-run-ask"
        )

    def _ask_done(self, query: str, payload: tuple[AskOutcome, float]) -> None:
        outcome, started = payload
        elapsed_ms = self.app.elapsed_ms(started)
        skin = self.app.skin
        budget = self.content_budget()
        lines = [receipt(skin, "done", "ask", query, budget=budget)]
        if outcome.hits:
            top = outcome.hits[0]
            title = str(top.get("title") or top.get("path") or "a page")
            kind = str(top.get("type") or "page")
            summary = f"{title} {skin.g('bullet')} {kind} {skin.g('bullet')} {elapsed_ms:.0f} ms"
            hit_line = Text(no_wrap=True)
            hit_line.append(f"    {skin.g('pointer')} retrieved  ", style=skin.accent)
            hit_line.append(fit(summary, budget - 18), style=skin.text)
            lines.append(hit_line)
        else:
            miss = Text(no_wrap=True)
            miss.append(f"    {skin.g('pointer')} retrieved  ", style=skin.accent)
            miss.append(
                fit(f"0 results · {elapsed_ms:.0f} ms — a miss means \"not found with these words\"", budget - 18),
                style=skin.dim,
            )
            lines.append(miss)
        self._add_line("ask", lines, pinned=False)
        self.app.record_receipt("retrieved", "asked", query)
        if outcome.warming:
            components = ", ".join(str(item) for item in (outcome.warming.get("components") or []))
            self._add_line(
                "ask-warming",
                [
                    receipt(
                        skin,
                        "warn",
                        "partial",
                        f"still warming: {components or 'search lanes'} — lexical results above",
                        budget=budget,
                    ),
                    continuation(skin, "full recall returns automatically; nothing to do"),
                ],
                pinned=False,
            )
        self._enter_done()

    def _on_ask_error(self, error: BackendError) -> None:
        self._reset_blocks()
        self._stage = "ask-error"
        self.query_one("#recovery", RecoveryPanel).show(
            state="fail",
            word="recall failed",
            what=error.message,
            facts=["Nothing was changed. Your capture is already saved."],
            options=[
                ("retry-ask", "Try the question again", ""),
                ("finish", "Finish setup", "Ask is always available from Home (2)"),
            ],
            budget=self.content_budget(),
        )
        self.set_footer([("enter", "choose"), ("esc", "rewind")])

    # ------------------------------------------------------------------ #
    # Stage: done
    # ------------------------------------------------------------------ #
    def _enter_done(self) -> None:
        self._stage = "done"
        self._reset_blocks()
        skin = self.app.skin
        self._set_static("intro", [Text(""), *[Text(line, style=skin.text) for line in CLOSE], Text("")])
        rows: list[BarRow] = []
        for action, label, sub in NEXT_OPTIONS:
            lines = [(1, Text(label, style=skin.text))]
            if sub:
                lines.append((3, Text(sub, style=skin.dim)))
            rows.append(BarRow(action, lines))
        choices = self.query_one("#choices", BarOptionList)
        choices.display = True
        choices.set_rows(rows)
        choices.focus()
        self.set_footer([("enter", "choose")])

    # ------------------------------------------------------------------ #
    # Choices and recoveries
    # ------------------------------------------------------------------ #
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "choices":
            return
        event.stop()
        self._dispatch(str(event.option_id or ""))

    def on_recovery_panel_chosen(self, event: RecoveryPanel.Chosen) -> None:
        event.stop()
        self._dispatch(event.action)

    def _dispatch(self, action: str) -> None:
        if action == "create":
            self._enter_path("create")
        elif action == "existing":
            self._enter_path("existing")
        elif action in ("scan", "scan-other"):
            self._enter_scan()
        elif action == "skipall":
            self._leave()
        elif action == "connect":
            self._connect_vault(self._resolve(self._path_draft or str(self._default_path())))
        elif action in ("relocate", "elsewhere"):
            self._enter_path("create")
        elif action == "retry":
            self._create_vault(self._resolve(self._path_draft or str(self._default_path())))
        elif action in ("create-here", "init-here"):
            folder = self._scan_folder or self._resolve(self._path_draft or str(self._default_path()))
            self._create_vault(folder)
        elif action == "retry-capture":
            self._enter_capture()
        elif action == "skip-capture":
            self._skip_step("capture", "skipped — capture any time from Home (3)")
        elif action == "retry-ask":
            self._enter_ask()
        elif action == "finish":
            self._enter_done()
        elif action == "home":
            self._leave()
        elif action == "ask":
            self._leave(goto="ask")
        elif action == "hooks":
            self.app.copy_to_clipboard("exomem install-hook")
            self.app.notify(
                "Run `exomem install-hook` in a terminal — the command is on your clipboard."
            )

    def _leave(self, goto: str = "home") -> None:
        self.app.finish_first_run(goto)

    # ------------------------------------------------------------------ #
    # Rewind and skip
    # ------------------------------------------------------------------ #
    def action_rewind(self) -> None:
        # Inside a step, esc steps back within it and keeps what was typed.
        if self._stage in ("path-create", "path-existing", "path-scan"):
            self._path_draft = self.query_one("#path-input", Input).value
            self._enter_welcome()
            return
        if self._stage in ("vault-error", "scan-error"):
            self._enter_path("create")
            return
        if not self._ledger:
            self.app.back()
            return
        last = self._ledger[-1]
        if last.pinned:
            self._pinned_notice(last.step)
            return
        self._ledger.pop()
        self._render_ledger()
        self._reenter(last.step)

    def _pinned_notice(self, step: str) -> None:
        skin = self.app.skin
        self._set_static(
            "note",
            [
                Text(""),
                Text(
                    fit(
                        f"{skin.g('bullet')} that {step} line records a write — it cannot be rewound",
                        self.content_budget() - 2,
                    ),
                    style=skin.dim,
                ),
            ],
        )

    def _reenter(self, step: str) -> None:
        if step in ("scan", "scan-report"):
            self._enter_scan()
        elif step == "packs":
            self._enter_packs()
        elif step == "capture":
            self._enter_capture()
        elif step in ("ask", "ask-warming"):
            self._enter_ask()
        else:
            self._enter_welcome()

    def action_skip(self) -> None:
        if self._stage == "packs":
            self._skip_step("packs", "skipped — choose them any time from Home (6)")
        elif self._stage == "capture":
            self._skip_step("capture", "skipped — capture any time from Home (3)")
        elif self._stage == "ask":
            self._skip_step("ask", "skipped — Ask is always on Home (2)")

    def _skip_step(self, step: str, detail: str) -> None:
        self._add_line(
            step,
            [receipt(self.app.skin, "idle", step, detail, budget=self.content_budget())],
            pinned=False,
        )
        if step == "packs":
            self._enter_capture()
        elif step == "capture":
            self._enter_ask()
        else:
            self._enter_done()

    # ------------------------------------------------------------------ #
    def on_resize(self, _event) -> None:
        self._render_ledger()

    def refresh_data(self) -> None:
        """Nothing to re-read: this screen's state is the conversation itself."""

    def vault_state(self) -> VaultState | None:
        return None if self._vault_root is None else VaultState(self._vault_root, True)
