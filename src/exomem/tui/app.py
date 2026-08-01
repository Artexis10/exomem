"""The Textual application: navigation, palette, help, and session startup.

The app paints immediately and talks to the backend only through worker
threads; the first data fetch (vault resolution + overview) happens after the
first frame, so launch is never blocked on a registry import or a cold cache.

It also owns the two pieces of state that belong to the session rather than to
any one screen: the rendering skin (glyph set + color roles, chosen once from
the terminal's encoding and `NO_COLOR`), and the session receipt log — the
`✓`/`▸` lines Home shows under "This session". Receipts are process state, not
vault state, and say so on screen.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace
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
    FirstRunScreen,
    HomeScreen,
    PacksScreen,
    ReviewScreen,
    SettingsScreen,
    StatusScreen,
)
from .screens.base import ExomemScreen
from .screens.home import DESTINATIONS
from .widgets import ConfirmModal, HelpModal

GLOBAL_HELP_ROWS: list[tuple[str, str]] = [
    ("^p", "command palette"),
    ("?", "this overlay"),
    ("u", "refresh this screen"),
    ("esc", "back — never discards typed input silently"),
    ("^q", "quit"),
]


@dataclass(frozen=True)
class SessionReceipt:
    """One thing that happened in this process, as a readable line."""

    state: str
    word: str
    detail: str


class GotoCommands(Provider):
    """Palette entries: every primary screen is reachable from anywhere."""

    async def discover(self) -> Hits:
        for name, _key, title, description in DESTINATIONS:
            yield DiscoveryHit(f"Go to {title}", partial(self.app.goto, name), help=description)

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
        color: bool | None = None,
    ):
        super().__init__()
        self.backend = backend if backend is not None else ExomemBackend(vault)
        if glyphs is None:
            glyphs = theme_module.pick_glyphs(getattr(sys.stdout, "encoding", None))
        if color is None:
            color = not theme_module.no_color_requested()
        self.glyphs = glyphs
        self.skin = theme_module.make_skin(glyphs, color=color)
        self.session_receipts: list[SessionReceipt] = []
        #: Snapshot tests pin this so stored frames do not diff on timing.
        self.fixed_elapsed_ms: float | None = None
        self._sections: dict[str, ExomemScreen] = {}
        self._home = HomeScreen()
        self._vault_state: VaultState | None = None
        self._first_run_offered = False

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
        # First run never re-runs once a vault is connected; this is the only
        # place it is offered, and only when there is genuinely nothing yet.
        if not self._first_run_offered:
            self._first_run_offered = True
            self.push_screen(FirstRunScreen())

    def on_vault_ready(self) -> None:
        """Called by first run once a vault is connected or created."""

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
    # Session receipts
    # ------------------------------------------------------------------ #
    def record_receipt(self, state: str, word: str, detail: str) -> None:
        """Log something that happened, for Home's "This session" block."""
        self.session_receipts.append(SessionReceipt(state, word, detail))
        if self._home.is_attached:
            self._home.refresh_session_receipts()

    def elapsed_ms(self, started: float) -> float:
        """How long a measured call actually took.

        Routed through the app so golden snapshots can pin it: a timing that
        varies by a millisecond would make every stored frame a false diff.
        """
        if self.fixed_elapsed_ms is not None:
            return self.fixed_elapsed_ms
        return (time.perf_counter() - started) * 1000

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def goto(self, name: str) -> None:
        if name == "home":
            self._pop_to_home()
            return
        factory = self.SECTION_FACTORIES.get(name)
        if factory is None:
            self.notify(f"{name.capitalize()} is not part of this build.", severity="warning")
            return
        screen = self._sections.get(name)
        if screen is None:
            screen = factory()
            self._sections[name] = screen
            # Installed screens survive pop_screen (Textual removes uninstalled
            # screens on replacement, which would close their message pumps and
            # leave a revisited instance dead).
            self.install_screen(screen, name=f"section-{name}")
        if self.screen is screen:
            return
        self._pop_to_home()
        self.push_screen(screen)

    def ask_for(self, query: str) -> None:
        """Open Ask with a query already in the field, and run it."""
        self.goto("ask")
        screen = self._sections.get("ask")
        if isinstance(screen, AskScreen) and query:
            # push_screen resolves on the next refresh; prefill after it.
            self.call_after_refresh(screen.prefill, query)

    def finish_first_run(self, goto: str = "home") -> None:
        self._pop_to_home()
        if goto != "home":
            self.goto(goto)

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

    def toggle_theme(self) -> None:
        """Switch dark/light for this session — completely, not cosmetically.

        Styled text is built from the skin at render time, so a theme swap has
        to rebuild what has already been drawn. Cached section screens are
        dropped and the current one is reopened; leaving half the UI in the
        previous palette would be a toggle that only pretends to work.
        """
        if self.theme == "exomem-dark":
            self.theme = "exomem-light"
            self.skin = replace(self.skin, **theme_module.LIGHT_SKIN_OVERRIDES)
        else:
            self.theme = "exomem-dark"
            self.skin = theme_module.make_skin(self.glyphs, color=self.skin.color)

        current = next(
            (name for name, screen in self._sections.items() if screen is self.screen), None
        )
        self._pop_to_home()
        for name in list(self._sections):
            self.uninstall_screen(f"section-{name}")
        self._sections.clear()
        self._home.repaint()
        if current is not None:
            self.goto(current)

    def action_confirm_quit(self) -> None:
        def on_close(choice: str | None) -> None:
            if choice == "confirm":
                self.exit(0)

        self.push_screen(
            ConfirmModal(
                "Quit exomem?",
                "Everything saved is already written to your vault.\n"
                "Anything still being typed here is not.",
                "Quit",
                "Stay",
                "enter choose · esc back — nothing is written either way",
            ),
            on_close,
        )


def run_tui(*, vault: str | None = None) -> int:
    app = ExomemTuiApp(vault=vault)
    app.run()
    return app.return_code or 0
