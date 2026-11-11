"""The registry and access-policy state of a store-mode vault, settled in the background.

OpenSpec make-vault-store-canonical D2 and task 2.4. The owners' builders read the registries
(traversal profiles, the note-type basis, project keys, the activation manifest) and the access
policy from the schema overlay: ``_Schema/**`` pages and ``_access.yaml``. A commit that changes
any of them derives its own pages with the previous overlay, so the rows of every page that the
overlay touches may be behind. That commit records its revision in ``store_meta`` and returns at
once. This job then derives each stored page again, one bounded transaction at a time, and records
the revision that the derived rows now reflect. Status reads both revisions: the gap between them
is the ``rebuilding`` state.

The revisions and the pass cursor are ``store_meta`` keys, so no collection-store schema carries
them. A batch and its cursor commit together: a kill leaves the rows behind the store and never
ahead of it, and the next start resumes at the cursor. The store thread's scheduler runs one batch
per tick (``StoreServer._registry_tick``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

from .. import access, registry_history, vault
from ..kbdir import kb_dirname
from . import derive, mode

#: The ``store_meta`` keys: the revision of the last commit that changed an overlay source, the
#: overlay revision that the derived rows reflect, and the running pass (its target revision and
#: the last path it derived).
OVERLAY_REVISION = "vault_store_overlay_revision"
REGISTRY_REVISION = "vault_store_registry_revision"
PASS = "vault_store_registry_pass"
#: The version of the owners' objects (tables and indexes outside the collection schema) whose rows
#: the store holds, and this build's version. A store that an older build set up lacks an object
#: that this build derives; its pass creates the objects and derives every page, and only then
#: records this version. Version 1 adds the inbound-link tables.
OWNER_SCHEMA = "vault_store_owner_schema"
OWNER_SCHEMA_VERSION = 1
KEYS = (OVERLAY_REVISION, REGISTRY_REVISION, PASS, OWNER_SCHEMA)

#: The states that status reports: the closed set of this module's own states. ``unknown`` is
#: ``mode.UNKNOWN``.
CURRENT = "current"
REBUILDING = "rebuilding"

#: What ``pending`` and one ``settle`` call report. A batch is bounded so that a request waits for
#: at most one. ``READY`` is only ``pending``'s answer: a batch can run.
IDLE = "idle"
WAITING = "waiting"
READY = "ready"
ADVANCED = "advanced"
DONE = "done"
BATCH_PAGES = 32


@dataclass(frozen=True, slots=True)
class Registry:
    """The overlay state of the derived rows: ``current``, ``rebuilding`` or ``unknown``.

    ``revision`` is the overlay revision that the rows reflect, and None when no record says.
    ``target`` is the revision of the last commit that changed the overlay (the head, when nothing
    records the rows at all).
    """

    state: str
    revision: int | None
    target: int | None
    #: Whether the store's owner objects are older than this build's (``OWNER_SCHEMA``): the state
    #: is then ``unknown`` until a pass creates and fills them.
    owner_schema_behind: bool = False


def is_overlay_source(root: Path, rel: str) -> bool:
    """Whether the path holds an input of the registries or of the access policy.

    The schema overlay is the one folder that ownership gives to the store as pages, and the access
    policy is one file beside it; both come from the loaders' own paths, so a new registry file
    needs no entry here. The kept versions under the history folder are not inputs: the loaders read
    the live overlay files, and only the registry tools read a kept version, at request time.
    """
    schema_folder = f"{kb_dirname()}/{vault.SCHEMA_OVERLAY_DIRNAME}/"
    history = f"{registry_history.history_dir(root, '').relative_to(root).as_posix()}/"
    policy = access.access_config_path(root).relative_to(root).as_posix()
    return (rel.startswith(schema_folder) and not rel.startswith(history)) or rel == policy


def record_overlay_change(tx: Connection, root: Path, paths: Iterable[str], revision: int) -> None:
    """Record, inside the commit's transaction, that this revision changed an overlay source."""
    if any(is_overlay_source(root, rel) for rel in paths):
        _set(tx, OVERLAY_REVISION, str(revision))


