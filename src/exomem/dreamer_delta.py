"""Which pages the dreamer processes next. Never a filesystem walk.

Three sources, one work list (the store's `pending` table):

1. **Fast path.** When the stored checkpoint belongs to this process's freshness
   instance, `freshness.delta_since` gives the exact coalesced change set.
2. **Restart and overflow.** When the delta is incomplete, the live signature
   map (`freshness.live_entries`, one dict copy under the registry lock) is
   diffed against the persisted `seen` map. Signatures `(mtime_ns, ctime_ns,
   size)` do not depend on the process, so the diff is exact and a restart
   costs O(pages) dict comparisons and not one filesystem call.
3. **First enable, or a lost sidecar.** `seen` is empty, so the same diff marks
   every live page changed: the reseed. It drains through the same table in
   sorted path order, so it is deterministic and resumable.

The freshness checkpoint captured before a diff (or the delta's target) is
held as `pending_checkpoint` and committed only once `pending` is empty, so the
stored checkpoint never claims coverage the worker has not reached.

Nothing here needs a consistent vault-wide snapshot. Each page's contribution
is recomputed from that page alone and keyed by its signature, so a write that
lands mid-pass changes that page's signature and puts it back on the delta.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import access, dreamer_store, freshness
from .find_corpus import EXCLUDED_DIR_NAMES, EXCLUDED_DIR_PREFIXES, NAVIGATION_BASENAMES
from .kbdir import kb_dirname

SCOPE = "vault"


@dataclass(frozen=True)
class Work:
    """The next batch of vault-relative paths, or why there is none."""

    paths: list[str] = field(default_factory=list)
    reseeding: bool = False
    remaining: int = 0
    waiting: str | None = None


def _encode_checkpoint(value: freshness.FreshnessCheckpoint) -> list:
    return [
        value.instance_id,
        int(value.generation),
        list(value.triple) if value.triple is not None else None,
    ]


def _decode_checkpoint(value: object) -> freshness.FreshnessCheckpoint | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    instance, generation, triple = value
    try:
        parsed = tuple(triple) if isinstance(triple, list) and len(triple) == 3 else None
        return freshness.FreshnessCheckpoint(
            str(instance),
            int(generation),
            (int(parsed[0]), int(parsed[1]), str(parsed[2])) if parsed else None,
        )
    except (TypeError, ValueError):
        return None


def checkpoint(
    store: dreamer_store.DreamerStore, conn: sqlite3.Connection
) -> freshness.FreshnessCheckpoint | None:
    """The committed checkpoint: every change before it has been processed."""
    return _decode_checkpoint(store.get_meta(conn, "checkpoint"))


def eligible(vault_root: Path, rel_path: str) -> bool:
    """Indexable Markdown under the Knowledge Base, outside governance and trash."""
    rel = PurePosixPath(rel_path)
    if rel.suffix.lower() != ".md" or not rel.parts or rel.parts[0] != kb_dirname():
        return False
    if rel.name.casefold() in NAVIGATION_BASENAMES:
        return False
    for part in rel.parts[1:-1]:
        if part in EXCLUDED_DIR_NAMES or part.startswith(EXCLUDED_DIR_PREFIXES):
            return False
    return access.is_indexable(vault_root, rel_path)


def _relative(vault_root: Path, key: str) -> str | None:
    try:
        return Path(key).relative_to(vault_root).as_posix()
    except ValueError:
        return None


def advance_if_drained(store: dreamer_store.DreamerStore, conn: sqlite3.Connection) -> bool:
    """Commit the held checkpoint once nothing is pending. True when it moved."""
    if store.pending_count(conn):
        return False
    held = store.get_meta(conn, "pending_checkpoint")
    if held is None:
        return False
    store.set_meta(conn, "checkpoint", held)
    store.set_meta(conn, "pending_checkpoint", None)
    return True


def has_work(store: dreamer_store.DreamerStore, conn: sqlite3.Connection, vault_root: Path) -> bool:
    """Whether a tick would find anything to process. Read-only and O(1).

    False only when nothing is pending and the committed checkpoint is this
    process's current freshness generation, so an idle poll writes nothing.
    """
    if store.pending_count(conn):
        return True
    held = checkpoint(store, conn)
    if held is None:
        return True
    current = freshness.generation(Path(vault_root), SCOPE)
    if current is None:
        return True
    return held.instance_id != freshness.consumer_checkpoint_instance() or (
        held.generation != current
    )


def next_paths(
    store: dreamer_store.DreamerStore,
    conn: sqlite3.Connection,
    vault_root: Path,
    *,
    limit: int,
) -> Work:
    """Up to `limit` paths to process next. Call inside a store write transaction."""
    vault_root = Path(vault_root)
    if not freshness.is_live(vault_root, SCOPE):
        return Work(waiting="freshness_unavailable")
    reseeding = bool(store.get_meta(conn, "reseeding"))
    if not store.pending_count(conn):
        advance_if_drained(store, conn)
        reseeding = False
        store.set_meta(conn, "reseeding", None)
        _queue_changes(store, conn, vault_root)
        reseeding = bool(store.get_meta(conn, "reseeding"))
    remaining = store.pending_count(conn)
    return Work(
        paths=store.pending_next(conn, limit),
        reseeding=reseeding and remaining > 0,
        remaining=remaining,
    )


def _queue_changes(
    store: dreamer_store.DreamerStore, conn: sqlite3.Connection, vault_root: Path
) -> None:
    held = checkpoint(store, conn)
    if held is not None:
        delta = freshness.delta_since(vault_root, SCOPE, held)
        if delta.complete:
            touched: set[str] = set()
            for key in sorted(delta.changed):
                rel = _relative(vault_root, key)
                if rel is None or not eligible(vault_root, rel):
                    continue
                live = freshness.live_signature(vault_root, SCOPE, key)
                if dreamer_store.encode_sig(live) != store.seen_get(conn, rel):
                    touched.add(rel)
            for key in sorted(delta.deleted):
                rel = _relative(vault_root, key)
                if rel is not None and store.seen_get(conn, rel) is not None:
                    touched.add(rel)
            if touched:
                store.pending_add(conn, sorted(touched))
                store.set_meta(conn, "pending_checkpoint", _encode_checkpoint(delta.to))
            else:
                store.set_meta(conn, "checkpoint", _encode_checkpoint(delta.to))
            return
    captured = freshness.consumer_checkpoint(vault_root, SCOPE)
    live_map = freshness.live_entries(vault_root, SCOPE)
    if live_map is None:
        return
    seen_map = store.seen_map(conn)
    live_rel: dict[str, str | None] = {}
    for key, signature in live_map.items():
        rel = _relative(vault_root, key)
        if rel is not None and eligible(vault_root, rel):
            live_rel[rel] = dreamer_store.encode_sig(signature)
    changed = [rel for rel, sig in live_rel.items() if seen_map.get(rel) != sig]
    deleted = [rel for rel in seen_map if rel not in live_rel]
    if not seen_map and changed:
        store.set_meta(conn, "reseeding", True)
    queued = sorted({*changed, *deleted})
    store.pending_add(conn, queued)
    store.set_meta(conn, "pending_checkpoint", _encode_checkpoint(captured))
    if not queued:
        advance_if_drained(store, conn)


def live_signature(vault_root: Path, rel_path: str) -> tuple[int, int, int] | None:
    """One page's live signature, keyed the way the registry keys it."""
    return freshness.live_signature(vault_root, SCOPE, Path(vault_root) / rel_path)


_UNSET = object()


def mark_processed(
    store: dreamer_store.DreamerStore,
    conn: sqlite3.Connection,
    vault_root: Path,
    rel_path: str,
    signature: object = _UNSET,
) -> tuple[int, int, int] | None:
    """Record one page as processed at the signature it was processed under.

    Pass the signature sampled BEFORE the page was read: a write that lands
    while it is processed then leaves `seen` older than the live map, and the
    next delta requeues the page. Omitted, the current live signature is used.
    Returns that signature, or None when the page is gone (its `seen` row is
    removed). Call inside the same transaction as the page's contribution, so
    a crash loses at most the page in flight.
    """
    if signature is _UNSET:
        signature = live_signature(vault_root, rel_path)
    if signature is None:
        store.seen_delete(conn, rel_path)
    else:
        store.seen_set(conn, rel_path, signature)
    store.pending_remove(conn, rel_path)
    return signature
