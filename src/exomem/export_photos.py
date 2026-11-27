"""A Google Photos Takeout sidecar: the JSON object beside a photo that holds its capture time and its caption.

Which sidecar is which, and how Takeout names it, is registry data (`source_formats`, `export_pairing`). This module
holds the container (one JSON object of at most 1 MiB, read whole) and the facts that a sidecar carries. A sidecar
derives no files: filing reads it once (`export_sidecars`), then gives its photo the time and the caption.

The sidecar is untrusted content: a reader keeps its values as data, never opens the `url` it carries, and never sends
it to a model. No vendor documents the format. The reader follows what Takeout wrote in 2026 and treats every field but
the entry's `item_keys` as optional, so a missing or odd field degrades the facts and never fails the sidecar.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO

if TYPE_CHECKING:
    from .source_formats import SourceFormat

#: A larger file is not opened: a sidecar holds about a kilobyte, so a bigger one is not a sidecar.
MAX_BYTES = 1024 * 1024
#: The caption kept on a photo's page; a longer one is cut, so a sidecar of a megabyte cannot make a page of that size.
MAX_CAPTION_CHARS = 4096
#: A file name never passes this many characters, so a longer title names no file.
_MAX_TITLE_CHARS = 255
_MAX_DIGITS = 12
#: The last second that a four-digit year holds (9999-12-31T23:59:59Z).
_LAST_SECOND = 253_402_300_799
#: The C0 controls but tab and newline, and the C1 controls: a caption keeps its text and no control character.
_NO_CONTROLS = {code: None for code in (*range(0x20), *range(0x7F, 0xA0)) if code not in (0x09, 0x0A)}


@dataclass(frozen=True, slots=True)
class Facts:
    """What one sidecar states: the name of its photo, the time and the caption."""

    title: str | None
    time: str | None  # `photoTakenTime` in UTC as `YYYY-MM-DDTHH:MM:SSZ`; None when it is missing, unusable or an upload time
    time_skipped: bool  # `photoTakenTime` equals `creationTime` to the second: the one shape known to be an upload time
    caption: str | None  # the owner's `description`, trimmed; None when it holds no text


def read_object(stream: BinaryIO) -> dict | None:
    """The JSON object that the stream holds, or None when it is larger than `MAX_BYTES` or is not one; an `OSError` propagates."""
    data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return None
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):  # a ValueError covers bad JSON and bytes that are not text
        return None
    return value if isinstance(value, dict) else None


def recognise(stream: BinaryIO, fmt: SourceFormat) -> bool:
    """Whether the stream is a JSON object of at most `MAX_BYTES` that carries every key of the entry's `item_keys`."""
    found = read_object(stream)
    return found is not None and all(key in found for key in fmt.item_keys)


def facts_of(document: dict) -> Facts:
    """The facts of a recognised sidecar."""
    title = document.get("title")
    taken, created = _epoch(document.get("photoTakenTime")), _epoch(document.get("creationTime"))
    skipped = taken is not None and taken == created
    return Facts(
        title=title if isinstance(title, str) and 0 < len(title) <= _MAX_TITLE_CHARS else None,
        time=None if taken is None or skipped else _utc(taken), time_skipped=skipped, caption=_caption(document.get("description")))


def _epoch(stamp: Any) -> int | None:
    """The whole seconds of a `{"timestamp": "<epoch seconds>"}` object, or None."""
    value = stamp.get("timestamp") if isinstance(stamp, dict) else None
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= _MAX_DIGITS:
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _LAST_SECOND:
        return None
    return value


def _utc(seconds: int) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _caption(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.translate(_NO_CONTROLS).strip()[:MAX_CAPTION_CHARS].strip() or None