def record_derived_from_files(tx: Connection, revision: int) -> None:
    """Migration's rows equal a cold derivation from the files, so they reflect ``revision``, and its
    owners' objects are this build's."""
    _set(tx, OVERLAY_REVISION, str(revision))
    _set(tx, REGISTRY_REVISION, str(revision))
    _set(tx, OWNER_SCHEMA, str(OWNER_SCHEMA_VERSION))
    tx.execute("DELETE FROM store_meta WHERE key = ?", (PASS,))


def clear(tx: Connection) -> None:
    """Rollback returns the vault to files, which the owners derive again: none of this stays."""
    tx.executemany("DELETE FROM store_meta WHERE key = ?", [(key,) for key in KEYS])


def read(conn: Connection) -> Registry:
    """The registry state that the store's own records show.

    ``unknown`` is a store that records no reflected revision, and one whose record of the last
    overlay change is not a number: neither says which overlay the rows reflect, and a guess of
    ``current`` would serve the rows of an overlay that may have changed. A store whose owner
    objects are older than this build's is ``unknown`` too: some of its rows do not exist yet.
    """
    meta = dict(
        conn.execute(
            "SELECT key, value FROM store_meta WHERE key IN (?, ?, ?)",
            (OVERLAY_REVISION, REGISTRY_REVISION, OWNER_SCHEMA),
        )
    )
    reflected = _revision(meta.get(REGISTRY_REVISION))
    # No overlay change on record is a revision of 0: every reflected revision is at or past it.
    changed = 0 if OVERLAY_REVISION not in meta else _revision(meta[OVERLAY_REVISION])
    owners = _revision(meta.get(OWNER_SCHEMA))
    behind = owners is None or owners < OWNER_SCHEMA_VERSION
    if reflected is None or changed is None or behind:
        # One pass over every page to the head settles it, and until then it is unknown.
        head = conn.execute("SELECT revision FROM store_mode WHERE id = 1").fetchone()
        return Registry(mode.UNKNOWN, reflected, None if head is None else head[0], behind)
    if reflected >= changed:
        return Registry(CURRENT, reflected, changed)
    return Registry(REBUILDING, reflected, changed)


def pending(conn: Connection, root: Path) -> str:
    """Whether a batch is owed: ``idle``, ``waiting`` for the projector, or ``ready`` to run.

    It reads only, so the store thread asks it on a plain read and takes the vault's mutation
    boundary for a batch that can run, never for a tick with nothing to do.
    """
    return _due(conn, root)[0]


def waiting_on(conn: Connection, root: Path) -> int:
    """How many overlay paths still wait to reach the disk, queued or held: the job waits for each.

    A held path waits for a person, so it can hold the job back for as long as the hold lasts. The
    count names no path and no content: status shows it, and the held list names the paths.

    The loaders read the disk until the readers move to the store (tasks 3.1 and 3.2), so a pass
    that began while a path waited would derive rows from the old registry and record them as
    current. The wait costs the seconds that the projector takes, and it lapses once the loaders
    read the store.
    """
    queued = conn.execute("SELECT path, from_path FROM store_projection_queue").fetchall()
    held = conn.execute("SELECT path FROM store_held").fetchall()
    named = {rel for row in (*queued, *held) for rel in row if rel is not None}
    return sum(is_overlay_source(root, rel) for rel in named)


