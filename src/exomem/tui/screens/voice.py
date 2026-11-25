"""Keep a voice: the owner names whose voice a speaker label of a recording is.

Keeping a voice enrols a voice profile, so later recordings name that speaker without help. It is
an owner action and never a connector tool, so it lives here and in `exomem enroll-speaker`. The
screen shows the label's own lines first; only after the owner chooses to keep the voice does the
backend read the audio and compute a voiceprint. The recording is a path in this vault; a Cloud
vault uses the command, which reads the original through the signed-in Cloud download.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

from ..backend import BackendError
from ..format import fit
from ..widgets import AppFooter, AppHeader, RecoveryPanel, receipt
from .base import ExomemScreen

INTRO = (
    "Name whose voice a speaker label is, so later recordings name that speaker. "
    "The audio is read only after you confirm."
)

FIELDS = (
    ("voice-recording", "recording", "vault path of the recording, as its Source page shows"),
    ("voice-label", "label", "the speaker label on its lines, for example Speaker A"),
    ("voice-name", "name", "who the speaker is"),
)


class VoiceScreen(ExomemScreen):
    SCREEN_TITLE = "Keep a voice"

    FOOTER_KEYS = (("enter", "next"), ("esc", "back"))

    #: The recording, label and name that the owner was asked about; set when the sample is shown.
    _pending: tuple[str, str, str] = ("", "", "")

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        with Vertical(id="body", classes="-flush"):
            yield Static(id="voice-intro")
            for field_id, word, hint in FIELDS:
                yield Static(id=f"{field_id}-line")
                yield Input(placeholder=hint, id=field_id, classes="line-input")
            yield Static(id="voice-status")
            yield RecoveryPanel(id="voice-recovery")
        yield AppFooter()

    def on_mount(self) -> None:
        skin = self.app.skin
        self.query_one("#voice-intro", Static).update(Text(f"  {INTRO}", style=skin.dim))
        for field_id, word, _hint in FIELDS:
            self.query_one(f"#{field_id}-line", Static).update(Text(f"  {word}", style=skin.secondary))
        self.query_one("#voice-recording", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        order = [field_id for field_id, _word, _hint in FIELDS]
        position = order.index(event.input.id)
        if position + 1 < len(order):
            self.query_one(f"#{order[position + 1]}", Input).focus()
            return
        self._show_sample()

    def _values(self) -> tuple[str, str, str]:
        recording, label, name = (self.query_one(f"#{field_id}", Input).value.strip() for field_id, _word, _hint in FIELDS)
        return recording, label, name

    def _show_sample(self) -> None:
        recording, label, name = self._values()
        if not (recording and label and name):
            self.app.notify("Fill in the recording, the label and the name.", severity="warning")
            return
        self.query_one("#voice-recovery", RecoveryPanel).hide()
        self.run_backend(
            lambda: self.app.backend.voice_sample(recording, label),
            lambda sample: self._ask(recording, label, name, sample),
            self._on_error,
            group="voice-sample",
        )

    def _ask(self, recording: str, label: str, name: str, sample: dict) -> None:
        facts = [fit(line, self.content_budget() - 4) for line in sample.get("lines") or []]
        self.query_one("#voice-recovery", RecoveryPanel).show(
            state="idle",
            word="confirm",
            what=f"{label} speaks in {sample.get('spans', 0)} stretch(es) of this recording",
            facts=[*facts, f"Keeping this voice reads the audio and stores a voiceprint as {name}."],
            options=[("keep", f"Keep this voice as {name}", ""), ("cancel", "Do not keep it", "no audio is read")],
            budget=self.content_budget(),
        )
        self._pending = (recording, label, name)

    def on_recovery_panel_chosen(self, event: RecoveryPanel.Chosen) -> None:
        event.stop()
        self.query_one("#voice-recovery", RecoveryPanel).hide()
        if event.action != "keep":
            self.query_one("#voice-name", Input).focus()
            return
        recording, label, name = self._pending
        self.run_backend(
            lambda: self.app.backend.voice_enroll(recording, label, name),
            lambda kept: self._kept(name, kept),
            self._on_error,
            group="voice-enroll",
        )

    def _kept(self, name: str, kept: dict) -> None:
        line = receipt(self.app.skin, "done", "voice kept", f"{name} ({kept.get('samples', 1)} sample(s))", budget=self.content_budget())
        self.query_one("#voice-status", Static).update(Text("  ").append_text(line))
        self.app.record_receipt("done", "voice kept", name)

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#voice-recovery", RecoveryPanel).show(
            state="fail",
            word="not kept",
            what=error.message,
            facts=["No voice was stored.", error.code] + ([error.remediation] if error.remediation else []),
            options=[("edit", "Change the recording, label or name", "")],
            budget=self.content_budget(),
        )
