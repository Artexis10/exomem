from __future__ import annotations

import socket
from pathlib import Path

import pytest


def test_report_refuses_non_terminal_manifest_and_offline_blocks_sockets(tmp_path: Path) -> None:
    from lme.report import render_run_report
    from protocol.manifest import start_manifest

    start_manifest(
        tmp_path,
        run_id="started",
        dataset={"id": "fixture", "variant": "mini", "source": "local", "revision": "1", "sha256": "a" * 64, "case_count": 0},
        started_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(ValueError, match="non-terminal"):
        render_run_report(tmp_path, offline=True)
    with pytest.raises(OSError, match="offline"):
        with render_run_report.offline_guard():
            socket.socket.connect(object(), ("127.0.0.1", 9))