def settle(root: Path, writer) -> str:
    """Derive one batch of pages again toward the overlay revision, or say why none can run.

    A page's rows are derived from the disk's registries, so the batch waits until the projector has
    put every overlay file on disk. Each batch derives the stored pages after the cursor in path
    order, so a kill-free run and a resumed run write the same rows. The last batch records the
    revision that the rows now reflect.
    """
    conn = writer.connection
    due, registry = _due(conn, root)
    target = registry.target
    if due != READY or target is None:
        return due
    with writer.handle.transaction() as tx:
        if registry.owner_schema_behind:
            _create_owner_objects(tx, root)
        target, found = _resume(tx, target, registry.state)
        paths = [
            row[0]
            for row in tx.execute(
                "SELECT path FROM store_pages WHERE path > ? ORDER BY path LIMIT ?",
                (found, BATCH_PAGES),
            )
        ]
        if paths:
            derive.rederive_pages(tx, root, paths)
        # A batch changes derived rows, and the last one the registry state, with no page commit.
        # Moving the store revision makes every cache keyed on it see that change (task 3.3).
        tx.execute("UPDATE store_mode SET revision = revision + 1 WHERE id = 1")
        if len(paths) < BATCH_PAGES:
            _set(tx, REGISTRY_REVISION, str(target))
            overlay = tx.execute(
                "SELECT value FROM store_meta WHERE key = ?", (OVERLAY_REVISION,)
            ).fetchone()
            if overlay is not None and _revision(overlay[0]) is None:
                # Every page reflects the overlay at ``target``, so it replaces the damaged record.
                _set(tx, OVERLAY_REVISION, str(target))
            if registry.owner_schema_behind:
                _set(tx, OWNER_SCHEMA, str(OWNER_SCHEMA_VERSION))
            tx.execute("DELETE FROM store_meta WHERE key = ?", (PASS,))
            return DONE
        _set(tx, PASS, json.dumps({"target": target, "after": paths[-1]}))
        return ADVANCED


def _create_owner_objects(tx: Connection, root: Path) -> None:
    """Create, in the batch's transaction, each owner object that this build has and the store lacks,
    and record it, so that a rollback drops it with the objects that migration created.

    The owners' DDL creates only what is missing, so every batch of the pass may run it. The pass
    derives the stored pages; the files that the store does not own get their link rows here, once,
    from the disk that holds them, as migration derives them.
    """
    from .migrate import create_owner_schema

    created = dict(item for group in create_owner_schema(tx) for item in group.items())
    tx.executemany(
        "INSERT OR IGNORE INTO store_mode_objects(name, type) VALUES (?, ?)", list(created.items())
    )
    if "inbound_links" in created:
        held = frozenset(row[0] for row in tx.execute("SELECT path FROM store_pages"))
        vault.derive_inbound_rows(root, tx, include=lambda rel: rel not in held)


def _due(conn: Connection, root: Path) -> tuple[str, Registry]:
    registry = read(conn)
    if registry.state == CURRENT or registry.target is None:
        # No mode row means not a store-mode vault: there is nothing to derive.
        return IDLE, registry
    return (WAITING if waiting_on(conn, root) else READY), registry


def _resume(tx: Connection, target: int, state: str) -> tuple[int, str]:
    """The target of this batch's pass and the path after which it continues.

    A pass for a rebuild starts over at a new target, because the pages before its cursor were
    derived from an overlay that has changed since. A pass for an unknown state keeps the target it
    began with: the head moves with every commit, a page committed meanwhile is derived by the
    overlay it was committed under, and an overlay commit sets its own revision, which this pass
    does not reflect. Restarting at each head would never finish on a vault that is written to.
    """
    row = tx.execute("SELECT value FROM store_meta WHERE key = ?", (PASS,)).fetchone()
    if row is not None:
        try:
            recorded = json.loads(row[0])
            pinned = int(recorded["target"])
            if pinned == target or state == mode.UNKNOWN:
                return pinned, str(recorded["after"])
        except (ValueError, KeyError, TypeError):
            pass  # an unreadable cursor starts the pass over: it costs time, never correctness
    return target, ""


def _revision(value: str | None) -> int | None:
    try:
        return None if value is None else int(value)
    except ValueError:
        return None


def _set(tx: Connection, key: str, value: str) -> None:
    tx.execute(
        "INSERT INTO store_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )

