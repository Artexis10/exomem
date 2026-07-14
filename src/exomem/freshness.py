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
over exactly the same `(absolute_path, mtime_ns, ctime_ns, size)` records the
walk would produce — and `scopes_for` applies exactly the same inclusion rules the two
walks apply (`find.EXCLUDED_DIR_NAMES` for kb, `vault.VAULT_SCAN_SKIP_DIRS` for
vault; `.md`-only; sync-conflict duplicates excluded). A registry that included
one extra file or directory would silently diverge from the walk it stands in
for, so the equality is pinned by tests across create/modify/delete/move/rename.

Canonicalization contract (event side only — see #126): the walk side
(`seed`/`reconcile`, fed by `walk_vault_md`/`_walk_md`'s `iterdir()`) always
yields a file's long-form name — the OS directory listing never returns an
8.3 short alias unless specifically asked. The EVENT side (`on_files_changed`,
fed by watchdog callbacks and self-write registrations) has no such guarantee:
on Windows, a file whose basename is long enough to earn an 8.3 short alias
(e.g. a long slug like `real-vault-...-by-d.md` → `REAL-V~1.MD`) can be
reported by an event under either form. Two string forms of the SAME file
would otherwise coexist as two separate keys in `_maps`; the next `reconcile()`
then reads that as "one file deleted, one file created", and any consumer
keyed on that identity (the wikilink resolver, the inbound-link index) drops
the file's entry until it's touched again — exactly the false "does not
resolve to any file in the vault" writer warning #126 reported.

`on_files_changed` is the single ingress point for EVENT-derived keys (its
only two callers — the watcher's debounced batch flush and the self-write
publish path — both funnel through it), so it canonicalizes there: each event
path is `resolve()`d and rejoined onto the literal `vault_root` prefix before
becoming a map key, so an 8.3 short segment expands to the long form the walk
side would have produced for the SAME still-existing file. For an already
DELETED path, `resolve()` can't query a vanished directory entry, so it can't
expand the leaf; canonicalization falls back to the best-effort partial result
(or the raw form, if `resolve()` itself raises). Any resulting stale "ghost"
key is not data loss — the 300s periodic `reconcile()` re-walks from disk and
replaces the map wholesale, so a leftover short-form key from a delete self-
heals on the very next cycle. Cost: this adds one `resolve()` call per
debounced EVENT file (a handful per batch), not per walk entry — negligible
next to the O(vault) walk the registry exists to avoid.

Pure substrate: mechanical file-change bookkeeping, no reasoning over content.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from .kbdir import kb_dirname

log = logging.getLogger(__name__)

SCOPES = ("kb", "vault")


class ReconcileDelta(NamedTuple):
    """What `reconcile` found: whether the map drifted, and the exact delta.

    `changed` (created/modified) and `deleted` are absolute path strings — the
    registry map's own keys — so the caller can dispatch precisely the paths a
    missed watchdog event left stale through the same event fan-out a live batch
    uses, healing the derived indexes off the query path instead of letting them
    rebuild lazily on the next `find`.
    """

    drifted: bool
    changed: list[str]
    deleted: list[str]


FileSignature = tuple[int, int, int]
SignatureLike = int | FileSignature

_lock = threading.RLock()
# (vault_root_str, scope) -> {absolute_path_str: (mtime_ns, ctime_ns, size)}
_maps: dict[tuple[str, str], dict[str, FileSignature]] = {}
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


def stat_signature(path: Path) -> FileSignature:
    """Shared file-change signature for corpus and parsed-page caches."""

    return signature_from_stat(path.stat())


def signature_from_stat(st: os.stat_result) -> FileSignature:
    """Build the shared signature from an already-fetched stat result."""

    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _normalize_signature(value: SignatureLike) -> FileSignature:
    # Keep the public registry seam compatible with older callers while all
    # production publishers use the full signature.
    if isinstance(value, int):
        return (value, 0, 0)
    return (int(value[0]), int(value[1]), int(value[2]))


def triple_from_entries(
    entries: Iterable[tuple[str, SignatureLike]],
) -> tuple[int, int, str]:
    """`(count, max_mtime_ns, digest)` for path + metadata-signature pairs.

    The single source of truth for the freshness digest — `find._walk_freshness_key`
    collects pairs via `stat()` and calls this; the registry calls it over its
    in-memory map. Digest-strength: the sorted path+metadata hash catches
    delete-paired-with-create, renames, and content replacements that preserve
    mtimes and would otherwise leave warmed recall stale.
    """
    items = sorted((sp, _normalize_signature(signature)) for sp, signature in entries)
    latest = 0
    h = hashlib.blake2b(digest_size=16)
    for sp, signature in items:
        if signature[0] > latest:
            latest = signature[0]
        h.update(sp.encode("utf-8", "surrogatepass"))
        h.update(b"\0")
        h.update(":".join(str(part) for part in signature).encode("ascii"))
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


def seed(vault_root: Path, scope: str, entries: Iterable[tuple[str, SignatureLike]]) -> None:
    """Install the full `(path_str, signature)` set for a scope and mark it live.

    Called once per scope at watcher start (from `warm_all`), and by the
    periodic reconcile. Entries must be produced by the SAME walk the fallback
    uses, so the live triple equals the walk triple on an unchanged tree.
    """
    key = _key(vault_root, scope)
    with _lock:
        _maps[key] = {sp: _normalize_signature(signature) for sp, signature in entries}
        _triples[key] = None
        _live.add(key)


def reconcile(
    vault_root: Path, scope: str, entries: Iterable[tuple[str, SignatureLike]]
) -> ReconcileDelta:
    """Replace the map from a fresh walk; return the drift delta.

    The 300s safety net for a missed watchdog event: the walk's result wins.
    A drift means an event was lost between reconciles — logged for visibility,
    never silently dropped. The returned `ReconcileDelta` carries the exact
    changed/deleted paths (this function holds both the old map and the fresh
    walk) so the caller can dispatch them through the event fan-out; the map is
    always fully replaced regardless of what the caller does with the delta.
    """
    key = _key(vault_root, scope)
    fresh = {sp: _normalize_signature(signature) for sp, signature in entries}
    with _lock:
        old = _maps.get(key)
        _maps[key] = fresh
        _triples[key] = None
        _live.add(key)
    old = old or {}
    changed = [sp for sp, signature in fresh.items() if old.get(sp) != signature]
    deleted = [sp for sp in old if sp not in fresh]
    drifted = bool(changed or deleted)
    if drifted:
        log.warning(
            "freshness_reconcile_drift: %s scope=%s map re-derived from a fresh walk "
            "(a filesystem event was missed since the last reconcile): "
            "%d changed, %d deleted",
            vault_root,
            scope,
            len(changed),
            len(deleted),
        )
    return ReconcileDelta(drifted=drifted, changed=changed, deleted=deleted)


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


def live_entries(vault_root: Path, scope: str) -> dict[str, FileSignature] | None:
    """The live `{abs_path_str: signature}` map for a scope, or None when not live.

    Returns a copy so callers can diff without holding the lock. The lexical heal
    reads this instead of re-walking the filesystem: whenever a heal fires, this
    map is already current (the watcher or the 300s reconcile updated it — that's
    exactly why the sidecar's triple drifted), so re-statting the whole corpus is
    redundant. Not live (kill-switched, or a scope never seeded) → None, and the
    caller falls back to a fresh walk.
    """
    if not event_indexes_enabled():
        return None
    key = _key(vault_root, scope)
    with _lock:
        if key not in _live:
            return None
        return dict(_maps.get(key, {}))


def _canonicalize_event_path(vault_root: Path, vr: Path, p: Path) -> Path:
    """Best-effort long-form path for an EVENT-derived filesystem change.

    See the module docstring's "Canonicalization contract" for the full
    rationale. `resolve()` expands an 8.3 short segment to its long form when
    the underlying directory entry still exists; the result is re-rooted onto
    the literal `vault_root` (not `vr`, its resolved form) so the reconstructed
    key shares the exact prefix the walk side's `iterdir()`-built keys use —
    resolving only the sub-path relative to `vr` avoids introducing a NEW
    mismatch class for vaults where `vault_root` itself isn't already in
    resolved form. Falls back to `p` unchanged when `resolve()`/`relative_to`
    can't establish that relationship (e.g. a deleted path's vanished leaf
    segment can't be queried, or `vault_root` is momentarily unreachable) —
    the raw form is exactly today's behavior, healed by the next reconcile.
    """
    try:
        rel = p.resolve().relative_to(vr)
    except (OSError, ValueError):
        return p
    return vault_root / rel


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

    Every path is canonicalized first (see the module docstring's
    "Canonicalization contract") so an event-derived path never produces a
    map key that diverges from the walk side's for the same file.
    """
    if not event_indexes_enabled():
        return
    try:
        vr = vault_root.resolve()
    except OSError:
        vr = vault_root
    # Phase 1 — classify + stat OUTSIDE the lock. `scopes_for` is stat-free;
    # only changed paths stat. A big external burst (a git pull / Obsidian Sync
    # landing hundreds of files) must not stat under the lock, or it would block
    # every concurrent find's triple() reader. Slightly staler stats are fine —
    # the 300s reconcile heals them, and a self-write also publishes here.
    del_items: list[tuple[str, bool, bool]] = []
    for path in deleted:
        p = _canonicalize_event_path(vault_root, vr, Path(path))
        in_kb, in_vault = scopes_for(vault_root, p)
        if in_kb or in_vault:
            del_items.append((str(p), in_kb, in_vault))
    chg_items: list[tuple[str, FileSignature | None, bool, bool]] = []
    for path in changed:
        p = _canonicalize_event_path(vault_root, vr, Path(path))
        in_kb, in_vault = scopes_for(vault_root, p)
        if not (in_kb or in_vault):
            continue
        try:
            signature: FileSignature | None = stat_signature(p)
        except OSError:
            signature = None  # created then gone before we could stat — treat as absent
        chg_items.append((str(p), signature, in_kb, in_vault))
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
        for sp, signature, in_kb, in_vault in chg_items:
            for scope, member in (("kb", in_kb), ("vault", in_vault)):
                self_key = _key(vault_root, scope)
                if not (member and self_key in _live):
                    continue
                m = _maps.setdefault(self_key, {})
                if signature is None:
                    if m.pop(sp, None) is not None:
                        _triples[self_key] = None
                elif m.get(sp) != signature:
                    m[sp] = signature
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
