"""Recent assistant activity and the note count, for the owner's settings view.

Served as `bootstrap(section="activity")`. Everything comes from content-free
sources: the call ledger (actor ID, client family, command, access, counts,
times) and the count of saved pages. Unknown is a visible state: an event's `items` and
`note_count` are null when not known, never zero, and `since` is the moment
from which the event list accounts for every call. It never covers a row the
reader could not read or the list could not show.
"""

from __future__ import annotations

import datetime as dt
import json
import os
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


def _unavailable(reason: str) -> Exception:
    from .cli_ops import OpError

    return OpError("ACTIVITY_UNAVAILABLE", reason)


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
        raise _unavailable("the call ledger is disabled on this server")
    try:
        since, events = _events(_lines_newest_first(call_ledger.ledger_path()), _known_from())
    except (OSError, ValueError) as error:  # ValueError: a tampered row the reader cannot use
        raise _unavailable("the call ledger could not be read") from error
    return {
        "since": _iso(since),
        "as_of": _iso(dt.datetime.now(dt.UTC)),
        "note_count": note_count(vault_root),
        "events": events,
    }


def _known_from() -> dt.datetime | None:
    """The time after which every call left a row, as far as the reader can know.

    A failed append left a call with no row. On a Cloud cell, a restarted
    container reads the same pod-scoped ledger as the process before it, which
    may have lost a call with neither a row nor a marker: on a full or read-only
    volume, or when it was killed between a commit and its append. So a cell
    claims nothing before this writer started; replacing the pod discards the
    older history anyway.
    """
    from . import cloud_cell

    bounds = [call_ledger.last_append_failure()]
    if cloud_cell.cloud_mode_enabled():
        bounds.append(call_ledger.WRITER_STARTED_AT - dt.timedelta(milliseconds=1))
    known = [bound for bound in bounds if bound is not None]
    return max(known) if known else None


def _events(
    lines: Iterator[bytes], known_from: dt.datetime | None = None
) -> tuple[dt.datetime, list[dict[str, Any]]]:
    """The newest merged events, and the moment from which they are complete.

    The scan reads intact rows, newest first, and stops at the first row the
    list cannot represent: a damaged row, a row that cannot say whether it read
    or wrote (version 1), or a call the full list has no room for. Every row the
    scan did not read was appended earlier, so its time is no later than the
    boundary: the stopping row's time, or the oldest time read when the
    stopping row is damaged or rotation archived the older rows. `known_from`
    (see `_known_from`) is a boundary too.
    """
    from . import commands
    from .privacy_log import client_family

    registry = {command.name: command for command in commands.PRODUCT_COMMANDS}
    listing = _Listing()
    read: list[tuple[dt.datetime, tuple[str | None, str | None, str] | None, int | None]] = []
    boundary: dt.datetime | None = None
    newer: dict[str, Any] | None = None
    for line in lines:
        if not line.strip():
            continue  # a blank line holds no row; a removed row breaks the chain
        row = _parse_row(line)
        at = _parse_time(row.get("ts_utc")) if row is not None else None
        if row is None or at is None or not _intact(row, newer):
            if not read:
                raise _unavailable("the call ledger's newest row is damaged")
            boundary = read[-1][0]
            break
        newer = row
        if "access" not in row or "item_count" not in row or listing.closed(at):
            boundary = at
            break
        key, items = _event_key(row, registry, client_family)
        if key is not None and not listing.add(at, key, items):
            boundary = at
            break
        read.append((at, key, items))
    else:
        if newer is not None and not (
            newer["sequence"] == 1 and newer.get("prev_hash") == call_ledger.GENESIS_HASH
        ):
            boundary = read[-1][0]  # rotation archived the older rows
    if known_from is not None:
        known_from = _floor_ms(known_from)
        boundary = known_from if boundary is None else max(boundary, known_from)
    return _interval(read, boundary)


def _interval(
    read: list[tuple[dt.datetime, tuple[str | None, str | None, str] | None, int | None]],
    boundary: dt.datetime | None,
) -> tuple[dt.datetime, list[dict[str, Any]]]:
    """`since` and the events over the rows strictly later than the boundary.

    Strictly later at the API's millisecond precision: a row the scan did not
    read can share the boundary's millisecond. The events are counted again
    over the kept rows, so a dropped row adds nothing to a listed count.
    """
    writer_started = _floor_ms(call_ledger.WRITER_STARTED_AT)
    kept = read if boundary is None else [entry for entry in read if entry[0] > boundary]
    if kept:
        since = kept[-1][0]
    elif boundary is None or boundary < writer_started:
        # Every call this writer recorded is later than the boundary, so none is missing.
        since = writer_started
    else:
        raise _unavailable("no intact call ledger row is later than where its history stops")
    listing = _Listing()
    for at, key, items in kept:
        if key is not None:
            listing.add(at, key, items)
    return since, listing.public()


