"""Adopt: read an existing folder of notes before deciding anything.

Scan-only is the default and works before a Knowledge Base exists — it writes
nothing, which is why it can be the very first thing a cautious user does.
Write modes are separate, confirmed steps that say what will be written and
where; originals are never rewritten, because adoption is a governed overlay,
not an in-place migration.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Input, Static

from ..backend import BackendError
from ..format import fit, truncate_path, wrap
from ..theme import Skin
from ..widgets import (
    AppFooter,
    AppHeader,
    ConfirmModal,
    RecoveryPanel,
    receipt,
)
from .base import ExomemScreen


def scan_report(report: dict, folder: Path, skin: Skin, budget: int) -> Text:
    """The read-only finding, as receipts rather than a wall of prose."""
    totals = (report.get("summary") or {}).get("totals") or {}
    governance = report.get("governance") or {}
    text = Text()
    text.append_text(
        receipt(skin, "done", "scanned", f"{folder} — read without writing anything", budget=budget)
    )
    text.append("\n")
    detail = (
        f"{totals.get('files', 0)} files {skin.g('bullet')} {totals.get('markdown', 0)} markdown "
        f"{skin.g('bullet')} {totals.get('dirs', 0)} folders {skin.g('bullet')} governed layer: "
        + ("present" if governance.get("kb_present") else "none")
    )
    text.append(f"     {fit(detail, budget - 5)}\n", style=skin.dim)

    packs = report.get("pack_suggestions") or []
    if packs:
        text.append("\nLikely packs\n", style=skin.secondary)
        for pack in packs[:6]:
            name = str(pack.get("name") or pack.get("id") or "unknown")
            signals = ", ".join(str(signal) for signal in pack.get("matched_signals") or [])
            text.append(f"  {name}", style=skin.text)
            if signals:
                text.append(f"  ({signals})", style=skin.dim)
            text.append("\n")

    actions = report.get("next_actions") or []
    if actions:
        text.append("\nSafe next steps\n", style=skin.secondary)
        for action in actions:
            status = str(action.get("status") or "")
            state = "ok" if status in ("available", "ready") else "idle"
            glyph, style = skin.status(state)
            text.append(f"  {glyph} ", style=style)
            text.append(str(action.get("action") or ""), style=skin.text)
            description = action.get("description")
            if description:
                text.append(f" — {description}", style=skin.dim)
            text.append("\n")
    return text


class AdoptScreen(ExomemScreen):
    SCREEN_TITLE = "Adopt"

    FOOTER_KEYS = (
        ("enter", "scan"),
        ("m", "save manifest"),
        ("c", "copy as sources"),
        ("u", "refresh"),
    )

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
        with Vertical(id="body", classes="-flush"):
            yield Input(
                placeholder="folder of notes to read (a scan changes nothing)",
                id="adopt-path",
                classes="line-input",
            )
            yield RecoveryPanel(id="adopt-recovery")
            with VerticalScroll(id="adopt-report-pane"):
                yield Static(id="adopt-report")
        yield AppFooter()

    def on_mount(self) -> None:
        skin = self.app.skin
        intro = Text()
        for line in wrap(
            "Point at any folder to see what adoption would find. Scan first, decide "
            "later — write modes are separate, confirmed steps.",
            self.content_budget() - 2,
        ):
            intro.append(f"  {line}\n", style=skin.dim)
        self.query_one("#adopt-report", Static).update(intro)
        self.query_one("#adopt-path", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "adopt-path":
            return
        raw = event.value.strip()
        if raw:
            self.scan_folder(Path(raw).expanduser())

    def scan_folder(self, folder: Path) -> None:
        """Public entry (also used by first run): validate, then scan."""
        if not folder.is_dir():
            self._on_error(BackendError("NOT_A_FOLDER", f"{folder} is not a directory"))
            return
        self._scan(folder)

    def _scan(self, folder: Path) -> None:
        backend = self.app.backend
        skin = self.app.skin
        self.query_one("#adopt-recovery", RecoveryPanel).hide()
        self.query_one("#adopt-report", Static).update(
            Text(f"  {skin.g('working')} reading {folder} — nothing is written", style=skin.dim)
        )

        def done(report: dict) -> None:
            self._scanned = folder
            self._report = report
            self.query_one("#adopt-report", Static).update(
                scan_report(report, folder, skin, self.content_budget())
            )
            # Move focus off the path input so m/c reach the screen bindings
            # instead of being typed into the field.
            self.query_one("#adopt-report-pane").focus()
            self.app.record_receipt("done", "scanned", truncate_path(str(folder), 34))

        self.run_backend(lambda: backend.adopt_scan(folder), done, self._on_error, group="adopt-scan")

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#adopt-recovery", RecoveryPanel).show(
            state="fail",
            word="not scanned",
            what=error.message,
            facts=["Nothing was changed."] + ([error.remediation] if error.remediation else []),
            options=[("retry", "Choose another folder", "your typing is kept")],
            budget=self.content_budget(),
        )

    def on_recovery_panel_chosen(self, event: RecoveryPanel.Chosen) -> None:
        event.stop()
        self.query_one("#adopt-recovery", RecoveryPanel).hide()
        self.query_one("#adopt-path", Input).focus()

    def action_write_mode(self, mode: str) -> None:
        if self._scanned is None or self._report is None:
            self.app.notify("Scan a folder first.", severity="warning")
            return
        folder = self._scanned
        backend = self.app.backend
        skin = self.app.skin

        def on_close(choice: str | None) -> None:
            if choice != "confirm":
                return

            def done(report: dict) -> None:
                self.query_one("#adopt-report", Static).update(
                    scan_report(report, folder, skin, self.content_budget())
                )
                self.app.record_receipt("done", mode.replace("-", " "), truncate_path(str(folder), 30))
                self.app.notify(f"{mode} completed.")

            self.run_backend(
                lambda: backend.adopt_write(folder, mode), done, self._on_error, group="adopt-write"
            )

        destination = self.app.backend.vault_root
        self.app.push_screen(
            ConfirmModal(
                f"Run {mode}?",
                f"Source: {folder}\n"
                f"Destination: the governed layer of {destination or 'your configured vault'}.\n"
                "Original files are never rewritten; folders outside the vault are refused.",
                f"Run {mode} — writes into the governed layer",
                "Back — nothing is written",
                "enter choose · esc back — nothing written yet",
            ),
            on_close,
        )

    def refresh_data(self) -> None:
        if self._scanned is not None:
            self._scan(self._scanned)
