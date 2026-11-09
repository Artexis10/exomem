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

from .. import access, vault
from ..kbdir import kb_dirname
from . import derive, mode

#: The ``store_meta`` keys: the revision of the last commit that changed an overlay source, the
#: overlay revision that the derived rows reflect, and the running pass (its target revision and
#: the last path it derived).
OVERLAY_REVISION = "vault_store_overlay_revision"
REGISTRY_REVISION = "vault_store_registry_revision"
PASS = "vault_store_registry_pass"
KEYS = (OVERLAY_REVISION, REGISTRY_REVISION, PASS)

#: The states that status reports: the closed set of this module's own states. ``unknown`` is
#: ``mode.UNKNOWN``.
CURRENT = "current"
REBUILDING = "rebuilding"

#: What one ``settle`` call did. A batch is bounded so that a request waits for at most one.
IDLE = "idle"
WAITING = "waiting"
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


def is_overlay_source(root: Path, rel: str) -> bool:
    """Whether the path holds an input of the registries or of the access policy.

    The schema overlay is the one folder that ownership gives to the store as pages, and the access
    policy is one file beside it; both come from the loaders' own paths, so a new registry file
    needs no entry here.
    """
    schema_folder = f"{kb_dirname()}/{vault.SCHEMA_OVERLAY_DIRNAME}/"
    policy = access.access_config_path(root).relative_to(root).as_posix()
    return rel.startswith(schema_folder) or rel == policy


def record_overlay_change(tx: Connection, root: Path, paths: Iterable[str], revision: int) -> None:
    """Record, inside the commit's transaction, that this revision changed an overlay source."""
    if any(is_overlay_source(root, rel) for rel in paths):
        _set(tx, OVERLAY_REVISION, str(revision))


def record_derived_from_files(tx: Connection, revision: int) -> None:
    """Migration's rows equal a cold derivation from the files, so they reflect ``revision``."""
    _set(tx, OVERLAY_REVISION, str(revision))
    _set(tx, REGISTRY_REVISION, str(revision))
    tx.execute("DELETE FROM store_meta WHERE key = ?", (PASS,))


def clear(tx: Connection) -> None:
    """Rollback returns the vault to files, which the owners derive again: none of this stays."""
    tx.executemany("DELETE FROM store_meta WHERE key = ?", [(key,) for key in KEYS])


def read(conn: Connection) -> Registry:
    """The registry state that the store's own records show."""
    meta = dict(
        conn.execute(
            "SELECT key, value FROM store_meta WHERE key IN (?, ?)",
            (OVERLAY_REVISION, REGISTRY_REVISION),
        )
    )
    reflected = _revision(meta.get(REGISTRY_REVISION))
    if reflected is None:
        # Nothing records which overlay the rows reflect (a vault that entered store mode before
        # this job): one pass over every page to the head settles it, and until then it is unknown.
        head = conn.execute("SELECT revision FROM store_mode WHERE id = 1").fetchone()
        return Registry(mode.UNKNOWN, None, None if head is None else head[0])
    changed = _revision(meta.get(OVERLAY_REVISION)) or 0
    if reflected >= changed:
        return Registry(CURRENT, reflected, changed)
    return Registry(REBUILDING, reflected, changed)


def settle(root: Path, writer) -> str:
    """Derive one batch of pages again toward the overlay revision, or say why none can run.

    A page's rows are derived from the disk's registries, so the batch waits until the projector has
    put every overlay file on disk. Each batch derives the stored pages after the cursor in path
    order, so a kill-free run and a resumed run write the same rows. The last batch records the
    revision that the rows now reflect.
    """
    conn = writer.connection
    registry = read(conn)
    if registry.state == CURRENT:
        return IDLE
    if _overlay_unprojected(conn, root):
        return WAITING
    target = registry.target
    if target is None:
        return IDLE  # no mode row: not a store-mode vault, so there is nothing to derive
    with writer.handle.transaction() as tx:
        found = _resume_after(tx, target)
        paths = [
            row[0]
            for row in tx.execute(
                "SELECT path FROM store_pages WHERE path > ? ORDER BY path LIMIT ?",
                (found, BATCH_PAGES),
            )
        ]
        if paths:
            derive.rederive_pages(tx, root, paths)
        if len(paths) < BATCH_PAGES:
            _set(tx, REGISTRY_REVISION, str(target))
            tx.execute("DELETE FROM store_meta WHERE key = ?", (PASS,))
            return DONE
        _set(tx, PASS, json.dumps({"target": target, "after": paths[-1]}))
        return ADVANCED


def _resume_after(tx: Connection, target: int) -> str:
    """The path after which this pass continues; a pass for another target starts over."""
    row = tx.execute("SELECT value FROM store_meta WHERE key = ?", (PASS,)).fetchone()
    if row is not None:
        try:
            recorded = json.loads(row[0])
            if recorded["target"] == target:
                return str(recorded["after"])
        except (ValueError, KeyError, TypeError):
            pass  # an unreadable cursor starts the pass over: it costs time, never correctness
    return ""


def _overlay_unprojected(conn: Connection, root: Path) -> bool:
    """Whether an overlay file still lacks the store's bytes on disk, queued or held.

    The loaders read the disk until the readers move to the store (tasks 3.1 and 3.2), so a pass
    that began earlier would derive rows from the old registry and record them as current. The wait
    costs the seconds that the projector takes, and it lapses once the loaders read the store.
    """
    queued = conn.execute("SELECT path, from_path FROM store_projection_queue").fetchall()
    held = conn.execute("SELECT path FROM store_held").fetchall()
    named = [rel for row in (*queued, *held) for rel in row if rel is not None]
    return any(is_overlay_source(root, rel) for rel in named)


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