def _intact(row: dict[str, Any], newer: dict[str, Any] | None) -> bool:
    """The row hashes to its own `row_hash`, and the newer row read before it
    links to it by sequence and `prev_hash`."""
    sequence = row.get("sequence")
    if type(sequence) is not int or row.get("row_hash") != call_ledger.row_hash(row):
        return False
    return newer is None or (
        newer["sequence"] == sequence + 1 and newer.get("prev_hash") == row["row_hash"]
    )


def _event_key(
    row: dict[str, Any], registry: dict[str, Any], client_family: Any
) -> tuple[tuple[str | None, str | None, str] | None, int | None]:
    """The (app ID, client family, action) of a successful memory call, and its count."""
    from .cloud_cell import canonical_app_id

    tool = row.get("tool")
    action = _action(registry.get(tool) if isinstance(tool, str) else None, row.get("access"))
    if row.get("outcome") != "ok" or action is None:
        return None, None
    items = row["item_count"] if type(row["item_count"]) is int else None
    return (
        canonical_app_id(row.get("app_id")), client_family(row.get("client_name")), action
    ), items


def _action(command: Any, access: object) -> str | None:
    if command is None:
        return None
    actions = frozenset(command.product_actions)
    if access == "read" and actions & _RECALL_ACTIONS:
        return "recalled"
    if access == "write" and actions & _SAVE_ACTIONS:
        return "saved"
    return None


class _Listing:
    """Merged events, newest first, at most `EVENT_LIMIT`."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._newest_by_key: dict[tuple[str | None, str | None, str], dict[str, Any]] = {}

    def closed(self, at: dt.datetime) -> bool:
        """Full, and a row this old can join no listed event."""
        return len(self.events) == EVENT_LIMIT and at < self.events[-1]["_newest"] - MERGE_WINDOW

    def add(
        self, at: dt.datetime, key: tuple[str | None, str | None, str], items: int | None
    ) -> bool:
        """Join the newest event of the key or list a new one; False when the full
        list has no room for it."""
        event = self._newest_by_key.get(key)
        if event is not None and event["_newest"] - at <= MERGE_WINDOW:
            # Unknown is contagious: a sum over a call of unknown size is not a count.
            event["items"] = (
                None if event["items"] is None or items is None else event["items"] + items
            )
            return True
        if len(self.events) == EVENT_LIMIT:
            return False
        event = {
            "app_id": key[0], "client": key[1], "action": key[2],
            "items": items, "at": _iso(at), "_newest": at,
        }
        self.events.append(event)
        self._newest_by_key[key] = event
        return True

    def public(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in event.items() if k != "_newest"} for event in self.events]


def _lines_newest_first(path: Path) -> Iterator[bytes]:
    """The live ledger file's lines, last first, read in bounded chunks.

    The segment after the file's last newline is skipped: it is empty, or a row
    still being appended, which is not yet history. The file is bounded by the
    ledger's own rotation.
    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return
    with handle:
        position = handle.seek(0, os.SEEK_END)
        remainder = b""
        tail = True
        while position > 0:
            step = min(_TAIL_CHUNK_BYTES, position)
            position -= step
            handle.seek(position)
            lines = (handle.read(step) + remainder).split(b"\n")
            remainder = lines.pop(0)
            for line in reversed(lines):
                if tail:
                    tail = False
                    continue
                yield line
        if not tail:
            yield remainder


def _parse_row(line: bytes) -> dict[str, Any] | None:
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
    return _floor_ms(parsed.astimezone(dt.UTC)) if parsed.tzinfo is not None else None


def _floor_ms(value: dt.datetime) -> dt.datetime:
    """The API's precision: `since` and `at` are reported in milliseconds."""
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat(timespec="milliseconds")


def note_count(vault_root: Path) -> int | None:
    """Pages saved in the Knowledge Base, or `None` when the count is unknown.

    Counted on every call, never cached: no token that every writer process
    advances can prove a cached count current, and a count older than a
    committed write is a false fact.
    """
    return _count_saved_pages(vault_root)


def _count_saved_pages(vault_root: Path) -> int | None:
    """Every page the Knowledge Base corpus walk yields: sources, records kept
    as pages, entities, evidence and compiled notes. Navigation pages and
    collection manifests are structure, not saved items. A walk that could not
    read part of the tree is unknown."""
    from .find_corpus import NAVIGATION_BASENAMES, walk_md
    from .structured_collections import MANIFEST_FILENAME
    from .vault import kb_root

    root = kb_root(vault_root)
    try:
        os.lstat(root)
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    errors: list[OSError] = []
    structure = NAVIGATION_BASENAMES | {MANIFEST_FILENAME}
    count = sum(1 for page in walk_md(root, on_error=errors.append) if page.name not in structure)
    return None if errors else count
