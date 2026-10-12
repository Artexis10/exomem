"""Recent assistant activity and the note count, for the owner's settings view.

Served as `bootstrap(section="activity")`. Everything comes from content-free
sources: the call ledger (client family, command, access, counts, times) and
the compiled-note count. Unknown is a visible state: an event's `items` and
`note_count` are null when not known, never zero, and `since` is the moment
from which the event list accounts for every call.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import call_ledger

#: At most this many events, newest first.
EVENT_LIMIT = 20
#: A row joins the newest event of its client and action when it is at most
#: this much older than that event's newest row.
MERGE_WINDOW = dt.timedelta(seconds=60)

#: Product verbs (the command registry's closed `product_actions` set) that make
#: a successful call an assistant memory event: a read that asks recalls; a
#: write that saves, records or updates saves. Everything else is not shown.
_RECALL_ACTIONS = frozenset({"ask"})
_SAVE_ACTIONS = frozenset({"save", "record", "update"})  # nosemgrep: ep-word-set -- members of the registry's closed product verb set, not text to interpret

_TAIL_CHUNK_BYTES = 64 * 1024


def payload(vault_root: Path) -> dict[str, Any]:
    """`{since, as_of, note_count, events}` for the calling owner."""
    from .cli_ops import OpError
    from .governance.principal import OWNER_AUDIENCE, effective_principal

    who = effective_principal()
    if not who.resolved or who.audience_id != OWNER_AUDIENCE:
        # Prevents a delegated principal from learning when and how much the
        # owner's other assistants recalled and saved. Costs nothing an owner
        # needs: a Cloud cell's bearer resolves to the cell owner.
        raise OpError("ACTIVITY_OWNER_REQUIRED", "activity is owner-only")
    if not call_ledger.enabled():
        raise OpError("ACTIVITY_UNAVAILABLE", "the call ledger is disabled on this server")
    try:
        since, events = _events(_rows_newest_first(call_ledger.ledger_path()))
    except OSError as error:
        raise OpError("ACTIVITY_UNAVAILABLE", "the call ledger could not be read") from error
    return {
        "since": _iso(since),
        "as_of": _iso(dt.datetime.now(dt.UTC)),
        "note_count": note_count(vault_root),
        "events": events,
    }


def _events(rows: Iterator[dict[str, Any]]) -> tuple[dt.datetime, list[dict[str, Any]]]:
    """The newest merged events, and the moment from which they are complete."""
    from . import commands
    from .privacy_log import client_family

    registry = {command.name: command for command in commands.PRODUCT_COMMANDS}
    events: list[dict[str, Any]] = []
    newest_by_key: dict[tuple[str | None, str], dict[str, Any]] = {}
    oldest_seen: dt.datetime | None = None
    for row in rows:
        if "access" not in row:
            # A version 1 row cannot say whether it read or wrote, so the list
            # can account only for the version 2 rows after it.
            break
        at = _parse_time(row.get("ts_utc"))
        if at is None:
            continue
        if len(events) == EVENT_LIMIT and at < events[-1]["_newest"] - MERGE_WINDOW:
            break  # nothing older can join a listed event or be listed
        oldest_seen = at
        tool = row.get("tool")
        action = _action(registry.get(tool) if isinstance(tool, str) else None, row.get("access"))
        if row.get("outcome") != "ok" or action is None:
            continue
        key = (client_family(row.get("client_name")), action)
        items = row.get("item_count") if type(row.get("item_count")) is int else None
        event = newest_by_key.get(key)
        if event is not None and event["_newest"] - at <= MERGE_WINDOW:
            # Unknown is contagious: a sum over a call of unknown size is not a count.
            event["items"] = None if event["items"] is None or items is None else event["items"] + items
            continue
        if len(events) == EVENT_LIMIT:
            continue
        event = {"client": key[0], "action": action, "items": items, "at": _iso(at), "_newest": at}
        events.append(event)
        newest_by_key[key] = event
    since = oldest_seen if oldest_seen is not None else call_ledger.WRITER_STARTED_AT
    return since, [{k: v for k, v in event.items() if k != "_newest"} for event in events]


def _action(command: Any, access: object) -> str | None:
    if command is None:
        return None
    actions = frozenset(command.product_actions)
    if access == "read" and actions & _RECALL_ACTIONS:
        return "recalled"
    if access == "write" and actions & _SAVE_ACTIONS:
        return "saved"
    return None


def _rows_newest_first(path: Path) -> Iterator[dict[str, Any]]:
    """The live ledger file's rows, last line first, read in bounded chunks.

    The file is bounded by the ledger's own rotation. A line that is not a JSON
    object (a torn append, a damaged row) is skipped; `verify` reports it.
    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return
    with handle:
        position = handle.seek(0, os.SEEK_END)
        remainder = b""
        while position > 0:
            step = min(_TAIL_CHUNK_BYTES, position)
            position -= step
            handle.seek(position)
            lines = (handle.read(step) + remainder).split(b"\n")
            remainder = lines.pop(0)
            for line in reversed(lines):
                row = _parse_row(line)
                if row is not None:
                    yield row
        row = _parse_row(remainder)
        if row is not None:
            yield row


def _parse_row(line: bytes) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(dt.UTC) if parsed.tzinfo is not None else None


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat(timespec="milliseconds")


_note_count_lock = threading.Lock()
_note_count_cache: dict[str, tuple[int, int]] = {}


def note_count(vault_root: Path) -> int | None:
    """Compiled notes in the vault, or `None` when the count is unknown.

    A Cloud cell caches the count against the writer commit counter: every
    change to its vault goes through a governed writer, which advances the
    counter. A local install can be edited outside the writers, so it counts
    every time rather than serve a stale number.
    """
    from . import cloud_cell, writer_lease

    key = str(vault_root)
    generation = (
        writer_lease.read_commit_generation(vault_root)
        if cloud_cell.cloud_mode_enabled()
        else None
    )
    if generation is not None:
        with _note_count_lock:
            cached = _note_count_cache.get(key)
        if cached is not None and cached[0] == generation:
            return cached[1]
    count = _count_notes(vault_root)
    if generation is not None and count is not None:
        with _note_count_lock:
            _note_count_cache[key] = (generation, count)
    return count


def _count_notes(vault_root: Path) -> int | None:
    from . import indexes
    from .vault import kb_root

    errors: list[OSError] = []
    try:
        counts = indexes._count_notes(kb_root(vault_root) / "Notes", on_error=errors.append)
    except OSError:
        return None
    return None if errors else sum(counts.values())
