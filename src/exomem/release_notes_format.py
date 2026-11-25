"""The release notes file: its one definition, for the release build that writes it and the cell that reads it.

The release build writes `user_release_notes.json` (`scripts/user_notes.py generate`) and the cell
image serves it (`release_notes`). Both import this module, so a change to a key, the version shape or
the date shape reaches the writer and the reader together. The release build loads it by file path
without installing the package, so it imports the standard library only.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

SCHEMA = 1
#: The shape of a version in the file and of a release tag after its `v`: `X.Y.Z`, a field format, not a reading of text.
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")

Version = tuple[int, int, int]


@dataclass(frozen=True)
class Release:
    """One release that has notes. `released_on` is a `YYYY-MM-DD` date and `notes` are newest first."""

    version: str
    parsed: Version
    released_on: str
    notes: tuple[str, ...]


def parse_version(text: object) -> Version | None:
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


def version_text(version: Version) -> str:
    return ".".join(map(str, version))


def document(releases: Iterable[Release]) -> dict:
    """The file's content for `releases`, in the order given."""
    return {
        "schema": SCHEMA,
        "releases": [
            {"version": release.version, "released_on": release.released_on, "notes": list(release.notes)}
            for release in releases
        ],
    }


def _release(entry: object) -> Release:
    if not isinstance(entry, dict):
        raise ValueError("a release is not an object")
    version, released_on, notes = entry.get("version"), entry.get("released_on"), entry.get("notes")
    parsed = parse_version(version)
    if parsed is None:
        raise ValueError(f"release version {version!r} is not X.Y.Z")
    try:
        canonical = isinstance(released_on, str) and datetime.date.fromisoformat(released_on).isoformat() == released_on
    except ValueError:
        canonical = False
    if not canonical:
        raise ValueError(f"release {version} has released_on {released_on!r}, not YYYY-MM-DD")
    if not isinstance(notes, list) or not notes or not all(isinstance(note, str) and note.strip() for note in notes):
        raise ValueError(f"release {version} has no notes, or a note that is not text")
    return Release(version, parsed, released_on, tuple(notes))


def parse_document(text: str) -> tuple[Release, ...]:
    """The releases in a notes file, newest first. Raises `ValueError` for any content the format does not allow.

    All or nothing: a reader that served the releases it could understand would show people a partial
    list as the whole.
    """
    content = json.loads(text)
    if not isinstance(content, dict) or content.get("schema") != SCHEMA or not isinstance(content.get("releases"), list):
        raise ValueError(f"not a schema {SCHEMA} notes file")
    releases = [_release(entry) for entry in content["releases"]]
    return tuple(sorted(releases, key=lambda release: release.parsed, reverse=True))
