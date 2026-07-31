"""First run: choose, create, or scan — each step skippable.

The measured first-run target is "demo, choose vault, scan, initialize": this
screen offers exactly the supported paths (point at an existing vault, create
a fresh one via init, or scan a folder read-only first) and never forces
optional features. Skipping lands on Home, where everything remains reachable.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from ..backend import BackendError
from ..theme import STYLE_OK
from ..widgets import AppHeader, ErrorNotice
from .base import ExomemScreen

_CHOICES = (
    ("use", "Use an existing vault", "point at a folder that already holds a Knowledge Base"),
    ("create", "Create a fresh vault", "initialize the governed layer in a folder you choose"),
    ("scan", "Scan a folder first", "read-only look at existing notes before deciding"),
    ("skip", "Skip for now", "everything stays reachable from Home"),
)


class OnboardingScreen(ExomemScreen):
    SCREEN_TITLE = "Welcome"

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("escape", "skip", "skip", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._pending_action: str | None = None

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        yield Static(
            Text(
                "Exomem keeps durable, governed memory in a plain-markdown vault you own.\n"
                "Capture raw thoughts, compile conclusions with provenance, and recall them "
                "with evidence — from here, the CLI, or your agents.",
            ),
            classes="pane",
        )
        yield OptionList(
            *[
                Option(self._prompt(title, description), id=name)
                for name, title, description in _CHOICES
            ],
            id="onboarding-choices",
        )
        path_input = Input(placeholder="folder path", id="onboarding-path")
        path_input.display = False
        yield path_input
        yield Static(id="onboarding-status", classes="pane")
        error = ErrorNotice(id="onboarding-error")
        error.display = False
        yield error
        yield Footer()

    @staticmethod
    def _prompt(title: str, description: str) -> Text:
        prompt = Text()
        prompt.append(title, style="bold")
        prompt.append(f"\n  {description}", style="dim")
        return prompt

    def on_mount(self) -> None:
        self.query_one("#onboarding-choices", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "onboarding-choices":
            return
        choice = event.option_id
        if choice == "skip":
            self.action_skip()
            return
        self._pending_action = choice
        path_input = self.query_one("#onboarding-path", Input)
        path_input.display = True
        placeholder = {
            "use": "path to the folder containing your Knowledge Base",
            "create": "folder to initialize (created if missing)",
            "scan": "folder to scan (read-only)",
        }[str(choice)]
        path_input.placeholder = placeholder
        path_input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "onboarding-path":
            return
        raw = event.value.strip()
        if not raw or self._pending_action is None:
            return
        folder = Path(raw).expanduser()
        action = self._pending_action
        if action == "scan":
            self.app.goto("adopt")
            adopt = self.app.screen
            from .adopt import AdoptScreen

            if isinstance(adopt, AdoptScreen):
                adopt.scan_folder(folder)
            return
        if action == "use":
            self._use_existing(folder)
        elif action == "create":
            self._create_vault(folder)

    def _status(self, message: str) -> None:
        self.query_one("#onboarding-status", Static).update(Text(message, style="dim"))

    def _use_existing(self, folder: Path) -> None:
        backend = self.app.backend
        self.query_one("#onboarding-error", ErrorNotice).show_error(None)

        def job():
            state = backend.adopt_vault_root(folder)
            if not state.initialized:
                raise BackendError(
                    "NOT_A_VAULT",
                    f"{folder} holds no Knowledge Base yet",
                    "choose 'Create a fresh vault' to initialize it, or scan it first",
                )
            return state

        def done(_state) -> None:
            self._finish(f"Vault connected: {folder}")

        self.run_backend(job, done, self._on_error, group="onboarding")

    def _create_vault(self, folder: Path) -> None:
        backend = self.app.backend
        self.query_one("#onboarding-error", ErrorNotice).show_error(None)
        self._status(f"initializing {folder}…")

        def job():
            folder.mkdir(parents=True, exist_ok=True)
            backend.init_vault(folder)
            return backend.adopt_vault_root(folder)

        def done(_state) -> None:
            self._finish(f"Vault created: {folder}")

        self.run_backend(job, done, self._on_error, group="onboarding")

    def _finish(self, message: str) -> None:
        confirmation = Text()
        confirmation.append(f"{self.app.glyphs.get('ok', '*')} ", style=STYLE_OK)
        confirmation.append(message, style="bold")
        confirmation.append(
            "\nOptional next steps — packs (6) tune interpretation; `exomem install-hook` "
            "wires automatic capture/retrieval into your agents. Both can wait.",
            style="dim",
        )
        self.query_one("#onboarding-status", Static).update(confirmation)
        self.app.on_vault_ready()

    def _on_error(self, error: BackendError) -> None:
        self._status("")
        self.query_one("#onboarding-error", ErrorNotice).show_error(error)

    def action_skip(self) -> None:
        self.app.back()
