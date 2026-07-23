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
import uuid
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


class FreshnessCheckpoint(NamedTuple):
    """A consumer's resumption point in one live scope registry.

    `instance_id` pins the process-instance the checkpoint was minted against —
    a checkpoint from a prior process (restart) or another registry (`foreign`)
    can never yield a complete delta, because this process holds no event history
    bridging it. `generation` is the monotonic per-scope event counter; `triple`
    is the derived freshness key at that generation.
    """

    instance_id: str
    generation: int
    triple: tuple[int, int, str] | None


class ConsumerDelta(NamedTuple):
    """The atomic, target-state-coalesced change set between two checkpoints.

    `changed` (present at `to`) and `deleted` (absent at `to`) are duplicate-free
    and mutually disjoint: an edit-then-delete collapses to `deleted` only, a
    delete-then-recreate collapses to `changed` only, so apply order can neither
    resurrect a removed path nor drop a recreated one. `complete=False` means the
    registry cannot bridge `from_`→`to` from retained history (restart, foreign
    instance, drifted reconcile, over-old checkpoint, history overflow); such a
    response exposes NO partial suffix — `changed`/`deleted` are empty — so a
    consumer can never advance its authoritative checkpoint from it.

    `target_signatures` binds every `changed` path to the exact file signature
    present in the registry at `to`. A bounded consumer can therefore reject a
    later or unobserved filesystem edit instead of reading newer bytes while
    stamping the older target checkpoint.
    """

    from_: FreshnessCheckpoint
    to: FreshnessCheckpoint
    complete: bool
    changed: frozenset[str]
    deleted: frozenset[str]
    target_signatures: tuple[tuple[str, FileSignature], ...] = ()


# Bounded retained event history per live scope. Past this many batched events
# a checkpoint that predates the retained window can no longer be bridged and
# `delta_since` reports `complete=False` rather than a partial suffix. Kept
# module-level and test-adjustable; the trim below re-reads it on every append.
DELTA_HISTORY_LIMIT = 256

_lock = threading.RLock()
# (vault_root_str, scope) -> {absolute_path_str: (mtime_ns, ctime_ns, size)}
_maps: dict[tuple[str, str], dict[str, FileSignature]] = {}
# (vault_root_str, scope) -> cached derived triple (None = recompute on read)
_triples: dict[tuple[str, str], tuple[int, int, str] | None] = {}
# which (vault_root_str, scope) have been seeded and are being maintained
_live: set[tuple[str, str]] = set()
# process-instance id minted once per process; regenerated by `clear()` so a
# test's fresh state reads like a restart to any surviving foreign checkpoint.
_instance_id = uuid.uuid4().hex
# strictly-increasing global clock backing every scope's generation values.
_gen_clock = 0
# (vault_root_str, scope) -> current monotonic generation
_generations: dict[tuple[str, str], int] = {}
# (vault_root_str, scope) -> retained batched events, each (prev_gen, new_gen,
# paths_touched); the chain is contiguous, so a checkpoint is bridgeable iff its
# generation is >= the oldest retained event's `prev_gen`.
_history: dict[tuple[str, str], list[tuple[int, int, frozenset[str]]]] = {}


def _next_gen() -> int:
    """Allocate the next strictly-increasing generation value (call under lock)."""
    global _gen_clock
    _gen_clock += 1
    return _gen_clock


