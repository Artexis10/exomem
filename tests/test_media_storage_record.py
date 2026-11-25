"""The storage record carries the cell's two storage figures to the backup report (add-cloud-media-store D10, task 4.9).

The cell writes it on its worker tick from the figures that its status already reads, `exomem cell-media-storage`
prints it, and the Job command that cellctl renders copies it to line three of the termination message, where
cellctl parses it. cellctl's command and parser are loaded by path from its stdlib-only module, and the command
runs under sh with this package as `exomem`, as the Job runs it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_media_store import _WINDOW_VOLUME, _stored, _volume, _window_policy, _window_status
from test_media_store import selected_store as selected_store  # noqa: F401 - the fixture

from exomem import media_storage_record, media_store, state_record_file

BACKUP_SOURCE = Path(__file__).resolve().parents[1] / "infra" / "cellctl" / "src" / "cellctl" / "backup_source.py"
HOLD_START = "2026-11-01T01:00:00+00:00"
READ_AT = datetime(2026, 11, 1, 12, 30, 15, tzinfo=UTC)


def _backup_source():
    spec = importlib.util.spec_from_file_location("cellctl_backup_source", BACKUP_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _termination_message(vault: Path, tmp_path: Path) -> str:
    """Run the Job's storage command on `vault` after the use and census lines, and read the message back."""
    termination_log = tmp_path / "termination-log"
    termination_log.write_text(f"0123 1 2\n3 4096 {HOLD_START}", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "exomem").write_text(f'#!/bin/sh\nexec "{sys.executable}" -m exomem "$@"\n', encoding="utf-8")
    (bin_dir / "exomem").chmod(0o755)
    script = _backup_source().media_storage_command(HOLD_START)
    script = script.replace("/data/vault", str(vault)).replace("/dev/termination-log", str(termination_log))

    subprocess.run(["sh", "-c", script], check=True, env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})

    return termination_log.read_text(encoding="utf-8")


def _unknown_line() -> str:
    return json.dumps({"captured_at": HOLD_START, "vault_bytes": None, "media_used_bytes": None}, separators=(",", ":"))


@pytest.mark.skipif(os.name == "nt", reason="the backup Job runs under Linux sh")
class TestBackupJobReport:
    def test_the_cell_writes_the_line_that_cellctl_parses(self, vault: Path) -> None:
        """Catches a change to one side of the record format: cellctl would read every report as no figures."""
        backup_source = _backup_source()
        media_storage_record.write(vault, media_storage_record.StorageRecord(READ_AT, 9_000, 5_000))
        media_storage_record.write(vault, media_storage_record.StorageRecord(READ_AT, None, 5_000))

        line = media_storage_record.read(vault)

        assert backup_source.parse_media_storage(line) == (None, 5_000, READ_AT)  # An unknown figure stays unknown.
        assert backup_source.parse_media_storage(_unknown_line()) == (None, None, datetime.fromisoformat(HOLD_START))

    def test_the_backup_jobs_command_copies_the_cells_record_to_line_three(self, vault: Path, tmp_path: Path) -> None:
        """Catches a Job that names no command the cell image has, or reports the hold start as the reading time."""
        media_storage_record.write(vault, media_storage_record.StorageRecord(READ_AT, 9_000, 5_000))

        message = _termination_message(vault, tmp_path)

        assert message.split("\n")[:2] == ["0123 1 2", f"3 4096 {HOLD_START}"]
        assert _backup_source().parse_media_storage(message.split("\n")[2]) == (9_000, 5_000, READ_AT)

    @pytest.mark.parametrize("shape", ["missing", "link", "oversize", "malformed", "no zone", "negative"])
    def test_a_record_the_job_cannot_trust_reports_unknown_at_the_hold_start(
            self, vault: Path, tmp_path: Path, shape) -> None:
        """Catches a reader that follows a link or reads an unbounded file on the Job's volume, or passes on a
        record whose figure is no byte count."""
        record = media_storage_record.record_path(vault)
        record.parent.mkdir(parents=True, exist_ok=True)
        line = '{"captured_at":"2026-11-01T12:30:15+00:00","vault_bytes":1,"media_used_bytes":2}\n'
        if shape == "link":
            (tmp_path / "elsewhere").write_text(line, encoding="utf-8")
            record.symlink_to(tmp_path / "elsewhere")
        elif shape != "missing":
            record.write_text({"oversize": line + " " * 300, "malformed": "none\n",
                               "no zone": line.replace("+00:00", ""),
                               "negative": line.replace('"vault_bytes":1', '"vault_bytes":-1')}[shape],
                              encoding="utf-8")

        message = _termination_message(vault, tmp_path)

        assert message.split("\n")[2] == _unknown_line()

    def test_the_storage_command_starts_without_the_server_framework(self, vault: Path) -> None:
        """Catches a command that loads the whole CLI: on the Job's one CPU it outlasts the Job's bound."""
        media_storage_record.write(vault, media_storage_record.StorageRecord(READ_AT, 9_000, 5_000))
        probe = ("import sys; from exomem.__main__ import main; "
                 "print(main(['cell-media-storage', '--vault', sys.argv[1]])); print('fastmcp' in sys.modules)")

        run = subprocess.run([sys.executable, "-c", probe, str(vault)], check=True, capture_output=True, text=True)

        assert run.stdout.splitlines() == [media_storage_record.format_line(
            media_storage_record.StorageRecord(READ_AT, 9_000, 5_000)), "0", "False"]


