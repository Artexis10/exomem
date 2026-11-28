"""What changed in this release, for the person who uses the cell.

The release build writes `user_release_notes.json` beside this module (`scripts/user_notes.py
generate`): the notes of the 20 newest releases that have any, newest first. The format lives in
`release_notes_format`. Two things read the file:

- `payload` is the `bootstrap(section="whats_new")` answer that Home shows, under the key `whats_new`.
- `update_notice` is the one-time `update_notice` result field that tells a connected assistant,
  once, that a release with notes arrived since it last heard (Cloud cells only).

A missing or unusable file means the notes are unknown, which is not the same as no notes: the
section says `notes_available: false` and `update_notice` does nothing, so no app's record moves
past a release whose notes it never saw. A later image with the file tells everything since the
app's recorded version.

Everything here comes from the release build and the cell's own state. It reads no vault content.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import tempfile
from importlib import resources
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from .release_notes_format import Release, Version, note_object, parse_document, parse_version

_log = logging.getLogger(__name__)

NOTES_FILE = "user_release_notes.json"
STATE_FILENAME = ".app-update-notices.json"
STATE_SCHEMA = 1
SAY_PREFIX = "Tell the user once in this conversation: "
#: The most apps the cell remembers. An app that falls off the list is a new app on its next call,
#: which records the version and tells it nothing.
MAX_APPS = 500
_LOCK_TIMEOUT_SECONDS = 2.0


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


@functools.cache
def releases() -> tuple[Release, ...] | None:
    """The releases that have notes, newest first, or None when the notes are unknown. Read once per process.

    Unknown is a missing file or one that cannot be used. A development install has no file and says
    nothing; a Cloud cell is built with one, so there the log warns once that it is missing or
    unusable.
    """
    from . import cloud_cell
    from .log_events import log_event

    try:
        return parse_document(_read_text())
    except FileNotFoundError:
        if cloud_cell.cloud_mode_enabled():
            log_event(_log, logging.WARNING, "release_notes_unreadable")
    except (OSError, ValueError):
        log_event(_log, logging.WARNING, "release_notes_unreadable")
    return None


def payload() -> dict[str, Any]:
    """`{"whats_new": {version, releases, notes_available}}`: the running version and the notes of each recent release.

    Each release lists `notes`, the sentences as on every earlier version, and `items`, the same notes in the
    same order as `{"title", "text"}`; `title` is null for a note written without one. The payload only adds
    `items`, so a reader that knows `notes` alone keeps working.
    `releases` is empty both when the image has no notes and when it has none to give; `notes_available`
    tells the two apart, so Home never reads an unknown list as an empty one.
    """
    known = releases()
    return {
        "whats_new": {
            "version": running_version(),
            "releases": [
                {
                    "version": release.version,
                    "released_on": release.released_on,
                    "notes": [note.text for note in release.notes],
                    "items": [note_object(note) for note in release.notes],
                }
                for release in known or ()
            ],
            "notes_available": known is not None,
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
    handle, staged = tempfile.mkstemp(prefix=f".{STATE_FILENAME}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"schema": STATE_SCHEMA, "apps": told}, stream)
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


def state_path(vault_root: Path) -> Path:
    """The per-app record, in the vault's state directory."""
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / STATE_FILENAME


def lock_path(vault_root: Path) -> Path:
    """The lock that one app's read and write of the record share."""
    return state_path(vault_root).with_name(f".{STATE_FILENAME}.lock")


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
        state_paths.ensure_vault_state_dir(vault_root)
        path = state_path(vault_root)
        with FileLock(str(lock_path(vault_root)), timeout=_LOCK_TIMEOUT_SECONDS):
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
    result is unchanged. While the notes are unknown (no usable file) nothing happens at all: no
    record moves, so the next image that has the notes tells everything since the app's version.
    """
    from . import cloud_cell

    running = running_version()
    running_parsed = parse_version(running)
    notes = releases()
    if running_parsed is None or notes is None:
        return None
    known = parse_version(_told_memo.get(os.fspath(vault_root), {}).get(app_id))
    if known is not None and known >= running_parsed:
        return None
    recorded, before = _record(vault_root, app_id, running_parsed, running)
    before_parsed = parse_version(before)
    if not recorded or before_parsed is None:
        return None
    arrived = [
        note.text
        for release in notes
        if before_parsed < release.parsed <= running_parsed
        for note in release.notes
    ]
    if not arrived:
        return None
    return {"version": running, "say": _say(cloud_cell.store_identity()["name"], arrived)}
