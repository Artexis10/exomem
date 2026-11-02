"""The drain record: the last complete census of a draining cell's evicted originals (D12).

The cell writes one line beside its media ledger, `<count> <bytes> <census time>`. The nightly backup Job copies
that line into its termination message and cellctl reads it from there. The Job mounts the volume read-only, so
the reader opens one file and needs no store, lease or database. This module imports only the state paths.
"""

from __future__ import annotations

import os
import stat
import uuid
from datetime import datetime
from pathlib import Path

from . import state_paths

RECORD_NAME = ".media-drain-record"
# The line is two decimal counts of at most 19 digits and an ISO-8601 time; the cap rejects anything else.
MAX_RECORD_BYTES = 128
MAX_DIGITS = 19


class RecordUnreadable(Exception):
    """The record is missing or not in the closed format; the message names the reason, never the content."""


def record_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(Path(vault_root)) / RECORD_NAME


def format_line(evicted: int, evicted_bytes: int, captured_at: datetime) -> str:
    return f"{evicted} {evicted_bytes} {captured_at.isoformat(timespec='seconds')}"


def parse_line(line: str) -> tuple[int, int, datetime]:
    parts = line.split(" ")
    if len(parts) != 3 or not all(n.isascii() and n.isdecimal() and len(n) <= MAX_DIGITS for n in parts[:2]):
        raise RecordUnreadable("the drain record is not in its format")
    try:
        captured_at = datetime.fromisoformat(parts[2])
    except ValueError:
        raise RecordUnreadable("the drain record has no census time") from None
    if captured_at.tzinfo is None:
        raise RecordUnreadable("the drain record has no time zone")
    return int(parts[0]), int(parts[1]), captured_at


def write(vault_root: Path, evicted: int, evicted_bytes: int, captured_at: datetime) -> None:
    """Replace the record: a durable temporary file, then a rename, so a reader sees the old line or the new."""
    path = record_path(vault_root)
    temporary = path.with_name(f"{RECORD_NAME}.{uuid.uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(format_line(evicted, evicted_bytes, captured_at) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def remove(vault_root: Path) -> None:
    record_path(vault_root).unlink(missing_ok=True)


def read(vault_root: Path) -> str:
    """The record line as written; raises `RecordUnreadable` for a missing, linked, oversize or malformed record."""
    try:
        # O_NOFOLLOW refuses a link, and O_NONBLOCK keeps a pipe from holding the Job; fstat then wants a regular file.
        descriptor = os.open(record_path(vault_root), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise RecordUnreadable("the drain record cannot be opened") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RecordUnreadable("the drain record is not a regular file")
        raw = os.read(descriptor, MAX_RECORD_BYTES)  # A longer file is cut here, which leaves it outside the format.
    except OSError as error:
        raise RecordUnreadable("the drain record cannot be read") from error
    finally:
        os.close(descriptor)
    try:
        line = raw.decode("ascii").removesuffix("\n")
    except UnicodeError:
        raise RecordUnreadable("the drain record is not ASCII") from None
    parse_line(line)
    return line
