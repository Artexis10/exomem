"""One-line record files that the cell writes into its state directory and the backup Job copies out (D12).

A record is ASCII text of a bounded length. The writer replaces it atomically, so a reader sees the old record or the
new one. The reader runs in the backup Job, which mounts the volume read-only: it refuses a link, a pipe and an
oversize file, and needs no store, lease or database. This module imports only the standard library.
"""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path


class RecordUnreadable(Exception):
    """The record is missing or not in its closed format; the message names the reason, never the content."""


def write_atomic(path: Path, text: str) -> None:
    """Replace the file: a durable temporary file, then a rename, then a flush of the directory."""
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(text + "\n")
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


def read_line(path: Path, *, max_bytes: int, name: str) -> str:
    """The record's text without its newline; `name` words the refusal ("the <name> cannot be opened").

    Raises `RecordUnreadable` for a missing, linked, oversize, non-ASCII or multi-line file.
    """
    try:
        # O_NOFOLLOW refuses a link, and O_NONBLOCK keeps a pipe from holding the Job; fstat then wants a regular file.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise RecordUnreadable(f"the {name} cannot be opened") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RecordUnreadable(f"the {name} is not a regular file")
        raw = os.read(descriptor, max_bytes + 1)  # One byte more than the bound shows a file that is longer.
    except OSError as error:
        raise RecordUnreadable(f"the {name} cannot be read") from error
    finally:
        os.close(descriptor)
    if len(raw) > max_bytes:
        raise RecordUnreadable(f"the {name} is longer than {max_bytes} bytes")
    try:
        text = raw.decode("ascii").removesuffix("\n")
    except UnicodeError:
        raise RecordUnreadable(f"the {name} is not ASCII") from None
    if "\n" in text:  # The Job copies the record as one line of its termination message, which a second line would shift.
        raise RecordUnreadable(f"the {name} has more than one line")
    return text
