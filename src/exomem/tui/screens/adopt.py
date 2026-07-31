"""Adopt: scan an existing folder of notes, safely.

Scan-only is the default and works before any Knowledge Base exists — it
modifies nothing. Write modes are separate, explicit steps behind a
confirmation that names the mode and destination, and originals are never
rewritten (adoption is a governed overlay, not an in-place migration).
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Input, Static

from ..backend import BackendError
from ..theme import STYLE_OK, STYLE_WARN
from ..widgets import AppHeader, ConfirmModal, EmptyState, ErrorNotice
from .base import ExomemScreen


def render_scan_report(report: dict, glyphs: dict[str, str]) -> Text:
    totals = (report.get("summary") or {}).get("totals") or {}
    governance = report.get("governance") or {}
    text = Text()
    text.append(f"{glyphs.get('ok', '*')} ", style=STYLE_OK)
    text.append("Scanned without writing anything.\n", style="bold")
    text.append(
        f"  {totals.get('files', 0)} files · {totals.get('markdown', 0)} markdown · "
        f"{totals.get('dirs', 0)} folders\n",
    )
    if governance.get("kb_present"):
        text.append("  Governed layer: present\n", style="dim")
    else:
        text.append("  Governed layer: not initialized yet\n", style="dim")
    text.append("  Originals stay untouched; adoption copies into the governed layer.\n", style="dim")

    packs = report.get("pack_suggestions") or []
    if packs:
        text.append("\nLikely packs\n", style="bold")
        for pack in packs[:6]:
            name = pack.get("name") or pack.get("id") or "unknown"
            signals = ", ".join(pack.get("matched_signals") or [])
            text.append(f"  {name}")
            if signals:
                text.append(f"  ({signals})", style="dim")
            text.append("\n")

    actions = report.get("next_actions") or []
    if actions:
        text.append("\nSafe next steps\n", style="bold")
        for action in actions:
            status = str(action.get("status") or "")
            marker = glyphs.get("ok", "*") if status in ("available", "ready") else glyphs.get("idle", "o")
            style = "" if status in ("available", "ready") else "dim"
            text.append(f"  {marker} {action.get('action')}", style=style)
            description = action.get("description")
            if description:
                text.append(f" — {description}", style="dim")
            if status and status not in ("available", "ready"):
                text.append(f"  [{status}]", style=STYLE_WARN)
            text.append("\n")
    return text


class AdoptScreen(ExomemScreen):
    SCREEN_TITLE = "Adopt"

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("m", "write_mode('save-manifest')", "save manifest"),
        Binding("c", "write_mode('copy-as-sources')", "copy as sources"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._scanned: Path | None = None
        self._report: dict | None = None

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        yield Input(
            placeholder="folder to scan (nothing is modified by a scan)", id="adopt-path"
        )
        yield Static(id="adopt-status", classes="pane")
        error = ErrorNotice(id="adopt-error")
        error.display = False
        yield error
        with VerticalScroll(id="adopt-report-pane"):
            yield Static(id="adopt-report", classes="pane")
            yield EmptyState(
                "Point at any folder of notes to see what adoption would find.",
                "Scan first, decide later — write modes are separate, confirmed steps.",
                id="adopt-empty",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#adopt-path", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "adopt-path":
            return
        raw = event.value.strip()
        if not raw:
            return
        folder = Path(raw).expanduser()
        if not folder.is_dir():
            self.query_one("#adopt-error", ErrorNotice).show_error(
                BackendError("NOT_A_FOLDER", f"{folder} is not a directory")
            )
            return
        self._scan(folder)

    def _scan(self, folder: Path) -> None:
        backend = self.app.backend
        self.query_one("#adopt-error", ErrorNotice).show_error(None)
        self.query_one("#adopt-status", Static).update(
            Text(f"scanning {folder} — read-only…", style="dim")
        )

        def done(report: dict) -> None:
            self._scanned = folder
            self._report = report
            self.query_one("#adopt-status", Static).update(Text(str(folder), style="dim"))
            self.query_one("#adopt-report", Static).update(
                render_scan_report(report, self.app.glyphs)
            )
            self.query_one("#adopt-empty", EmptyState).display = False
            # Move focus off the path input so the m/c action keys reach the
            # screen bindings instead of being typed into the field.
            self.query_one("#adopt-report-pane").focus()

        def failed(error: BackendError) -> None:
            self.query_one("#adopt-status", Static).update("")
            self.query_one("#adopt-error", ErrorNotice).show_error(error)

        self.run_backend(lambda: backend.adopt_scan(folder), done, failed, group="adopt-scan")

    def action_write_mode(self, mode: str) -> None:
        if self._scanned is None or self._report is None:
            self.app.notify("Scan a folder first.", severity="warning")
            return
        folder = self._scanned
        backend = self.app.backend

        def on_close(confirmed: bool | None) -> None:
            if not confirmed:
                return
            self.query_one("#adopt-status", Static).update(
                Text(f"running {mode}…", style="dim")
            )

            def done(report: dict) -> None:
                self.query_one("#adopt-status", Static).update(
                    Text(f"{mode} finished for {folder}", style="dim")
                )
                self.query_one("#adopt-report", Static).update(
                    render_scan_report(report, self.app.glyphs)
                )
                self.app.notify(f"{mode} completed.")

            self.run_backend(
                lambda: backend.adopt_write(folder, mode),
                done,
                lambda error: self.query_one("#adopt-error", ErrorNotice).show_error(error),
                group="adopt-write",
            )

        detail = (
            f"Mode: {mode}\nSource: {folder}\nDestination: the governed Knowledge Base layer. "
            "Original files are never rewritten."
        )
        self.app.push_screen(ConfirmModal(f"Run {mode}?", detail), on_close)
