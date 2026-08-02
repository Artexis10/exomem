"""Packs: the domains that guide interpretation.

Multi-select over the built-in catalog, persisted through the same selection
path setup uses. Packs guide interpretation, retrieval defaults, and review —
they never impose a folder structure or an ontology, and their identifiers
stay stable, so choosing one is reversible in the ordinary sense of the word.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import SelectionList, Static
from textual.widgets.selection_list import Selection

from ..backend import BackendError
from ..format import wrap
from ..widgets import AppFooter, AppHeader, RecoveryPanel, receipt
from .base import ExomemScreen


class PacksScreen(ExomemScreen):
    SCREEN_TITLE = "Packs"

    FOOTER_KEYS = (("space", "toggle"), ("a", "apply"), ("u", "refresh"))

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("a", "apply", "apply selection"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._catalog: list[dict] = []
        self._persisted: list[str] = []

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        with Vertical(id="body", classes="-flush"):
            yield Static(id="packs-intro")
            yield SelectionList(id="packs-list")
            yield Static(id="packs-status")
            yield RecoveryPanel(id="packs-recovery")
        yield AppFooter()

    def on_mount(self) -> None:
        skin = self.app.skin
        intro = Text()
        for line in wrap(
            "Packs guide interpretation, retrieval defaults, and review. They never "
            "impose folders or an ontology. space toggles, a applies.",
            self.content_budget() - 2,
        ):
            intro.append(f"  {line}\n", style=skin.dim)
        self.query_one("#packs-intro", Static).update(intro)

    def on_screen_resume(self) -> None:
        # Refresh on every visit, not just first mount — installed screens
        # keep their instance across navigation.
        self.refresh_data()

    def refresh_data(self) -> None:
        backend = self.app.backend
        self.query_one("#packs-recovery", RecoveryPanel).hide()
        self.run_backend(backend.packs_state, self._on_state, self._on_error, group="packs")

    def _on_state(self, state: dict) -> None:
        skin = self.app.skin
        self._catalog = list(state.get("catalog") or [])
        self._persisted = list(state.get("selected") or [])
        selection_list = self.query_one("#packs-list", SelectionList)
        selection_list.clear_options()
        for pack in self._catalog:
            pack_id = str(pack.get("id") or "")
            prompt = Text(no_wrap=True)
            prompt.append(str(pack.get("name") or pack_id), style=skin.text)
            description = str(pack.get("description") or "")
            if description:
                prompt.append(f" — {description}", style=skin.dim)
            selection_list.add_option(Selection(prompt, pack_id, pack_id in self._persisted))
        selection_list.focus()
        self._update_status()

    def _update_status(self, applied: list[str] | None = None) -> None:
        skin = self.app.skin
        if applied is not None:
            line = receipt(skin, "done", "packs", ", ".join(applied), budget=self.content_budget())
        else:
            line = receipt(
                skin,
                "ok" if self._persisted else "idle",
                "in effect",
                ", ".join(self._persisted) if self._persisted else "defaults",
                budget=self.content_budget(),
            )
        block = Text("\n  ")
        block.append_text(line)
        self.query_one("#packs-status", Static).update(block)

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#packs-recovery", RecoveryPanel).show(
            state="fail",
            word="not saved",
            what=error.message,
            facts=["Nothing was changed."] + ([error.remediation] if error.remediation else []),
            options=[("retry", "Try again", ""), ("back", "Leave the selection as it was", "")],
            budget=self.content_budget(),
        )

    def on_recovery_panel_chosen(self, event: RecoveryPanel.Chosen) -> None:
        event.stop()
        self.query_one("#packs-recovery", RecoveryPanel).hide()
        if event.action == "retry":
            self.action_apply()
        else:
            self.query_one("#packs-list", SelectionList).focus()

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
            self._update_status(applied=chosen)
            self.app.record_receipt("done", "packs", ", ".join(chosen))

        self.run_backend(
            lambda: backend.apply_packs(chosen), done, self._on_error, group="packs-apply"
        )
