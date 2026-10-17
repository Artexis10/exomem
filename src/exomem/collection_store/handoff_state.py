"""What a managed handoff's collections step left durable on this host (design §10, P1b.3).

The migrator classifies its own step by it, and the supervisor classifies a migrator it had
to stop the same way, so both read the step from the store and the marker, never from where
a process raised or was stopped. It loads no migration libraries, so a launcher can ask.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from .. import held_fs
from . import authority, connection, takeover
from .connection import CollectionStoreError

#: What a managed handoff's collections step ended in, as the supervisor records it, beside
#: the migration's ``ABANDONED``, ``RESTORED`` and report verdicts. ``migrated``: the marker
#: routes the store; ``pending``: a published store still carries its intent; ``unfinished``:
#: an apply that reported none of these, which published nothing either; ``unsafe``: this
#: host's live store is missing beside the marker that routes it while a swap's superseded
#: store remains, or the store or marker this step left cannot be read.
MIGRATED, PENDING, UNFINISHED, UNSAFE = "migrated", "pending", "unfinished", "unsafe"
#: How a managed handoff's migration ends without publishing (design §10, D2).
ABANDONED = "abandoned"
#: `left`'s reading of a store or marker that could not be read.
UNREADABLE = "unreadable"


def left(root):
    """The durable state a migration leaves on this host: (marker, live store).

    The marker is its bytes, None or ``UNREADABLE``. The store is "absent", "aside" (absent
    beside a marker while a superseded store remains: a swap stopped between its renames),
    ``UNREADABLE``, ``PENDING`` (it carries a migration intent) or "settled".
    """
    try:
        marker = authority.read_marker(root)
    except (CollectionStoreError, held_fs.HeldFsError, OSError):
        marker = UNREADABLE
    path = connection.store_path(root)
    if not path.exists():
        aside = marker is not None and any(path.parent.glob(path.name + takeover.SUPERSEDED + "*"))
        return marker, "aside" if aside else "absent"
    try:
        with closing(connection.open_head_reader(path)) as reader:
            intent = authority.pending_create(reader)
    except (CollectionStoreError, sqlite3.DatabaseError, ValueError):
        return marker, UNREADABLE
    return marker, PENDING if intent is not None and intent["kind"] == authority.MIGRATE else "settled"


def published(before, after):
    """What a route that raised left published, from durable state alone; None for nothing.

    Only the cutover's admission writes the marker, so a marker that changed under the route
    was this migration's, installed. A store set aside, or left unreadable when it was not
    before, is never read as nothing published: serving it in file mode could fork it.
    """
    (marker_before, store_before), (marker, store) = before, after
    if store == "aside" or (UNREADABLE in (marker, store) and UNREADABLE not in (marker_before, store_before)):
        return UNSAFE
    if store == PENDING:
        return PENDING
    if marker != marker_before and UNREADABLE not in (marker_before, store_before):
        return MIGRATED
    return None
