"""The Textual application: navigation, palette, help, and session startup.

The app paints immediately and talks to the backend only through worker
threads; the first data fetch (vault resolution + overview) happens after the
first frame so launch is never blocked on the registry import or a cold cache.
"""

from __future__ import annotations

import sys
from functools import partial

from textual.app import App
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.reactive import reactive

from . import theme as theme_module
from .backend import ExomemBackend, VaultState
from .screens import (
    AdoptScreen,
    AskScreen,
    CaptureScreen,
    ContinueScreen,
    HomeScreen,
    OnboardingScreen,
    PacksScreen,
    ReviewScreen,
    SettingsScreen,
    StatusScreen,
)
from .screens.base import ExomemScreen
from .screens.home import DESTINATIONS
from .widgets import ConfirmModal, HelpModal

GLOBAL_HELP_ROWS: list[tuple[str, str]] = [
    ("ctrl+p", "command palette"),
    ("?", "this help overlay"),
    ("esc", "back (never discards typed input silently)"),
    ("ctrl+q", "quit"),
]


class GotoCommands(Provider):
    """Palette entries: every primary screen is reachable from anywhere."""

    async def discover(self) -> Hits:
        for name, _key, title, description in DESTINATIONS:
            yield DiscoveryHit(
                f"Go to {title}", partial(self.app.goto, name), help=description
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, _key, title, description in DESTINATIONS:
            label = f"Go to {title}"
            score = matcher.match(label)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    partial(self.app.goto, name),
                    help=description,
                )


class ExomemTuiApp(App):
    TITLE = "exomem"
    CSS = theme_module.APP_CSS
    COMMANDS = App.COMMANDS | {GotoCommands}
    BINDINGS = [
        Binding("ctrl+q", "confirm_quit", "quit", show=False, priority=True),
    ]

    context_label: reactive[str] = reactive("", init=False)

    SECTION_FACTORIES: dict[str, type[ExomemScreen]] = {
        "continue": ContinueScreen,
        "ask": AskScreen,
        "capture": CaptureScreen,
        "review": ReviewScreen,
        "adopt": AdoptScreen,
        "packs": PacksScreen,
        "status": StatusScreen,
        "settings": SettingsScreen,
    }

    def __init__(
        self,
        backend: ExomemBackend | None = None,
        *,
        vault: str | None = None,
        glyphs: dict[str, str] | None = None,
    ):
        super().__init__()
        self.backend = backend if backend is not None else ExomemBackend(vault)
        if glyphs is None:
            glyphs = theme_module.pick_glyphs(getattr(sys.stdout, "encoding", None))
        self.glyphs = glyphs
        self._sections: dict[str, ExomemScreen] = {}
        self._home = HomeScreen()
        self._vault_state: VaultState | None = None
        self._onboarding_offered = False

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #
    def get_default_screen(self) -> HomeScreen:
        # Home IS the base of the stack — `_pop_to_home` relies on it.
        return self._home

    def on_mount(self) -> None:
        self.register_theme(theme_module.EXOMEM_DARK)
        self.register_theme(theme_module.EXOMEM_LIGHT)
        self.theme = "exomem-dark"
        self.run_worker(self._startup, thread=True, group="startup", exclusive=True)

    def _startup(self) -> None:
        state = self.backend.resolve_vault()
        self.call_from_thread(self._apply_vault_state, state)
        if not state.initialized:
            return
        self.backend.start_runtime()
        sections = self.backend.overview()
        self.call_from_thread(self._apply_overview, sections)

    def _apply_vault_state(self, state: VaultState) -> None:
        self._vault_state = state
        if state.initialized and state.root is not None:
            self.context_label = state.root.name
            return
        if state.root is None:
            self.context_label = "no vault configured"
        else:
            self.context_label = f"{state.root.name} · not initialized"
        self._home.show_first_run(state.detail)
        if not self._onboarding_offered:
            self._onboarding_offered = True
            self.push_screen(OnboardingScreen())

    def on_vault_ready(self) -> None:
        """Called by onboarding once a vault is connected or created."""

        def job() -> None:
            state = self.backend.resolve_vault()
            self.call_from_thread(self._apply_vault_state, state)
            if state.initialized:
                self.backend.start_runtime()
                sections = self.backend.overview()
                self.call_from_thread(self._apply_overview, sections)

        self.run_worker(job, thread=True, group="startup", exclusive=True)

    def _apply_overview(self, sections: dict) -> None:
        mode_entry = sections.get("mode") or {}
        if mode_entry.get("ok"):
            mode = (mode_entry.get("data") or {}).get("mode")
            if mode and self._vault_state and self._vault_state.root is not None:
                self.context_label = f"{self._vault_state.root.name} · {mode}"
        if self._home.is_attached:
            self._home.apply_overview(sections)

    def reload_overview(self) -> None:
        def job() -> None:
            sections = self.backend.overview()
            self.call_from_thread(self._apply_overview, sections)

        self.run_worker(job, thread=True, group="overview", exclusive=True)

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def goto(self, name: str) -> None:
        if name == "home":
            self._pop_to_home()
            return
        factory = self.SECTION_FACTORIES.get(name)
        if factory is None:
            self.notify(
                f"{name.capitalize()} is not wired up yet in this build.",
                severity="warning",
            )
            return
        screen = self._sections.get(name)
        if screen is None:
            screen = factory()
            self._sections[name] = screen
        if self.screen is screen:
            return
        self._pop_to_home()
        self.push_screen(screen)

    def _pop_to_home(self) -> None:
        while len(self.screen_stack) > 1:
            self.pop_screen()

    def back(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()
        else:
            self.action_confirm_quit()

    def action_back(self) -> None:
        self.back()

    def action_help(self) -> None:
        screen = self.screen
        rows: list[tuple[str, str]] = []
        title = "exomem"
        if isinstance(screen, ExomemScreen):
            title = screen.SCREEN_TITLE or title
            rows.extend(screen.help_rows())
        rows.extend(GLOBAL_HELP_ROWS)
        self.push_screen(HelpModal(title, rows))

    def action_confirm_quit(self) -> None:
        def on_close(confirmed: bool | None) -> None:
            if confirmed:
                self.exit(0)

        self.push_screen(
            ConfirmModal(
                "Quit exomem?",
                "Atomic writes protect the vault; anything still typing here is lost.",
            ),
            on_close,
        )


def run_tui(*, vault: str | None = None) -> int:
    app = ExomemTuiApp(vault=vault)
    app.run()
    return app.return_code or 0
