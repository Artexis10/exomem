"""Exomem's interactive terminal UI (the optional `tui` extra).

This package import must stay light: `__main__` imports it only after the TTY
and extra-availability guards pass, and the Textual stack itself loads lazily
inside `run` so probing the package never pays the UI import cost.
"""

from __future__ import annotations


def run(*, vault: str | None = None, mouse: bool = True) -> int:
    """Launch the TUI application; returns a process exit code."""
    from .app import run_tui

    return run_tui(vault=vault, mouse=mouse)
