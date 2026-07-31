"""Packs: choose the domains that guide interpretation.

Multi-select over the built-in catalog, persisted through the same
pack-selection operation setup uses. Packs guide interpretation, retrieval
defaults, and review — they never force a folder structure or an ontology,
and their identifiers stay stable.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, SelectionList, Static
from textual.widgets.selection_list import Selection

from ..backend import BackendError
from ..theme import STYLE_OK
from ..widgets import AppHeader, ErrorNotice
from .base import ExomemScreen


class PacksScreen(ExomemScreen):
    SCREEN_TITLE = "Packs"

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("a", "apply", "apply selection"),
        Binding("u", "refresh", "refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._catalog: list[dict] = []
        self._persisted: list[str] = []

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        yield Static(
            Text(
                "Packs guide interpretation, retrieval defaults, and review. "
                "They never impose folders or an ontology; space toggles, a applies.",
                style="dim",
            ),
            classes="pane",
        )
        yield SelectionList(id="packs-list")
        yield Static(id="packs-status", classes="pane")
        error = ErrorNotice(id="packs-error")
        error.display = False
        yield error
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        backend = self.app.backend
        self.query_one("#packs-error", ErrorNotice).show_error(None)
        self.run_backend(backend.packs_state, self._on_state, self._on_error, group="packs")

    def _on_state(self, state: dict) -> None:
        self._catalog = list(state.get("catalog") or [])
        self._persisted = list(state.get("selected") or [])
        selection_list = self.query_one("#packs-list", SelectionList)
        selection_list.clear_options()
        for pack in self._catalog:
            pack_id = str(pack.get("id") or "")
            name = str(pack.get("name") or pack_id)
            description = str(pack.get("description") or "")
            prompt = Text()
            prompt.append(name, style="bold")
            if description:
                prompt.append(f" — {description}", style="dim")
            selection_list.add_option(
                Selection(prompt, pack_id, pack_id in self._persisted)
            )
        selection_list.focus()
        self._update_status()

    def _update_status(self) -> None:
        status = Text()
        status.append("persisted: ", style="dim")
        status.append(", ".join(self._persisted) if self._persisted else "defaults")
        self.query_one("#packs-status", Static).update(status)

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#packs-error", ErrorNotice).show_error(error)

    def action_apply(self) -> None:
        selection_list = self.query_one("#packs-list", SelectionList)
        chosen = [str(value) for value in selection_list.selected]
        if not chosen:
            self.app.notify(
                "Select at least one pack (the default pack applies otherwise).",
                severity="warning",
            )
            return
        backend = self.app.backend

        def done(_result: dict) -> None:
            self._persisted = chosen
            self._update_status()
            confirmation = Text()
            confirmation.append(f"{self.app.glyphs.get('ok', '*')} ", style=STYLE_OK)
            confirmation.append("Selection saved: ", style="bold")
            confirmation.append(", ".join(chosen))
            self.query_one("#packs-status", Static).update(confirmation)

        self.run_backend(
            lambda: backend.apply_packs(chosen), done, self._on_error, group="packs-apply"
        )
