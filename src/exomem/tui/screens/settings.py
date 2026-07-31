"""Settings: only what a user can understand and safely change from here.

Compute mode persists through the supported mode-write path (storage location
shown). Appearance is session-level. The vault is displayed with the real ways
to change it — no fake editors, no secrets, ever.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, RadioButton, RadioSet, Static

from ..backend import BackendError
from ..theme import STYLE_OK
from ..widgets import AppHeader, ErrorNotice
from .base import ExomemScreen

_MODES = ("quiet", "normal", "performance")


class SettingsScreen(ExomemScreen):
    SCREEN_TITLE = "Settings"

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("t", "toggle_theme", "theme"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._loaded_mode: str | None = None

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        error = ErrorNotice(id="settings-error")
        error.display = False
        yield error
        with VerticalScroll():
            yield Static("Compute mode", classes="pane-title")
            yield Static(
                Text(
                    "quiet = low footprint (CPU) · normal = default · performance = use the GPU. "
                    "Applies to the local engine; a running server picks it up live.",
                    style="dim",
                ),
                classes="pane",
            )
            with RadioSet(id="settings-mode"):
                for mode in _MODES:
                    yield RadioButton(mode, id=f"mode-{mode}")
            yield Static(id="settings-mode-status", classes="pane")

            yield Static("Appearance", classes="pane-title")
            yield Static(
                Text("t toggles dark/light for this session.", style="dim"),
                classes="pane",
            )

            yield Static("Vault", classes="pane-title")
            yield Static(id="settings-vault", classes="pane")
        yield Footer()

    def on_mount(self) -> None:
        self._load()

    def _load(self) -> None:
        backend = self.app.backend

        def job() -> dict:
            # Both reads run off-thread: resolve_vault stats the filesystem,
            # which can stall on network/WSL mounts.
            return {"policy": backend.mode(), "vault": backend.resolve_vault()}

        def done(payload: dict) -> None:
            policy = payload["policy"]
            mode = str(policy.get("mode") or "normal")
            self._loaded_mode = mode
            button = self.query_one(f"#mode-{mode}", RadioButton)
            button.value = True
            status = Text()
            status.append("stored at ", style="dim")
            status.append(str(policy.get("config_path") or ""), style="dim")
            self.query_one("#settings-mode-status", Static).update(status)
            self._render_vault(payload["vault"])

        self.run_backend(job, done, self._on_error, group="settings")

    def _render_vault(self, state) -> None:
        text = Text()
        if state.root is None:
            text.append("No vault configured.\n")
        else:
            text.append(f"{state.root}\n")
            if not state.initialized:
                text.append("(not initialized yet)\n", style="dim")
        text.append(
            "Change it with `exomem tui --vault <path>` or the EXOMEM_VAULT_PATH "
            "environment variable; `exomem setup` guides a fresh install.",
            style="dim",
        )
        self.query_one("#settings-vault", Static).update(text)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id != "settings-mode" or event.pressed is None:
            return
        # Persist by stable widget id, never by display label.
        mode = str(event.pressed.id or "").removeprefix("mode-")
        if mode not in _MODES or self._loaded_mode is None or mode == self._loaded_mode:
            return
        backend = self.app.backend

        def done(config_path: str) -> None:
            self._loaded_mode = mode
            status = Text()
            status.append(f"{self.app.glyphs.get('ok', '*')} ", style=STYLE_OK)
            status.append(f"mode set to {mode}", style="bold")
            status.append(f"  ({config_path}) — a running server applies it within ~10s", style="dim")
            self.query_one("#settings-mode-status", Static).update(status)

        self.run_backend(lambda: backend.set_mode(mode), done, self._on_error, group="settings-mode")

    def action_toggle_theme(self) -> None:
        self.app.theme = (
            "exomem-light" if self.app.theme == "exomem-dark" else "exomem-dark"
        )

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#settings-error", ErrorNotice).show_error(error)
