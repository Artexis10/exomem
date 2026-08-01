"""Status: engine health and diagnostics, every warning with a next step.

A read-only composition of diagnostics that already exist — doctor preflight,
resource posture, warm state, hook checks, install provenance — rendered in
the same receipt language as everywhere else. Rendering never writes, never
downloads, and never shows a secret value.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from ..backend import BackendError
from ..format import fit
from ..theme import Skin
from ..widgets import AppFooter, AppHeader, RecoveryPanel, continuation, receipt
from .base import ExomemScreen

_OK_WORDS = ("ok", "pass", "passed", "valid", "ready", "true")
_WARN_WORDS = ("warn", "warning", "degraded", "stale")
_FAIL_WORDS = ("fail", "failed", "error", "corrupt", "unsafe", "false")


def state_of(value: str) -> str:
    lowered = value.lower()
    if lowered in _OK_WORDS:
        return "ok"
    if lowered in _WARN_WORDS:
        return "warn"
    if lowered in _FAIL_WORDS:
        return "fail"
    return "idle"


def render_doctor(report: dict, skin: Skin, budget: int) -> Text:
    text = Text()
    checks = report.get("checks") or []
    for check in checks:
        if not isinstance(check, dict):
            continue
        state = state_of(str(check.get("status") or check.get("state") or ""))
        text.append_text(
            receipt(
                skin,
                state,
                str(check.get("name") or "check"),
                str(check.get("detail") or check.get("message") or ""),
                budget=budget,
            )
        )
        text.append("\n")
        remediation = check.get("remediation") or check.get("fix")
        if remediation and state in ("warn", "fail"):
            text.append_text(continuation(skin, str(remediation), indent=2))
            text.append("\n")
    if not checks:
        text.append_text(
            receipt(
                skin,
                "ok" if report.get("success") else "warn",
                "preflight",
                "healthy" if report.get("success") else "issues found",
                budget=budget,
            )
        )
        text.append("\n")
    return text


def render_mapping(data: dict[str, Any], skin: Skin, budget: int) -> Text:
    text = Text()
    for key, value in data.items():
        if isinstance(value, dict):
            value = ", ".join(f"{name}={item}" for name, item in list(value.items())[:6])
        line = Text(no_wrap=True)
        line.append(f"{str(key):<14}", style=skin.secondary)
        line.append(fit(str(value), budget - 14), style=skin.text)
        text.append_text(line)
        text.append("\n")
    return text


class StatusScreen(ExomemScreen):
    SCREEN_TITLE = "Status"

    FOOTER_KEYS = (("u", "refresh"), ("esc", "back"))

    SECTIONS = (
        ("doctor", "Preflight — doctor, read-only"),
        ("resources", "Resources"),
        ("readiness", "Warm state"),
        ("hooks", "Agent hooks"),
        ("install", "Install"),
    )

    def compose(self) -> ComposeResult:
        yield AppHeader(self.SCREEN_TITLE)
        with Vertical(id="body"):
            yield RecoveryPanel(id="status-recovery")
            with VerticalScroll():
                for key, title in self.SECTIONS:
                    yield Static(title, id=f"status-title-{key}")
                    yield Static(id=f"status-{key}")
        yield AppFooter()

    def on_mount(self) -> None:
        skin = self.app.skin
        for key, title in self.SECTIONS:
            self.query_one(f"#status-title-{key}", Static).update(
                Text(f"\n{title}", style=skin.secondary)
            )
            self.query_one(f"#status-{key}", Static).update(
                Text(f"{skin.g('working')} reading", style=skin.dim)
            )

    def on_screen_resume(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        backend = self.app.backend
        self.query_one("#status-recovery", RecoveryPanel).hide()

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
        skin = self.app.skin
        budget = self.content_budget()
        for key, _title in self.SECTIONS:
            target = self.query_one(f"#status-{key}", Static)
            entry = sections.get(key) or {}
            if not entry.get("ok"):
                error = entry.get("error")
                text = Text()
                text.append_text(
                    receipt(
                        skin,
                        "warn",
                        "unavailable",
                        f"{error.code}: {error.message}" if isinstance(error, BackendError) else "",
                        budget=budget,
                    )
                )
                if isinstance(error, BackendError) and error.remediation:
                    text.append("\n")
                    text.append_text(continuation(skin, error.remediation, indent=2))
                target.update(text)
                continue
            data = entry.get("data")
            if key == "doctor" and isinstance(data, dict):
                target.update(render_doctor(data, skin, budget))
            elif key == "readiness" and isinstance(data, dict):
                if data.get("warming"):
                    pending = ", ".join(data.get("pending") or data.get("components") or [])
                    target.update(
                        receipt(
                            skin,
                            "warn",
                            "warming",
                            f"{pending or 'search lanes'} — completes in the background",
                            budget=budget,
                        )
                    )
                else:
                    target.update(
                        receipt(
                            skin,
                            "ok",
                            "warm",
                            "all requested lanes loaded (lean installs run lexical-only)",
                            budget=budget,
                        )
                    )
            elif key == "hooks" and isinstance(data, dict):
                if data.get("success"):
                    target.update(
                        receipt(skin, "ok", "hooks", "capture, retrieval, continuation", budget=budget)
                    )
                else:
                    text = Text()
                    text.append_text(
                        receipt(skin, "warn", "hooks", "not fully wired", budget=budget)
                    )
                    text.append("\n")
                    text.append_text(continuation(skin, "run: exomem install-hook", indent=2))
                    target.update(text)
            elif isinstance(data, dict):
                target.update(render_mapping(data, skin, budget))
            else:
                target.update(Text(str(data), style=skin.dim))

    def _on_error(self, error: BackendError) -> None:
        self.query_one("#status-recovery", RecoveryPanel).show(
            state="fail",
            word="diagnostics failed",
            what=error.message,
            facts=["Nothing was changed."],
            options=[("retry", "Try again", "")],
            budget=self.content_budget(),
        )

    def on_recovery_panel_chosen(self, event: RecoveryPanel.Chosen) -> None:
        event.stop()
        self.refresh_data()
