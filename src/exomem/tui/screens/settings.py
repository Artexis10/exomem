"""Settings: only what a user can understand and safely change from here.

Compute mode persists through the supported write path, and the screen shows
where it was stored. Appearance is session-level. The vault is displayed with
the real ways to change it — no fake editor, no secrets, ever.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import OptionList, Static

from ..backend import BackendError, VaultState
from ..format import wrap
from ..widgets import AppFooter, AppHeader, BarOptionList, BarRow, RecoveryPanel, receipt
from .base import ExomemScreen

MODES = (
    ("quiet", "quiet", "low footprint — CPU only"),
    ("normal", "normal", "the default balance"),
    ("performance", "performance", "use the GPU when one is present"),
)


class SettingsScreen(ExomemScreen):
    SCREEN_TITLE = "Settings"

    FOOTER_KEYS = (("enter", "apply"), ("t", "theme"), ("u", "refresh"))

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("t", "toggle_theme", "theme"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._mode: str | None = None

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        with Vertical(id="body", classes="-flush"):
            yield RecoveryPanel(id="settings-recovery")
            with VerticalScroll():
                yield Static(id="settings-mode-title")
                yield BarOptionList(id="settings-mode")
                yield Static(id="settings-mode-status")
                yield Static(id="settings-appearance")
                yield Static(id="settings-vault-title")
                yield Static(id="settings-vault")
        yield AppFooter()

    def on_mount(self) -> None:
        skin = self.app.skin
        self.query_one("#settings-mode-title", Static).update(
            Text("  Compute mode", style=skin.secondary)
        )
        self.query_one("#settings-appearance", Static).update(
            Text("\n  Appearance\n  t toggles dark and light for this session.", style=skin.dim)
        )
        self.query_one("#settings-vault-title", Static).update(
            Text("\n  Vault", style=skin.secondary)
        )
        self.query_one("#settings-mode", BarOptionList).set_rows(self._rows(None))

    def _rows(self, active: str | None) -> list[BarRow]:
        skin = self.app.skin
        rows: list[BarRow] = []
        for key, label, description in MODES:
            line = Text(no_wrap=True)
            chosen = key == active
            line.append("(")
            line.append(skin.g("ok") if chosen else skin.g("idle"), style=skin.accent if chosen else skin.dim)
            line.append(") ")
            line.append(f"{label:<12}", style=skin.text if chosen else skin.dim)
            line.append(description, style=skin.dim)
            rows.append(BarRow(key, [(1, line)]))
        return rows

    def on_screen_resume(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        backend = self.app.backend
        self.query_one("#settings-recovery", RecoveryPanel).hide()

        def job() -> dict:
            # Both reads run off-thread: resolve_vault stats the filesystem,
            # which can stall on network or WSL mounts.
            return {"policy": backend.mode(), "vault": backend.resolve_vault()}

        self.run_backend(job, self._on_loaded, self._on_error, group="settings")

    def _on_loaded(self, payload: dict) -> None:
        skin = self.app.skin
        policy = payload["policy"]
        self._mode = str(policy.get("mode") or "normal")
        options = self.query_one("#settings-mode", BarOptionList)
        keys = [key for key, _label, _description in MODES]
        options.set_rows(
            self._rows(self._mode),
            highlight=keys.index(self._mode) if self._mode in keys else 0,
        )
        options.focus()
        self.query_one("#settings-mode-status", Static).update(
            Text(f"\n  stored at {policy.get('config_path') or ''}", style=skin.dim)
        )
        self._render_vault(payload["vault"])

    def _render_vault(self, state: VaultState) -> None:
        skin = self.app.skin
        text = Text()
        if state.root is None:
            text.append("  no vault configured\n", style=skin.text)
        else:
            text.append(f"  {state.root}\n", style=skin.text)
            if not state.initialized:
                text.append("  not initialized yet\n", style=skin.dim)
        for line in wrap(
            "Change it with `exomem tui --vault <path>` or the EXOMEM_VAULT_PATH "
            "environment variable; `exomem setup` guides a fresh install.",
            self.content_budget() - 2,
        ):
            text.append(f"  {line}\n", style=skin.dim)
        self.query_one("#settings-vault", Static).update(text)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "settings-mode":
            return
        event.stop()
        mode = str(event.option_id or "")
        if not mode or mode == self._mode:
            return
        backend = self.app.backend

        def done(config_path: str) -> None:
            skin = self.app.skin
            self._mode = mode
            self.query_one("#settings-mode", BarOptionList).set_rows(
                self._rows(mode), highlight=[key for key, _l, _d in MODES].index(mode)
            )
            block = Text("\n  ")
            block.append_text(
                receipt(
                    skin,
                    "done",
                    "mode",
                    f"{mode} — stored at {config_path}; a running server applies it within ~10s",
                    budget=self.content_budget(),
                )
            )
            self.query_one("#settings-mode-status", Static).update(block)
            self.app.record_receipt("done", "mode", mode)

        self.run_backend(lambda: backend.set_mode(mode), done, self._on_error, group="settings-mode")

    def action_toggle_theme(self) -> None:
        self.app.toggle_theme()

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#settings-recovery", RecoveryPanel).show(
            state="fail",
            word="not changed",
            what=error.message,
            facts=["Nothing was changed."] + ([error.remediation] if error.remediation else []),
            options=[("retry", "Try again", "")],
            budget=self.content_budget(),
        )

    def on_recovery_panel_chosen(self, event: RecoveryPanel.Chosen) -> None:
        event.stop()
        self.refresh_data()
