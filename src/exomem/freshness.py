"""Event-maintained markdown freshness registry (OpenSpec: event-maintained-indexes).

`find` builds a digest-strength freshness key — `(count, max_mtime_ns, digest)` —
to key its hot result cache and to decide whether BM25 / the wikilink resolver /
the inbound index need rebuilding. Computing that key used to mean a full
stat-walk of the markdown tree on every request (~494ms on a ~1900-file vault),
paid even on a cache HIT because the key IS the cache key.

This registry maintains the same triple incrementally: seeded once by a full
walk at startup, then patched by the file watcher and the in-process writers as
files change. `find` reads the derived triple in sub-millisecond time with zero
syscalls whenever the registry is live for that `(vault_root, scope)`; when it is
NOT live (no watcher, CLI process, kill switch), callers fall back to the walk
and get a byte-identical triple.

Parity is guaranteed by construction: the digest is computed by the SINGLE
shared `triple_from_entries` helper below (also used by `find._walk_freshness_key`),
over exactly the same `(str(absolute_path), st_mtime_ns)` pairs the walk would
produce — and `scopes_for` applies exactly the same inclusion rules the two
walks apply (`find.EXCLUDED_DIR_NAMES` for kb, `vault.VAULT_SCAN_SKIP_DIRS` for
vault; `.md`-only; sync-conflict duplicates excluded). A registry that included
one extra file or directory would silently diverge from the walk it stands in
for, so the equality is pinned by tests across create/modify/delete/move/rename.

Pure substrate: mechanical file-change bookkeeping, no reasoning over content.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Iterable
from pathlib import Path

from .kbdir import kb_dirname

log = logging.getLogger(__name__)

SCOPES = ("kb", "vault")

_lock = threading.RLock()
# (vault_root_str, scope) -> {absolute_path_str: mtime_ns}
_maps: dict[tuple[str, str], dict[str, int]] = {}
# (vault_root_str, scope) -> cached derived triple (None = recompute on read)
_triples: dict[tuple[str, str], tuple[int, int, str] | None] = {}
# which (vault_root_str, scope) have been seeded and are being maintained
_live: set[tuple[str, str]] = set()


def event_indexes_enabled() -> bool:
    """False when EXOMEM_DISABLE_EVENT_INDEXES is set — the single rollback
    lever that reverts freshness, matrix, and inbound to their polling behavior."""
    return not _truthy(os.environ.get("EXOMEM_DISABLE_EVENT_INDEXES"))


def _truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}


def triple_from_entries(entries: Iterable[tuple[str, int]]) -> tuple[int, int, str]:
    """`(count, max_mtime_ns, digest)` for `(path_str, mtime_ns)` pairs.

    The single source of truth for the freshness digest — `find._walk_freshness_key`
    collects pairs via `stat()` and calls this; the registry calls it over its
    in-memory map. Digest-strength: the sorted path+mtime hash catches
    delete-paired-with-create, renames (mtime preserved), and backdated
    replacements that count/max-mtime alone would miss.
    """
    items = sorted(entries)
    latest = 0
    h = hashlib.blake2b(digest_size=16)
    for sp, ns in items:
        if ns > latest:
            latest = ns
        h.update(sp.encode("utf-8", "surrogatepass"))
        h.update(b"\0")
        h.update(str(ns).encode("ascii"))
        h.update(b"\0")
    return len(items), latest, h.hexdigest()


def scopes_for(vault_root: Path, path: Path) -> tuple[bool, bool]:
    """`(in_kb, in_vault)` — does `path` belong in each scope's freshness map?

    Mirrors the two walks exactly: `.md` only, sync-conflict duplicates
    excluded, and no ancestor directory (relative to the scope root) in that
    scope's skip set. Stat-free, so it works for already-deleted paths.
    """
    from .find import EXCLUDED_DIR_NAMES
    from .vault import VAULT_SCAN_SKIP_DIRS

    if path.suffix.lower() != ".md" or ".sync-conflict-" in path.name:
        return (False, False)

    try:
        vault_parts = path.relative_to(vault_root).parts
    except ValueError:
        return (False, False)
    in_vault = not any(d in VAULT_SCAN_SKIP_DIRS for d in vault_parts[:-1])

    in_kb = False
    try:
        kb_parts = path.relative_to(vault_root / kb_dirname()).parts
        in_kb = not any(d in EXCLUDED_DIR_NAMES for d in kb_parts[:-1])
    except ValueError:
        in_kb = False
    return (in_kb, in_vault)


def _key(vault_root: Path, scope: str) -> tuple[str, str]:
    return (_canon(vault_root), scope)


def _canon(vault_root: Path) -> str:
    try:
        return str(vault_root.resolve())
    except OSError:
        return str(vault_root)


def is_live(vault_root: Path, scope: str) -> bool:
    """True when this `(vault_root, scope)` is seeded and being maintained."""
    if not event_indexes_enabled():
        return False
    with _lock:
        return _key(vault_root, scope) in _live


def seed(vault_root: Path, scope: str, entries: Iterable[tuple[str, int]]) -> None:
    """Install the full `(path_str, mtime_ns)` set for a scope and mark it live.

    Called once per scope at watcher start (from `warm_all`), and by the
    periodic reconcile. Entries must be produced by the SAME walk the fallback
    uses, so the live triple equals the walk triple on an unchanged tree.
    """
    key = _key(vault_root, scope)
    with _lock:
        _maps[key] = {sp: ns for sp, ns in entries}
        _triples[key] = None
        _live.add(key)


def reconcile(vault_root: Path, scope: str, entries: Iterable[tuple[str, int]]) -> bool:
    """Replace the map from a fresh walk; return True if it drifted.

    The 300s safety net for a missed watchdog event: the walk's result wins.
    A drift means an event was lost between reconciles — logged for visibility,
    never silently dropped.
    """
    key = _key(vault_root, scope)
    fresh = {sp: ns for sp, ns in entries}
    with _lock:
        drifted = _maps.get(key) != fresh
        _maps[key] = fresh
        _triples[key] = None
        _live.add(key)
    if drifted:
        log.warning(
            "freshness_reconcile_drift: %s scope=%s map re-derived from a fresh walk "
            "(a filesystem event was missed since the last reconcile)",
            vault_root, scope,
        )
    return drifted


def triple(vault_root: Path, scope: str) -> tuple[int, int, str] | None:
    """The derived `(count, max_mtime_ns, digest)` when live, else None.

    Cached and invalidated on map mutation, so repeated `find` calls between
    file changes pay the hash once, not per request.
    """
    if not event_indexes_enabled():
        return None
    key = _key(vault_root, scope)
    with _lock:
        if key not in _live:
            return None
        cached = _triples.get(key)
        if cached is None:
            cached = triple_from_entries(_maps.get(key, {}).items())
            _triples[key] = cached
        return cached


def on_files_changed(
    vault_root: Path,
    changed: Iterable[Path] = (),
    deleted: Iterable[Path] = (),
) -> None:
    """Patch the live scope maps for a batch of filesystem changes.

    `changed` = created/modified paths (re-stat for the new mtime), `deleted` =
    removed paths (drop from the maps). Only live scopes are touched; a scope
    that was never seeded stays not-live and keeps falling back to the walk.
    Classification is stat-free, so a path that vanished between the event and
    here is still correctly removed.
    """
    if not event_indexes_enabled():
        return
    # Phase 1 — classify + stat OUTSIDE the lock. `scopes_for` is stat-free;
    # only changed paths stat. A big external burst (a git pull / Obsidian Sync
    # landing hundreds of files) must not stat under the lock, or it would block
    # every concurrent find's triple() reader. Slightly staler stats are fine —
    # the 300s reconcile heals them, and a self-write also publishes here.
    del_items: list[tuple[str, bool, bool]] = []
    for path in deleted:
        p = Path(path)
        in_kb, in_vault = scopes_for(vault_root, p)
        if in_kb or in_vault:
            del_items.append((str(p), in_kb, in_vault))
    chg_items: list[tuple[str, int | None, bool, bool]] = []
    for path in changed:
        p = Path(path)
        in_kb, in_vault = scopes_for(vault_root, p)
        if not (in_kb or in_vault):
            continue
        try:
            ns: int | None = p.stat().st_mtime_ns
        except OSError:
            ns = None  # created then gone before we could stat — treat as absent
        chg_items.append((str(p), ns, in_kb, in_vault))
    if not (del_items or chg_items):
        return

    # Phase 2 — apply the map mutations under the lock (no syscalls).
    with _lock:
        if not _live:
            return
        for sp, in_kb, in_vault in del_items:
            for scope, member in (("kb", in_kb), ("vault", in_vault)):
                self_key = _key(vault_root, scope)
                if member and self_key in _live:
                    m = _maps.get(self_key)
                    if m is not None and m.pop(sp, None) is not None:
                        _triples[self_key] = None
        for sp, ns, in_kb, in_vault in chg_items:
            for scope, member in (("kb", in_kb), ("vault", in_vault)):
                self_key = _key(vault_root, scope)
                if not (member and self_key in _live):
                    continue
                m = _maps.setdefault(self_key, {})
                if ns is None:
                    if m.pop(sp, None) is not None:
                        _triples[self_key] = None
                elif m.get(sp) != ns:
                    m[sp] = ns
                    _triples[self_key] = None


def invalidate(vault_root: Path | None = None) -> None:
    """Drop the registry back to not-live for a vault (or all vaults).

    Called at the end of `reconcile` (the heal-my-drift command) so a
    post-reconcile process re-seeds cleanly rather than trusting stale state.
    """
    with _lock:
        if vault_root is None:
            _maps.clear()
            _triples.clear()
            _live.clear()
            return
        root = _canon(vault_root)
        for scope in SCOPES:
            key = (root, scope)
            _maps.pop(key, None)
            _triples.pop(key, None)
            _live.discard(key)


def snapshot() -> dict:
    """Diagnostics: live scopes and their file counts."""
    with _lock:
        return {
            "live": sorted(_live),
            "counts": {k: len(v) for k, v in _maps.items()},
        }


def clear() -> None:
    """Test hook: return to the never-seeded state."""
    invalidate(None)
