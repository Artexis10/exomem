"""`exomem tui` dispatch: TTY gate, soft-fail install hint, lazy TUI import.

These tests run in the lean matrix — none of them import textual. The launch
path itself is exercised by `tests/tui/` (skipped when the extra is absent).
"""

from __future__ import annotations

import pytest

from exomem import __main__ as main_module
from exomem.__main__ import _CLI_ONLY_SUBCOMMANDS, main


def test_tui_is_registered_as_cli_only():
    assert "tui" in _CLI_ONLY_SUBCOMMANDS


def test_non_tty_exits_2_with_one_line(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(main_module, "_tui_stdio_is_tty", lambda: False)
    rc = main(["tui"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "interactive terminal" in err
    assert err.strip().count("\n") == 0


def test_missing_extra_prints_install_hint(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(main_module, "_tui_stdio_is_tty", lambda: True)
    monkeypatch.setattr(
        main_module, "_module_available", lambda name: name != "textual"
    )
    rc = main(["tui"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "exomem[tui]" in err
    assert "--extra tui" in err
    assert "Traceback" not in err


def test_tty_with_extra_launches_lazily(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main_module, "_tui_stdio_is_tty", lambda: True)
    monkeypatch.setattr(main_module, "_module_available", lambda name: True)
    calls: dict[str, object] = {}

    import exomem.tui as tui_pkg

    def fake_run(*, vault=None, mouse=True):
        calls["vault"] = vault
        calls["mouse"] = mouse
        return 0

    monkeypatch.setattr(tui_pkg, "run", fake_run)
    rc = main(["tui", "--vault", "/tmp/some-vault"])
    assert rc == 0
    assert calls["vault"] == "/tmp/some-vault"
    assert calls["mouse"] is True

    rc = main(["tui", "--no-mouse"])
    assert rc == 0
    assert calls["mouse"] is False, (
        "--no-mouse hands click-drag selection back to the terminal"
    )


def test_importing_tui_package_does_not_import_textual():
    # Probed in a subprocess: the pytest-textual-snapshot plugin imports
    # textual into THIS process at session start, so sys.modules here is
    # contaminated by design.
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import exomem.tui; sys.exit(1 if 'textual' in sys.modules else 0)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
