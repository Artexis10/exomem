"""What changed in this release, for the person who uses the cell.

The release build writes `user_release_notes.json` beside this module (`scripts/user_notes.py
generate`): the notes of the 20 newest releases that have any, newest first. An image without the
file serves an empty list and never fails. Two things read it:

- `payload` is the `bootstrap(section="whats_new")` answer that Home shows, under the key `whats_new`.
- `update_notice` is the one-time `update_notice` result field that tells a connected assistant,
  once, that a release with notes arrived since it last heard (Cloud cells only).

Everything here comes from the release build and the cell's own state. It reads no vault content.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from importlib import resources
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

_log = logging.getLogger(__name__)

NOTES_FILE = "user_release_notes.json"
SCHEMA = 1
STATE_FILENAME = "app-update-notices.json"
SAY_PREFIX = "Tell the user once in this conversation: "
#: The most apps the cell remembers. An app that falls off the list is a new app on its next call,
#: which records the version and tells it nothing.
MAX_APPS = 500
_LOCK_TIMEOUT_SECONDS = 2.0
#: What the notes document and the release tags spell: `X.Y.Z`, the shape of a version field, not a reading of text.
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")

Version = tuple[int, int, int]


@dataclass(frozen=True)
class Release:
    version: str
    parsed: Version
    released_on: str
    notes: tuple[str, ...]


def parse_version(text: object) -> Version | None:
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


@functools.cache
def running_version() -> str:
    """The version this process runs; `0+unknown` when no release is installed, as `bootstrap` reports it."""
    from . import install_info

    try:
        return install_info.installed_version()
    except PackageNotFoundError:
        return "0+unknown"


def _read_text() -> str:
    return resources.files("exomem").joinpath(NOTES_FILE).read_text(encoding="utf-8")


def _release(entry: object) -> Release | None:
    if not isinstance(entry, dict):
        return None
    version, released_on, notes = entry.get("version"), entry.get("released_on"), entry.get("notes")
    parsed = parse_version(version)
    if parsed is None or not isinstance(released_on, str):
        return None
    if not isinstance(notes, list) or not notes or not all(isinstance(n, str) and n.strip() for n in notes):
        return None
    return Release(version, parsed, released_on, tuple(notes))


@functools.cache
def releases() -> tuple[Release, ...]:
    """The releases that have notes, newest first. Read once per process.

    A missing file is the normal state of an image built without notes. A file that is there but
    cannot be used serves nothing, and the log says so once: serving half of it would show people
    a partial list as the whole.
    """
    from .log_events import log_event

    try:
        text = _read_text()
    except FileNotFoundError:
        return ()
    except OSError:
        log_event(_log, logging.WARNING, "release_notes_unreadable")
        return ()
    try:
        document = json.loads(text)
        found = [_release(entry) for entry in document["releases"]] if document["schema"] == SCHEMA else [None]
    except (ValueError, KeyError, TypeError):
        found = [None]
    if None in found:
        log_event(_log, logging.WARNING, "release_notes_unreadable")
        return ()
    return tuple(sorted(found, key=lambda release: release.parsed, reverse=True))


def payload() -> dict[str, Any]:
    """`{"whats_new": {version, releases}}`: the running version and the notes of each recent release."""
    return {
        "whats_new": {
            "version": running_version(),
            "releases": [
                {"version": release.version, "released_on": release.released_on, "notes": list(release.notes)}
                for release in releases()
            ],
        }
    }


# --------------------------------------------------------------------------
# The one-time notice for a connected app
# --------------------------------------------------------------------------

#: Per vault root, the version each app was last told about, as this process last read or wrote the
#: state file. It makes the steady state, where an app is up to date, cost no file read.
_told_memo: dict[str, dict[str, str]] = {}
_state_failure_logged = False


def _read_told(path: Path) -> dict[str, str]:
    """The saved `{app id: version}` map. A file that is missing or unusable reads as empty: every app
    is then new, which records the version and tells nothing, so a damaged file never repeats a notice."""
    try:
        apps = json.loads(path.read_text(encoding="utf-8"))["apps"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(apps, dict):
        return {}
    return {app: version for app, version in apps.items() if isinstance(app, str) and isinstance(version, str)}


def _write_told(path: Path, told: dict[str, str]) -> None:
    handle, staged = tempfile.mkstemp(prefix=f".{STATE_FILENAME}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"schema": SCHEMA, "apps": told}, stream)
        os.replace(staged, path)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise


def _note_state_failure() -> None:
    """Say without detail that the state file did not take the write: a warning once, debug after.

    The app is retried on its next call, so a state directory that stays unwritable would otherwise
    lose every notice unseen. A cataloged event: a cloud cell's log redacts any other message.
    """
    global _state_failure_logged
    from .log_events import log_event

    level = logging.DEBUG if _state_failure_logged else logging.WARNING
    _state_failure_logged = True
    log_event(_log, level, "app_update_state_unwritable")


def _record(vault_root: Path, app_id: str, running: Version, running_text: str) -> tuple[bool, str | None]:
    """Record `running_text` as the version `app_id` was told about: `(recorded, version before)`.

    Nothing is recorded when the app is already at the running version or ahead of it (a rollback
    keeps its record, so a later upgrade does not tell it the same news twice), or when the write
    fails. The read and the write share one lock, so two calls of one app, in this process or
    another, cannot both find it behind.
    """
    from . import state_paths

    key = os.fspath(vault_root)
    try:
        path = state_paths.ensure_vault_state_dir(vault_root) / STATE_FILENAME
        with FileLock(str(path.parent / f".{STATE_FILENAME}.lock"), timeout=_LOCK_TIMEOUT_SECONDS):
            told = _read_told(path)
            before = told.get(app_id)
            before_parsed = parse_version(before)
            if before_parsed is not None and before_parsed >= running:
                _told_memo[key] = told
                return False, before
            told.pop(app_id, None)
            told[app_id] = running_text  # the newest app last, so the oldest falls off first
            while len(told) > MAX_APPS:
                del told[next(iter(told))]
            _write_told(path, told)
            _told_memo[key] = told
            return True, before
    except (OSError, ValueError, Timeout):
        _note_state_failure()
        return False, None


def _say(name: str, notes: list[str]) -> str:
    """The sentence the assistant relays: the newest note, a count of the rest, and where the list is.

    The result holds at most `cloud_cell.SERVICE_NOTICE_MAX_SAY_CHARS` characters: a note that would
    overrun is cut, because the notes file's own 200-character limit is checked at review, not here.
    """
    from . import cloud_cell

    others = len(notes) - 1
    tail = (
        " You can read it under What's new on Home."
        if others == 0
        else f" {others} more {'change' if others == 1 else 'changes'}: see What's new on Home."
    )
    opening = f"{SAY_PREFIX}{name} was updated."
    room = cloud_cell.SERVICE_NOTICE_MAX_SAY_CHARS - len(opening) - len(tail) - 1
    note = notes[0] if len(notes[0]) <= room else notes[0][: room - 1].rstrip() + "…"
    return f"{opening} {note}{tail}"


def update_notice(vault_root: Path, app_id: str) -> dict[str, str] | None:
    """`{version, say}` the first time a release with notes reaches `app_id`, else None.

    An app's first call only records the running version. A later call records it first, then
    returns the notice when a release newer than the recorded one, up to the running one, has
    notes; when none does, the version moves silently. An app that is ahead of the running version
    (a rollback) keeps its record. A write that fails returns None: no notice, and the caller's
    result is unchanged.
    """
    from . import cloud_cell

    running = running_version()
    running_parsed = parse_version(running)
    if running_parsed is None:
        return None
    known = parse_version(_told_memo.get(os.fspath(vault_root), {}).get(app_id))
    if known is not None and known >= running_parsed:
        return None
    recorded, before = _record(vault_root, app_id, running_parsed, running)
    before_parsed = parse_version(before)
    if not recorded or before_parsed is None:
        return None
    arrived = [
        note
        for release in releases()
        if before_parsed < release.parsed <= running_parsed
        for note in release.notes
    ]
    if not arrived:
        return None
    return {"version": running, "say": _say(cloud_cell.store_identity()["name"], arrived)}
