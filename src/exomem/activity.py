"""Recent assistant activity and the note count, for the owner's settings view.

Served as `bootstrap(section="activity")`. Everything comes from content-free
sources: the call ledger (client family, command, access, counts, times) and
the count of saved pages. Unknown is a visible state: an event's `items` and
`note_count` are null when not known, never zero, and `since` is the moment
from which the event list accounts for every call. It never covers a row the
reader could not read or the list could not show.
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
        since, events = _events(_lines_newest_first(call_ledger.ledger_path()))
    except OSError as error:
        raise _unavailable("the call ledger could not be read") from error
    return {
        "since": _iso(since),
        "as_of": _iso(dt.datetime.now(dt.UTC)),
        "note_count": note_count(vault_root),
        "events": events,
    }


def _events(lines: Iterator[bytes]) -> tuple[dt.datetime, list[dict[str, Any]]]:
    """The newest merged events, and the moment from which they are complete.

    The scan stops at the first row the list cannot represent: a damaged row
    (not JSON, no valid time, or a break in the sequence), a version 1 row,
    which cannot say whether it read or wrote, or a call the full list has no
    room for. `since` is then the oldest row the list accounts for. Rows are
    appended in time order, so a damaged row is older than every row after it;
    a version 1 row has a time, which must be older than `since`.
    """
    from . import commands
    from .privacy_log import client_family

    registry = {command.name: command for command in commands.PRODUCT_COMMANDS}
    events: list[dict[str, Any]] = []
    newest_by_key: dict[tuple[str | None, str], dict[str, Any]] = {}
    oldest_accounted: dt.datetime | None = None
    stopped_at: dt.datetime | None = None
    damaged = False
    expected_sequence: int | None = None
    for line in lines:
        if not line.strip():
            continue  # a blank line holds no row; a removed row breaks the sequence
        row = _parse_row(line)
        sequence = row.get("sequence") if row is not None else None
        at = _parse_time(row.get("ts_utc")) if row is not None else None
        if (
            row is None
            or at is None
            or type(sequence) is not int
            or (expected_sequence is not None and sequence != expected_sequence)
        ):
            damaged = True
            break
        expected_sequence = sequence - 1
        if row.get("schema_version") == 1:
            stopped_at = at
            break
        if "access" not in row or "item_count" not in row:
            damaged = True
            break
        if len(events) == EVENT_LIMIT and at < events[-1]["_newest"] - MERGE_WINDOW:
            stopped_at = at
            break  # nothing older can join a listed event
        tool = row.get("tool")
        action = _action(registry.get(tool) if isinstance(tool, str) else None, row.get("access"))
        if row.get("outcome") == "ok" and action is not None:
            key = (client_family(row.get("client_name")), action)
            items = row["item_count"] if type(row["item_count"]) is int else None
            event = newest_by_key.get(key)
            if event is not None and event["_newest"] - at <= MERGE_WINDOW:
                # Unknown is contagious: a sum over a call of unknown size is not a count.
                event["items"] = (
                    None if event["items"] is None or items is None else event["items"] + items
                )
            elif len(events) == EVENT_LIMIT:
                stopped_at = at
                break  # the first call the list cannot show ends what it accounts for
            else:
                event = {
                    "client": key[0], "action": action, "items": items, "at": _iso(at),
                    "_newest": at,
                }
                events.append(event)
                newest_by_key[key] = event
        oldest_accounted = at
    if damaged and oldest_accounted is None:
        # No row is newer than the damage, so no interval can be claimed complete.
        raise _unavailable("the call ledger's newest row is damaged")
    since = oldest_accounted if oldest_accounted is not None else call_ledger.WRITER_STARTED_AT
    if stopped_at is not None and stopped_at >= since:
        # The row that stopped the scan is not older than what the list claims to
        # cover, as when an older process still appends version 1 rows: no
        # interval can be claimed complete.
        raise _unavailable("the call ledger's order does not cover a complete interval")
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
    return parsed.astimezone(dt.UTC) if parsed.tzinfo is not None else None


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat(timespec="milliseconds")


_note_count_lock = threading.Lock()
_note_count_cache: dict[str, tuple[tuple[int, int], int]] = {}


def note_count(vault_root: Path) -> int | None:
    """Pages saved in the Knowledge Base, or `None` when the count is unknown.

    A Cloud cell caches the count against two counters that every governed
    writer advances as it leaves the mutation boundary: this process's own
    count, which cannot fail to advance, and the vault's commit generation on
    disk, which also sees a writer in another process such as the media worker.
    A local install can be edited outside the writers, so it counts every time
    rather than serve a stale number.
    """
    from . import cloud_cell, writer_lease

    key = str(vault_root)
    generation: tuple[int, int] | None = None
    if cloud_cell.cloud_mode_enabled():
        on_disk = writer_lease.read_commit_generation(vault_root)
        if on_disk is not None:  # unreadable: count every time
            generation = (writer_lease.process_mutation_count(), on_disk)
    if generation is not None:
        with _note_count_lock:
            cached = _note_count_cache.get(key)
        if cached is not None and cached[0] == generation:
            return cached[1]
    count = _count_saved_pages(vault_root)
    if generation is not None and count is not None:
        with _note_count_lock:
            _note_count_cache[key] = (generation, count)
    return count


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
