"""The drain record is the one file that the backup Job copies from the cell (add-cloud-media-store D12).

The cell writes it, `exomem cell-media-census` prints it, and the Job command that cellctl renders copies it to
line two of the termination message, where cellctl parses it. cellctl's command and parser are loaded by path from
its stdlib-only module, and the command runs under sh with this package as `exomem`, as the Job runs it.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exomem import media_drain_record

# The backup Job only ever runs under Linux sh; the command embeds POSIX paths.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="the backup Job runs under Linux sh")

BACKUP_SOURCE = Path(__file__).resolve().parents[1] / "infra" / "cellctl" / "src" / "cellctl" / "backup_source.py"
HOLD_START = "2026-11-01T01:00:00+00:00"
CENSUS_TIME = datetime(2026, 11, 1, 12, 30, 15, tzinfo=UTC)


def _backup_source():
    spec = importlib.util.spec_from_file_location("cellctl_backup_source", BACKUP_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _termination_message(vault: Path, tmp_path: Path) -> str:
    """Run the Job's census command on `vault` after line one of the message, and read the message back."""
    termination_log = tmp_path / "termination-log"
    termination_log.write_text("0123 1 2", encoding="utf-8")  # Line one: the snapshot and the use.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "exomem").write_text(f'#!/bin/sh\nexec "{sys.executable}" -m exomem "$@"\n', encoding="utf-8")
    (bin_dir / "exomem").chmod(0o755)
    script = _backup_source().media_census_command(HOLD_START)
    script = script.replace("/data/vault", str(vault)).replace("/dev/termination-log", str(termination_log))

    subprocess.run(["sh", "-c", script], check=True, env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})

    return termination_log.read_text(encoding="utf-8")


def test_the_cell_writes_the_line_that_cellctl_parses(vault: Path) -> None:
    """Catches a change to one side of the record format: cellctl would read every report as no census."""
    backup_source = _backup_source()
    media_drain_record.write(vault, 3, 4096, CENSUS_TIME)

    line = media_drain_record.read(vault)

    assert backup_source.parse_media_census(line) == (3, 4096, CENSUS_TIME)
    unknown = f"{backup_source.MEDIA_CENSUS_UNKNOWN} {HOLD_START}"
    assert backup_source.parse_media_census(unknown) == (None, None, datetime.fromisoformat(HOLD_START))


def test_the_backup_jobs_census_command_copies_the_cells_record_to_line_two(vault: Path, tmp_path: Path) -> None:
    """Catches a Job that names no census the cell image has, or reports the hold start as the census time."""
    media_drain_record.write(vault, 3, 4096, CENSUS_TIME)

    assert _termination_message(vault, tmp_path) == f"0123 1 2\n3 4096 {CENSUS_TIME.isoformat()}"


@pytest.mark.parametrize("shape", ["missing", "link", "oversize", "malformed", "no zone"])
def test_a_record_the_job_cannot_trust_reports_unknown_at_the_hold_start(vault: Path, tmp_path: Path, shape) -> None:
    """Catches a reader that follows a link or reads an unbounded file on the Job's volume, or passes a malformed line on."""
    record = media_drain_record.record_path(vault)
    record.parent.mkdir(parents=True, exist_ok=True)
    line = "0 0 2026-11-01T12:30:15+00:00\n"
    if shape == "link":
        (tmp_path / "elsewhere").write_text(line, encoding="utf-8")
        record.symlink_to(tmp_path / "elsewhere")
    elif shape != "missing":
        record.write_text({"oversize": line + " " * 200, "malformed": "none 0 2026-11-01T12:30:15+00:00\n",
                           "no zone": "0 0 2026-11-01T12:30:15\n"}[shape], encoding="utf-8")

    assert _termination_message(vault, tmp_path) == f"0123 1 2\nunknown unknown {HOLD_START}"


def test_the_census_command_starts_without_the_server_framework(vault: Path) -> None:
    """Catches a census command that loads the whole CLI: on the Job's one CPU it outlasts the Job's bound."""
    media_drain_record.write(vault, 3, 4096, CENSUS_TIME)
    probe = ("import sys; from exomem.__main__ import main; "
             "print(main(['cell-media-census', '--vault', sys.argv[1]])); print('fastmcp' in sys.modules)")

    run = subprocess.run([sys.executable, "-c", probe, str(vault)], check=True, capture_output=True, text=True)

    assert run.stdout.splitlines() == [f"3 4096 {CENSUS_TIME.isoformat()}", "0", "False"]
