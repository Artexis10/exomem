"""Fixtures for the TUI suite. Skips whole when the `tui` extra is absent."""

from __future__ import annotations

import pytest

pytest.importorskip("textual", reason="tui extra not installed")

from _fake import FakeBackend  # noqa: E402 — same-directory test helper


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def make_app(fake_backend: FakeBackend):
    from exomem.tui.app import ExomemTuiApp
    from exomem.tui.theme import GLYPHS_UNICODE

    def factory(backend: FakeBackend | None = None) -> ExomemTuiApp:
        return ExomemTuiApp(backend if backend is not None else fake_backend, glyphs=GLYPHS_UNICODE)

    return factory
