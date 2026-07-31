"""Status: engine health and diagnostics, every warning with a next step.

Read-only composition of existing diagnostics: doctor preflight, resource
posture, warm/readiness state, hook checks, install provenance. Rendering
never writes, never downloads, never shows secret values.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Static

from ..backend import BackendError
from ..theme import STYLE_FAIL, STYLE_OK, STYLE_WARN
from ..widgets import AppHeader, ErrorNotice
from .base import ExomemScreen

_STATE_STYLE = {"ok": STYLE_OK, "pass": STYLE_OK, "warn": STYLE_WARN, "warning": STYLE_WARN}


def _state_fragment(state: str, glyphs: dict[str, str]) -> tuple[str, str]:
    lowered = state.lower()
    if lowered in ("ok", "pass", "passed", "valid", "ready", "true"):
        return glyphs.get("ok", "*"), STYLE_OK
    if lowered in ("warn", "warning", "degraded", "stale"):
        return glyphs.get("warn", "!"), STYLE_WARN
    if lowered in ("fail", "failed", "error", "corrupt", "unsafe", "false"):
        return glyphs.get("fail", "x"), STYLE_FAIL
    return glyphs.get("idle", "o"), "dim"


def render_doctor(report: dict, glyphs: dict[str, str]) -> Text:
    text = Text()
    checks = report.get("checks") or []
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status") or check.get("state") or "")
        glyph, style = _state_fragment(status, glyphs)
        text.append(f"{glyph} ", style=style)
        text.append(str(check.get("name") or "check"), style="bold")
        detail = check.get("detail") or check.get("message")
        if detail:
            text.append(f"  {detail}", style="dim")
        remediation = check.get("remediation") or check.get("fix")
        text.append("\n")
        if remediation and style in (STYLE_WARN, STYLE_FAIL):
            text.append(f"   {glyphs.get('arrow', '->')} {remediation}\n", style="dim")
    if not checks:
        overall = "healthy" if report.get("success") else "issues found"
        glyph, style = _state_fragment("ok" if report.get("success") else "warn", glyphs)
        text.append(f"{glyph} preflight {overall}\n", style=style)
    return text


def render_mapping(data: dict[str, Any], glyphs: dict[str, str]) -> Text:
    text = Text()
    for key, value in data.items():
        if isinstance(value, dict):
            value = ", ".join(f"{k}={v}" for k, v in list(value.items())[:6])
        text.append(f"{glyphs.get('bullet', '-')} ", style="dim")
        text.append(str(key), style="bold")
        text.append(f"  {value}\n", style="dim")
    return text


class StatusScreen(ExomemScreen):
    SCREEN_TITLE = "Status"

    BINDINGS = [
        *ExomemScreen.BINDINGS,
        Binding("u", "refresh", "refresh"),
    ]

    SECTIONS = (
        ("doctor", "Preflight (doctor, read-only)"),
        ("resources", "Resources"),
        ("readiness", "Warm state"),
        ("hooks", "Agent hooks"),
        ("install", "Install"),
    )

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        error = ErrorNotice(id="status-error")
        error.display = False
        yield error
        with VerticalScroll():
            for key, title in self.SECTIONS:
                yield Static(title, classes="pane-title")
                yield Static(Text("loading…", style="dim"), id=f"status-{key}", classes="pane")
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        backend = self.app.backend
        self.query_one("#status-error", ErrorNotice).show_error(None)

        def job() -> dict:
            sections: dict[str, Any] = {}
            for name, fn in (
                ("doctor", backend.doctor_report),
                ("resources", backend.resource_report),
                ("readiness", backend.readiness),
                ("hooks", backend.hook_status),
                ("install", backend.install_report),
            ):
                try:
                    sections[name] = {"ok": True, "data": fn()}
                except BackendError as exc:
                    sections[name] = {"ok": False, "error": exc}
                except Exception as exc:  # noqa: BLE001 — one section must not kill Status
                    sections[name] = {"ok": False, "error": BackendError("INTERNAL", str(exc))}
            return sections

        self.run_backend(job, self._on_sections, self._on_error, group="status")

    def _on_sections(self, sections: dict) -> None:
        glyphs = self.app.glyphs
        for key, _title in self.SECTIONS:
            target = self.query_one(f"#status-{key}", Static)
            entry = sections.get(key) or {}
            if not entry.get("ok"):
                error = entry.get("error")
                text = Text()
                text.append(f"{glyphs.get('warn', '!')} unavailable", style=STYLE_WARN)
                if isinstance(error, BackendError):
                    text.append(f"  {error.code}: {error.message}", style="dim")
                    if error.remediation:
                        text.append(
                            f"\n   {glyphs.get('arrow', '->')} {error.remediation}", style="dim"
                        )
                target.update(text)
                continue
            data = entry.get("data")
            if key == "doctor" and isinstance(data, dict):
                target.update(render_doctor(data, glyphs))
            elif key == "readiness" and isinstance(data, dict):
                if data.get("warming"):
                    pending = ", ".join(data.get("pending") or data.get("components") or [])
                    text = Text()
                    text.append(f"{glyphs.get('warn', '!')} warming", style=STYLE_WARN)
                    text.append(f"  {pending or 'search lanes'} — completes in the background\n", style="dim")
                    target.update(text)
                else:
                    text = Text()
                    text.append(f"{glyphs.get('ok', '*')} ready", style=STYLE_OK)
                    text.append("  all requested lanes loaded (lean installs run lexical-only)\n", style="dim")
                    target.update(text)
            elif key == "hooks" and isinstance(data, dict):
                glyph, style = _state_fragment("ok" if data.get("success") else "warn", glyphs)
                text = Text()
                text.append(f"{glyph} ", style=style)
                if data.get("success"):
                    text.append("capture + retrieval + continuation hooks installed\n")
                else:
                    text.append("not fully wired")
                    text.append(f"\n   {glyphs.get('arrow', '->')} run: exomem install-hook\n", style="dim")
                target.update(text)
            elif isinstance(data, dict):
                target.update(render_mapping(data, glyphs))
            else:
                target.update(Text(str(data), style="dim"))

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#status-error", ErrorNotice).show_error(error)