def _record_event(key: tuple[str, str], paths: set[str]) -> None:
    """Advance `key`'s generation past a batch and retain its touched paths.

    Call under `_lock`. Trims to `DELTA_HISTORY_LIMIT` re-reading the module
    constant each time so a test can tighten the window in place.
    """
    prev = _generations.get(key, 0)
    new = _next_gen()
    _generations[key] = new
    hist = _history.setdefault(key, [])
    hist.append((prev, new, frozenset(paths)))
    overflow = len(hist) - DELTA_HISTORY_LIMIT
    if overflow > 0:
        del hist[:overflow]


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
        # A seed is a fresh registry baseline: no consumer can bridge across it,
        # so the retained history starts empty at a new generation.
        _generations[key] = _next_gen()
        _history[key] = []


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
        # The map swap and the drift generation/history transition happen in ONE
        # critical section, so no reader (`delta_since`, `consumer_checkpoint`)
        # can ever observe the fresh, drifted map paired with the pre-drift
        # generation — which would let a missed event read as "no change" and
        # bless a stale consumer checkpoint. Both halves move together or not at
        # all.
        _maps[key] = fresh
        _triples[key] = None
        _live.add(key)
        if old is None:
            # First initialization of this scope from a walk. Like `seed`, this is
            # a fresh registry baseline that NO prior checkpoint can bridge across:
            # before it, the scope was non-live (generation 0, triple None). Mint a
            # new generation and empty history atomically, under the SAME lock as
            # the map install, so a pre-initialization checkpoint can never read as
            # a complete empty delta against the now-initialized corpus (which
            # would let a consumer bless an empty/stale catalog as complete). Prior
            # to this, `reconcile(old is None)` left the generation at 0 and the
            # `generation == generation` fast path in `delta_since` reported a
            # bogus complete no-change delta.
            _generations[key] = _next_gen()
            _history[key] = []
            return ReconcileDelta(drifted=False, changed=[], deleted=[])
        changed = [sp for sp, signature in fresh.items() if old.get(sp) != signature]
        deleted = [sp for sp in old if sp not in fresh]
        drifted = bool(changed or deleted)
        if drifted:
            # A drift means an event was lost, so the retained history has a hole
            # no coalesced delta can honestly bridge. Advance the generation and
            # drop the history under the same lock as the map swap: every prior
            # checkpoint now reads as incomplete rather than yielding a suffix
            # that silently omits the missed event.
            _generations[key] = _next_gen()
            _history[key] = []
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


def consumer_checkpoint(vault_root: Path, scope: str) -> FreshnessCheckpoint:
    """This process's current resumption point for a live scope.

    Names the process instance, the scope's current generation, and the derived
    triple at that generation — the immutable `{instance_id, generation, triple}`
    a consumer stores and later hands back to `delta_since`.
    """
    key = _key(vault_root, scope)
    with _lock:
        generation = _generations.get(key, 0)
        derived = triple(vault_root, scope)
        return FreshnessCheckpoint(_instance_id, generation, derived)


