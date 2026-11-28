"""A Google Photos Takeout sidecar: the JSON object beside a photo that holds its caption.

Which sidecar is which, and how Takeout names it, is registry data (`source_formats`, `export_pairing`). This module
holds the container (one JSON object of at most 1 MiB, read whole) and the caption that a sidecar carries. A sidecar
derives no files: filing reads it once (`export_sidecars`), then gives its photo the caption and Google's timestamps.
The timestamps record provenance. Dating a photo from them is held until a measurement says where they come from.

The sidecar is untrusted content: a reader keeps its values as data, never opens the `url` it carries, and never sends
it to a model. No vendor documents the format. The reader follows what Takeout wrote in 2026 and treats every field but
the entry's `item_keys` as optional, so a missing or odd field degrades the caption and never fails the sidecar.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from .source_formats import SourceFormat

#: A larger file is not opened: a sidecar holds about a kilobyte, so a bigger one is not a sidecar.
MAX_BYTES = 1024 * 1024
#: The caption kept on a photo's page; a longer one is cut, so a sidecar of a megabyte cannot make a page of that size.
MAX_CAPTION_CHARS = 4096
#: The C0 controls but tab and newline, and the C1 controls: a caption keeps its text and no control character.
_NO_CONTROLS = {code: None for code in (*range(0x20), *range(0x7F, 0xA0)) if code not in (0x09, 0x0A)}
# nosemgrep: ep-word-set -- Google Photos' fixed JSON timestamp fields.
_TIME_FIELDS = ("photoTakenTime", "creationTime")


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


def caption_of(document: dict) -> str | None:
    """The owner's `description` of a recognised sidecar, trimmed and cut to `MAX_CAPTION_CHARS`; None when it holds no text."""
    value = document.get("description")
    if not isinstance(value, str):
        return None
    return value.translate(_NO_CONTROLS).strip()[:MAX_CAPTION_CHARS].strip() or None


def _timestamp(value: object) -> str | None:
    """Keep Google's epoch-second text within the digit width of a four-digit calendar year."""
    if isinstance(value, str) and len(value) <= 13 and value.isascii():
        digits = value.removeprefix("-")
        if len(digits) <= 12 and digits.isdecimal():
            return value
    return None


def timestamps_of(document: dict) -> dict[str, str]:
    """Keep the original timestamp string of each field that supplies one."""
    timestamps = {}
    for field in _TIME_FIELDS:
        entry = document.get(field)
        if isinstance(entry, dict) and (value := _timestamp(entry.get("timestamp"))) is not None:
            timestamps[field] = value
    return timestamps


def provenance_lines(timestamps: Mapping[str, str]) -> list[str]:
    """Label the vendor and field alongside each timestamp without interpreting it as a date."""
    return [
        f"- Google Photos `{field}`: `{value}`"
        for field in _TIME_FIELDS
        if (value := _timestamp(timestamps.get(field))) is not None
    ]


def timestamp_pair(timestamps: Mapping[str, str]) -> tuple[str, str] | None:
    """Both vendor timestamps, required to recognise the same photo in an album."""
    taken, created = (timestamps.get(field) for field in _TIME_FIELDS)
    return (taken, created) if taken and created else None
