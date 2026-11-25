"""The storage record: the cell's two storage figures for the backup report (add-cloud-media-store D10, task 4.9).

A Cloud cell writes one JSON line beside its media ledger: the bytes that its volume holds without cached copies of
stored originals (vault storage), the bytes of its stored originals each counted once (media storage), the time of
the reading, and the time of the owner census behind the media figure. A figure that the cell cannot read is null,
which is unknown and never 0; the status names the reason. The media figure is the census that offload recorded, which
can be older than the reading, so the record dates it apart: cellctl publishes the figures as of the older of the two.
The nightly backup Job copies the line into its termination message and cellctl publishes the figures to Home. The Job
mounts the volume read-only, so the reader opens one file and needs no store, lease or database.

The readers import only the state paths and `state_record_file`, so the Job's command starts in seconds.
`refresh` is the cell's writer and imports the status code when it runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import state_paths
from .state_record_file import RecordUnreadable, read_line, write_atomic

RECORD_NAME = ".media-storage-record"
# The line is a JSON object of two figures of at most 19 digits and two ISO-8601 times; the cap rejects anything else.
MAX_RECORD_BYTES = 256
# Substrate holds each figure in a bigint column.
MAX_FIGURE = 2**63 - 1
# The worker's timer ticks every minute, and a status already reads the same figures on demand, so the record needs
# no tighter refresh than this. The backup Job and cellctl take the record as it stands, and cellctl treats one that
# has aged past its own bound as unknown.
REFRESH_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class StorageRecord:
    """The two figures and when they were read; a figure is None when the cell could not read it.

    `media_observed_at` is the time of the owner census that `media_used_bytes` comes from: None where that figure is.
    """

    captured_at: datetime
    vault_bytes: int | None
    media_used_bytes: int | None
    media_observed_at: datetime | None


def record_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(Path(vault_root)) / RECORD_NAME


def format_line(record: StorageRecord) -> str:
    census = record.media_observed_at
    return json.dumps(
        {"captured_at": record.captured_at.isoformat(timespec="seconds"), "vault_bytes": record.vault_bytes,
         "media_used_bytes": record.media_used_bytes,
         "media_observed_at": None if census is None else census.isoformat(timespec="seconds")},
        separators=(",", ":"))


def parse_line(line: str) -> StorageRecord:
    """The record that a line holds; raises `RecordUnreadable` for a line outside the closed format."""
    try:
        fields = json.loads(line)
    except (ValueError, RecursionError):
        raise RecordUnreadable("the storage record is not JSON") from None
    if type(fields) is not dict:
        raise RecordUnreadable("the storage record is not an object")
    try:
        captured_at = datetime.fromisoformat(fields["captured_at"])
        vault_bytes, media_used_bytes = fields["vault_bytes"], fields["media_used_bytes"]
        census = fields["media_observed_at"]
        media_observed_at = None if census is None else datetime.fromisoformat(census)
    except (KeyError, TypeError, ValueError):
        raise RecordUnreadable("the storage record has no reading time, census time or figure") from None
    if captured_at.tzinfo is None or (media_observed_at is not None and media_observed_at.tzinfo is None):
        raise RecordUnreadable("the storage record has a time without a time zone")
    for figure in (vault_bytes, media_used_bytes):
        if figure is not None and (type(figure) is not int or not 0 <= figure <= MAX_FIGURE):
            raise RecordUnreadable("the storage record holds a figure that is no byte count")
    return StorageRecord(captured_at, vault_bytes, media_used_bytes, media_observed_at)


def write(vault_root: Path, record: StorageRecord) -> None:
    """Replace the record: a durable temporary file, then a rename, so a reader sees the old line or the new."""
    write_atomic(record_path(vault_root), format_line(record))


def remove(vault_root: Path) -> None:
    record_path(vault_root).unlink(missing_ok=True)


def read(vault_root: Path) -> str:
    """The record line as written, for the Job's termination message; raises `RecordUnreadable` for a missing, linked,
    oversize or malformed record."""
    line = read_line(record_path(vault_root), max_bytes=MAX_RECORD_BYTES, name="storage record")
    parse_line(line)
    return line


def read_record(vault_root: Path) -> StorageRecord:
    return parse_line(read(vault_root))


def refresh(vault_root: Path, *, now: datetime | None = None) -> bool:
    """Write the record when it is due; True when it wrote one.

    The figures are the status's own (`import_windows.storage_figures`), so the record and the status never differ,
    and nothing is counted afresh: the owner figure is the census that offload recorded, dated by that census. A
    census whose time cannot be read leaves the media figure null, since nothing could date it. Only a served Cloud cell
    writes one: a store that is off, or a personal vault, has none for the Job to copy, and a record left from before
    a turn-off is removed. A policy that cannot be read leaves the record to age out, and cellctl reads it as unknown.
    """
    from . import import_windows, originals

    vault = Path(vault_root)
    if originals.uses_local_configuration():
        return False
    try:
        config = originals.configuration(vault)
    except (OSError, ValueError):
        return False
    if config is None or config.get("enabled") is not True:
        remove(vault)
        return False
    now = now or datetime.now(UTC)
    try:
        written = read_record(vault)
    except RecordUnreadable:
        pass
    else:
        if 0 <= (now - written.captured_at).total_seconds() < REFRESH_SECONDS:
            return False
    figures, census_time = import_windows.storage_figures(vault, config)
    media_used_bytes = figures["media_storage"].get("used_bytes")
    media_observed_at = None if media_used_bytes is None else _census_time(census_time)
    if media_observed_at is None:
        media_used_bytes = None
    write(vault, StorageRecord(now, figures["vault_storage"].get("bytes"), media_used_bytes, media_observed_at))
    return True


def _census_time(text: str | None) -> datetime | None:
    """The census time that `text` holds; None when it is missing, unreadable or has no time zone."""
    try:
        parsed = None if text is None else datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None