def _record(vault: Path):
    return media_storage_record.read_record(vault)


def test_the_record_equals_the_status_figures_without_counting_a_cached_original_twice(
        vault: Path, selected_store, monkeypatch):
    """Catches a record that differs from the status Home's owner reads, or that charges the volume for cached
    copies, or that leaves them out of the media figure."""
    from types import SimpleNamespace

    _window_policy(selected_store, monkeypatch)
    cached = _stored(vault, "cached", 300)
    evicted = _stored(vault, "evicted", 500)
    assert media_store.evict(vault, evicted)
    used = 10_000 + cached.stat().st_blocks * 512
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(
        total=_WINDOW_VOLUME, used=used, free=_WINDOW_VOLUME - used))
    media_store.take_census(vault)  # The census that the worker's offload batch records.

    assert media_storage_record.refresh(vault, now=READ_AT)

    status = _window_status(vault, series_id="series-a", next_window_bytes=1)
    written = _record(vault)
    assert written.captured_at == READ_AT
    assert (written.vault_bytes, written.media_used_bytes) == (
        status["vault_storage"]["bytes"], status["media_storage"]["used_bytes"])
    assert written.vault_bytes == 10_000 and written.media_used_bytes > cached.stat().st_size


def test_a_figure_the_cell_cannot_read_is_null_in_the_record_and_never_zero(vault: Path, selected_store, monkeypatch):
    """Catches an unreadable census or volume that the record writes as 0 bytes."""
    _window_policy(selected_store, monkeypatch)
    _stored(vault, "cached", 300)
    monkeypatch.setattr(shutil, "disk_usage", _volume(_WINDOW_VOLUME, 0))  # Below the cached copy: unknown.
    monkeypatch.setattr(media_store, "recorded_census", lambda _vault: None)  # No census is recorded yet.

    assert media_storage_record.refresh(vault, now=READ_AT)

    written = _record(vault)
    assert (written.vault_bytes, written.media_used_bytes) == (None, None)


def test_the_tick_writes_the_record_every_quarter_hour_and_counts_nothing_afresh(
        vault: Path, selected_store, monkeypatch):
    """Catches a refresh on each tick, or one that walks the vault for a census: offload held the writer 16 minutes."""
    _window_policy(selected_store, monkeypatch)
    _stored(vault, "cached", 300)
    monkeypatch.setattr(media_store, "take_census", lambda _vault: pytest.fail("the timer took a census"))

    assert media_storage_record.refresh(vault, now=READ_AT)
    written = media_storage_record.record_path(vault).read_bytes()
    interval = timedelta(seconds=media_storage_record.REFRESH_SECONDS)

    assert not media_storage_record.refresh(vault, now=READ_AT + interval - timedelta(seconds=1))
    assert media_storage_record.record_path(vault).read_bytes() == written
    assert media_storage_record.refresh(vault, now=READ_AT + interval)
    assert _record(vault).captured_at == READ_AT + interval


def test_a_store_that_is_off_leaves_no_record(vault: Path, selected_store):
    """Catches a record left from before the owner turned the store off: Home would keep showing its figures."""
    values, config = selected_store
    media_storage_record.write(vault, media_storage_record.StorageRecord(READ_AT, 9_000, 5_000))
    config.write_text(json.dumps({"vaults": {str(vault): {**values, "enabled": False}}}))

    assert not media_storage_record.refresh(vault, now=READ_AT)

    assert not media_storage_record.record_path(vault).exists()


def test_a_personal_vault_writes_no_record(vault: Path):
    """Catches a record in a vault with no media store, which the backup Job would copy as a cell's figures."""
    assert not media_storage_record.refresh(vault, now=READ_AT)

    assert not media_storage_record.record_path(vault).exists()


def test_a_failed_write_leaves_the_earlier_record_and_no_temporary_file(vault: Path, monkeypatch):
    """Catches a record written in place: the backup Job copying it mid-write reads half a line."""
    media_storage_record.write(vault, media_storage_record.StorageRecord(READ_AT, 9_000, 5_000))
    before = media_storage_record.record_path(vault).read_bytes()
    monkeypatch.setattr(state_record_file.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("full")))

    with pytest.raises(OSError):
        media_storage_record.write(vault, media_storage_record.StorageRecord(READ_AT, 1, 2))

    assert media_storage_record.record_path(vault).read_bytes() == before
    assert [path.name for path in media_storage_record.record_path(vault).parent.iterdir()
            if path.name.startswith(media_storage_record.RECORD_NAME + ".")] == []