def delta_since(
    vault_root: Path, scope: str, checkpoint: FreshnessCheckpoint
) -> ConsumerDelta:
    """Atomic, non-destructive, target-state-coalesced delta from `checkpoint`.

    Captures a single target generation `to` and bridges `checkpoint`→`to` from
    retained history, classifying every touched path by its state at `to`:
    present ⇒ `changed`, absent ⇒ `deleted`. The two sets are duplicate-free and
    disjoint by construction, so apply order is irrelevant. Reads leave history
    intact (a later event stays discoverable from the same `to`). When the span
    cannot be honestly bridged — foreign/restarted instance, a checkpoint that
    predates retained history, or history overflow — returns `complete=False`
    with empty sets, never a partial suffix.
    """
    key = _key(vault_root, scope)
    with _lock:
        generation = _generations.get(key, 0)
        to = FreshnessCheckpoint(_instance_id, generation, triple(vault_root, scope))
        incomplete = ConsumerDelta(checkpoint, to, False, frozenset(), frozenset())

        # A checkpoint captured before this scope was ever live (non-live: triple
        # is None, generation 0) is NOT a complete empty baseline — nothing proves
        # the corpus was empty at that point, and initialization (`seed`/first
        # `reconcile`) mints a fresh generation no history bridges. Such a
        # checkpoint can never yield a complete delta; in particular the
        # `generation == generation` fast path below must not treat a triple-None,
        # gen-0 checkpoint as "already current". A live scope always derives a
        # non-None triple, so this rejects only genuine pre-initialization points.
        if checkpoint.triple is None:
            return incomplete
        # A checkpoint from another process/registry has no bridgeable history here.
        if checkpoint.instance_id != _instance_id:
            return incomplete
        # Same generation is complete only when the FULL checkpoint matches.
        # Generation alone cannot bless a malformed/misaligned triple with an
        # empty delta, which would advance stale consumer rows without replay.
        if checkpoint.generation == generation:
            if checkpoint != to:
                return incomplete
            return ConsumerDelta(checkpoint, to, True, frozenset(), frozenset())
        if checkpoint.generation > generation:
            return incomplete

        hist = _history.get(key) or []
        # No retained events but the generation moved on (e.g. a drifted reconcile
        # cleared history): the gap is unbridgeable.
        if not hist:
            return incomplete
        # The oldest retained event bridges from its `prev_gen`; a checkpoint older
        # than that predates the window (overflow / over-old) and is incomplete.
        if checkpoint.generation < hist[0][0]:
            return incomplete

        touched: set[str] = set()
        for _prev, new, paths in hist:
            if new > checkpoint.generation:
                touched |= paths
        m = _maps.get(key, {})
        changed = frozenset(sp for sp in touched if sp in m)
        deleted = frozenset(sp for sp in touched if sp not in m)
        target_signatures = tuple(sorted((sp, m[sp]) for sp in changed))
        return ConsumerDelta(
            checkpoint, to, True, changed, deleted, target_signatures
        )


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
        # Paths that actually mutated each scope's map, so the batch advances the
        # generation exactly once per scope and the retained event records the
        # target-state identities the delta will coalesce over.
        touched: dict[tuple[str, str], set[str]] = {}
        for sp, in_kb, in_vault in del_items:
            for scope, member in (("kb", in_kb), ("vault", in_vault)):
                self_key = _key(vault_root, scope)
                if member and self_key in _live:
                    m = _maps.get(self_key)
                    if m is not None and m.pop(sp, None) is not None:
                        _triples[self_key] = None
                        touched.setdefault(self_key, set()).add(sp)
        for sp, signature, in_kb, in_vault in chg_items:
            for scope, member in (("kb", in_kb), ("vault", in_vault)):
                self_key = _key(vault_root, scope)
                if not (member and self_key in _live):
                    continue
                m = _maps.setdefault(self_key, {})
                if signature is None:
                    if m.pop(sp, None) is not None:
                        _triples[self_key] = None
                        touched.setdefault(self_key, set()).add(sp)
                elif m.get(sp) != signature:
                    m[sp] = signature
                    _triples[self_key] = None
                    touched.setdefault(self_key, set()).add(sp)
        for self_key, paths in touched.items():
            _record_event(self_key, paths)


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
            _generations.clear()
            _history.clear()
            return
        root = _canon(vault_root)
        for scope in SCOPES:
            key = (root, scope)
            _maps.pop(key, None)
            _triples.pop(key, None)
            _live.discard(key)
            _generations.pop(key, None)
            _history.pop(key, None)


def rebaseline(vault_root: Path) -> dict[str, bool]:
    """Install exact final on-disk baselines for each event-maintained scope.

    Each scope is independent: a failed walk leaves only that scope non-live so
    the watcher's next periodic reconcile can initialize it without fanout.
    """
    invalidate(vault_root)
    result = {scope: False for scope in SCOPES}
    if not event_indexes_enabled():
        return result

    from . import find as find_module
    from .vault import walk_vault_md

    for scope in SCOPES:
        try:
            if scope == "kb":
                root = vault_root / kb_dirname()
                paths = find_module._walk_md(root) if root.is_dir() else ()
            else:
                paths = walk_vault_md(vault_root)
            seed(
                vault_root,
                scope,
                ((str(path), stat_signature(path)) for path in paths),
            )
            result[scope] = True
        except Exception:  # noqa: BLE001 - periodic reconcile safely retries
            log.exception(
                "freshness rebaseline failed; scope remains non-live: %s scope=%s",
                vault_root,
                scope,
            )
    return result


def snapshot() -> dict:
    """Diagnostics: live scopes and their file counts."""
    with _lock:
        return {
            "live": sorted(_live),
            "counts": {k: len(v) for k, v in _maps.items()},
        }


def clear() -> None:
    """Test hook: return to the never-seeded state.

    Mints a fresh process-instance id so any checkpoint held across the reset
    reads as foreign — the same signal a genuine process restart would give.
    """
    global _instance_id
    invalidate(None)
    with _lock:
        _instance_id = uuid.uuid4().hex
