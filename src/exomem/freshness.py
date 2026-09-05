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
import ntpath
import os
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, overload

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
    # A walk superseded by invalidation did not establish a new baseline and
    # must not acknowledge an external event merely because it found no drift.
    published: bool = True


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


class RecallFreshnessCheckpoint(NamedTuple):
    """Independent event stream for the policy-projected recall corpus."""

    instance_id: str
    generation: int
    triple: tuple[int, int, str]
    policy_version: str
    access_policy_fingerprint: str


class RecallProjectionUnavailable(RuntimeError):
    """A caller requiring maintained projection authority found none."""


@dataclass(frozen=True, slots=True)
class RecallPublicationState:
    """Immutable prepared recall identity for bounded sidecar publication."""

    checkpoint: RecallFreshnessCheckpoint
    registry_key: tuple[str, str]

    @property
    def triple(self) -> tuple[int, int, str]:
        return self.checkpoint.triple

    @property
    def policy_version(self) -> str:
        return self.checkpoint.policy_version

    @property
    def access_policy_fingerprint(self) -> str:
        return self.checkpoint.access_policy_fingerprint


class RecallDelta(NamedTuple):
    from_: RecallFreshnessCheckpoint
    to: RecallFreshnessCheckpoint
    complete: bool
    changed: frozenset[str]
    deleted: frozenset[str]
    target_signatures: tuple[tuple[str, FileSignature], ...] = ()
    requires_source_proof: bool = False


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
# Policy-projected recall membership.  It is separate from broad identity
# freshness so raw Records edits do not churn ordinary-recall state.
_recall_maps: dict[tuple[str, str], dict[str, FileSignature]] = {}
_recall_triples: dict[tuple[str, str], tuple[int, int, str] | None] = {}
_recall_generations: dict[tuple[str, str], int] = {}
_recall_identities: dict[tuple[str, str], tuple[str, str]] = {}
_recall_live: set[tuple[str, str]] = set()
_recall_history: dict[
    tuple[str, str], list[tuple[int, int, frozenset[str], bool]]
] = {}
_recall_publications: dict[tuple[str, str], RecallPublicationState] = {}
RECALL_PUBLICATION_PREPARE_ATTEMPTS = 4
# Full replacement walks run without the registry lock.  While one is active,
# event publications retain their target states here so the eventual map swap
# cannot overwrite a self-write or watcher batch that landed after enumeration
# passed that path.  A per-scope lock serializes competing replacement walks;
# readers and incremental events never take it.
_replacement_locks: dict[tuple[str, str], threading.Lock] = {}
# Every replacement receives an epoch after it acquires the per-scope lock.
# Invalidation advances that epoch while a replacement is still walking, so a
# seed abandoned by startup admission cannot publish authority if its iterator
# eventually returns.
_replacement_epochs: dict[tuple[str, str], int] = {}
_replacement_active: dict[tuple[str, str], int] = {}
_replacement_pending: dict[
    tuple[str, str], dict[str, tuple[FileSignature | None, bool]]
] = {}
# Watchdog records an external event here before its debounce window. Graph
# readers can then fail closed in O(1) until that exact queued generation has
# been published through the event-maintained corpus fan-out.
_external_pending_clock = 0
_external_pending: dict[str, int] = {}


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


def _record_recall_event(
    key: tuple[str, str],
    paths: set[str],
    *,
    requires_source_proof: bool = False,
) -> None:
    previous = _recall_generations.get(key, 0)
    current = _next_gen()
    _recall_generations[key] = current
    history = _recall_history.setdefault(key, [])
    history.append((previous, current, frozenset(paths), requires_source_proof))
    overflow = len(history) - DELTA_HISTORY_LIMIT
    if overflow > 0:
        del history[:overflow]


def _begin_replacement(key: tuple[str, str]) -> tuple[threading.Lock, int]:
    """Fence events while one scope's replacement walk runs off-lock."""
    with _lock:
        replacement_lock = _replacement_locks.setdefault(key, threading.Lock())
    replacement_lock.acquire()
    with _lock:
        epoch = _replacement_epochs.get(key, 0) + 1
        _replacement_epochs[key] = epoch
        _replacement_active[key] = epoch
        _replacement_pending[key] = {}
    return replacement_lock, epoch


def _finish_replacement(
    key: tuple[str, str], replacement_lock: threading.Lock, epoch: int
) -> None:
    with _lock:
        if _replacement_active.get(key) == epoch:
            _replacement_active.pop(key, None)
            _replacement_pending.pop(key, None)
    replacement_lock.release()


def _replacement_is_current(key: tuple[str, str], epoch: int) -> bool:
    """Whether an off-lock replacement still owns publication authority."""
    return (
        _replacement_active.get(key) == epoch
        and _replacement_epochs.get(key) == epoch
    )


def _merge_replacement_pending(
    key: tuple[str, str],
    broad: dict[str, FileSignature],
    recall: dict[str, FileSignature],
) -> None:
    """Merge events observed since a replacement walk began (call under lock)."""
    for path, (signature, is_recall) in _replacement_pending.get(key, {}).items():
        if signature is None:
            broad.pop(path, None)
            recall.pop(path, None)
            continue
        broad[path] = signature
        if is_recall:
            recall[path] = signature
        else:
            recall.pop(path, None)


def event_indexes_enabled() -> bool:
    """False when EXOMEM_DISABLE_EVENT_INDEXES is set — the single rollback
    lever that reverts freshness, matrix, and inbound to their polling behavior."""
    return not _truthy(os.environ.get("EXOMEM_DISABLE_EVENT_INDEXES"))


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() not in {"", "0", "false", "no", "off"}


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


def _digest_path(path: str) -> str:
    """Canonicalize Windows aliases without changing non-Windows digest inputs."""
    if os.name != "nt":
        return path
    try:
        return ntpath.normcase(str(Path(path).resolve()))
    except OSError:
        return ntpath.normcase(path)


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
    items = sorted((_digest_path(sp), _normalize_signature(signature)) for sp, signature in entries)
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


def recall_triple(vault_root: Path, scope: str) -> tuple[int, int, str]:
    """Freshness triple over recall-eligible pages only.

    This deliberately does not use the generic event map: generic freshness
    remains authoritative for stable identity and must observe direct edits to
    structured Record sources.  Recall excludes those sources by policy.
    """
    from . import recall_policy

    root = Path(vault_root)
    key = _key(root, scope)
    with _lock:
        if event_indexes_enabled() and key in _recall_live:
            cached = _recall_triples.get(key)
            if cached is None:
                cached = triple_from_entries(_recall_maps.get(key, {}).items())
                _recall_triples[key] = cached
            return cached
    from . import find as find_module
    if scope == "vault":
        from .vault import walk_vault_md

        walk = walk_vault_md(root)
    else:
        kb = root / kb_dirname()
        walk = find_module._walk_md(kb) if kb.is_dir() else ()
    entries: list[tuple[str, FileSignature]] = []
    for path in recall_policy.iter_recall_markdown(root, walk):
        try:
            entries.append((str(path), stat_signature(path)))
        except OSError:
            continue
    return triple_from_entries(entries)


@overload
def recall_checkpoint(
    vault_root: Path, scope: str, *, max_attempts: None = None
) -> RecallFreshnessCheckpoint: ...


@overload
def recall_checkpoint(
    vault_root: Path, scope: str, *, max_attempts: int
) -> RecallFreshnessCheckpoint | None: ...


def recall_checkpoint(
    vault_root: Path, scope: str, *, max_attempts: int | None = None
) -> RecallFreshnessCheckpoint | None:
    """Return one coherent recall-projection and policy identity checkpoint.

    Ordinary callers retain the established unbounded convergence behavior.
    Bounded publishers can opt into a finite retry budget so policy churn cannot
    keep a publication authority wait forever.
    """
    from . import recall_policy

    if max_attempts is not None and max_attempts < 1:
        return None
    root = Path(vault_root)
    key = _key(root, scope)
    attempts = 0
    while max_attempts is None or attempts < max_attempts:
        attempts += 1
        policy_version, access_fingerprint = recall_policy.recall_policy_identity(root)
        identity = (policy_version, access_fingerprint)
        with _lock:
            live = event_indexes_enabled() and key in _recall_live
            if live and _recall_identities.get(key) == identity:
                triple = _recall_triples.get(key)
                if triple is None:
                    triple = triple_from_entries(_recall_maps.get(key, {}).items())
                    _recall_triples[key] = triple
                generation = _recall_generations.get(key, 0)
                return RecallFreshnessCheckpoint(
                    _instance_id,
                    generation,
                    triple,
                    policy_version,
                    access_fingerprint,
                )
            if live:
                broad_generation = _generations.get(key, 0)
                broad_entries = dict(_maps.get(key, {}))

        if not live:
            # The policy shapes the cold walk.  Do not label its result with an
            # identity that changed while candidates were being admitted.
            triple = recall_triple(root, scope)
            if recall_policy.recall_policy_identity(root) != identity:
                continue
            with _lock:
                # A watcher may have become authoritative while the walk ran.
                if event_indexes_enabled() and key in _recall_live:
                    continue
                instance_id = _instance_id
                generation = _recall_generations.get(key, 0)
            return RecallFreshnessCheckpoint(
                instance_id,
                generation,
                triple,
                policy_version,
                access_fingerprint,
            )

        # Content reads are deliberately outside the global freshness lock.
        projected = {
            path: signature
            for path, signature in broad_entries.items()
            if recall_policy.is_recall_candidate(root, Path(path))
        }
        if recall_policy.recall_policy_identity(root) != identity:
            continue
        with _lock:
            if _generations.get(key, 0) != broad_generation:
                continue
            if _recall_identities.get(key) == identity:
                continue
            previous = _recall_maps.get(key, {})
            touched = {
                path
                for path in set(previous) | set(projected)
                if previous.get(path) != projected.get(path)
            }
            _recall_maps[key] = projected
            _recall_triples[key] = None
            _recall_identities[key] = identity
            _recall_publications.pop(key, None)
            # An access-policy transition is a real projected event even when
            # no Markdown changed.  Retain its exact row delta so a live catalog
            # can remove/re-add eligible pages without a request-path walk.
            _record_recall_event(key, touched)
            continue
    return None


def live_recall_checkpoint(
    vault_root: Path,
    scope: str,
) -> RecallFreshnessCheckpoint | None:
    """Return a current live checkpoint without walking or reprojecting.

    Access-policy reprojection can be O(corpus) and perform Windows path
    validation for every candidate.  That belongs to watcher/publication work,
    never a server reader.  A mismatched policy identity therefore returns
    ``None`` here and lets the caller decline while background repair converges.
    """
    from . import recall_policy

    root = Path(vault_root)
    key = _key(root, scope)
    identity = recall_policy.recall_policy_identity(root)
    with _lock:
        if (
            not event_indexes_enabled()
            or key not in _recall_live
            or _recall_identities.get(key) != identity
        ):
            return None
        triple = _recall_triples.get(key)
        if triple is None:
            triple = triple_from_entries(_recall_maps.get(key, {}).items())
            _recall_triples[key] = triple
        checkpoint = RecallFreshnessCheckpoint(
            _instance_id,
            _recall_generations.get(key, 0),
            triple,
            identity[0],
            identity[1],
        )
    if recall_policy.recall_policy_identity(root) != identity:
        return None
    return checkpoint


def recall_checkpoint_is_current(
    vault_root: Path,
    scope: str,
    expected: RecallFreshnessCheckpoint,
) -> bool:
    """Validate one exact managed proof without walking or reprojecting.

    The generation and policy identity are the authoritative O(1) identity of
    the projected map.  An observed-but-not-yet-dispatched filesystem event is
    also a mismatch: its generation has not advanced yet, but serving or
    publishing against the old proof during that debounce window is unsafe.
    """
    root = _canon(Path(vault_root))
    key = (root, scope)
    with _lock:
        if (
            not event_indexes_enabled()
            or root in _external_pending
            or key not in _recall_live
        ):
            return False
        triple = _recall_triples.get(key)
        if triple is None:
            triple = triple_from_entries(_recall_maps.get(key, {}).items())
            _recall_triples[key] = triple
        identity = _recall_identities.get(key)
        current = RecallFreshnessCheckpoint(
            _instance_id,
            _recall_generations.get(key, 0),
            triple,
            identity[0] if identity is not None else "",
            identity[1] if identity is not None else "",
        )
        return current == expected


def recall_projection_snapshot(
    vault_root: Path,
    scope: str,
    *,
    allow_fallback: bool = True,
) -> tuple[RecallFreshnessCheckpoint, dict[str, FileSignature]]:
    """Return one checkpoint-bound projected path map.

    Request-time semantic search needs both the projected freshness identity and
    the exact parent-path allowlist used before top-k ranking.  Obtaining those
    through ``recall_checkpoint()`` and a later walk both doubled cold-query
    cost and allowed the two observations to describe different generations.
    This function snapshots them together: live registries are copied under the
    registry lock, while cold callers perform one policy-projected stat walk.
    """
    from . import recall_policy

    root = Path(vault_root)
    key = _key(root, scope)
    if not allow_fallback:
        for _attempt in range(3):
            checkpoint = live_recall_checkpoint(root, scope)
            if checkpoint is None:
                break
            with _lock:
                if not event_indexes_enabled() or key not in _recall_live:
                    continue
                identity = _recall_identities.get(key)
                triple = _recall_triples.get(key)
                if triple is None:
                    triple = triple_from_entries(_recall_maps.get(key, {}).items())
                    _recall_triples[key] = triple
                current = RecallFreshnessCheckpoint(
                    _instance_id,
                    _recall_generations.get(key, 0),
                    triple,
                    identity[0] if identity is not None else "",
                    identity[1] if identity is not None else "",
                )
                entries = dict(_recall_maps.get(key, {}))
            if current == checkpoint:
                return current, entries
        raise RecallProjectionUnavailable(
            f"maintained recall projection is not live for scope={scope!r}"
        )
    while True:
        with _lock:
            live = event_indexes_enabled() and key in _recall_live
        if live:
            # Handles access-policy reprojection outside the registry lock.
            checkpoint = recall_checkpoint(root, scope)
            with _lock:
                if not event_indexes_enabled() or key not in _recall_live:
                    continue
                cached = _recall_triples.get(key)
                if cached is None:
                    cached = triple_from_entries(_recall_maps.get(key, {}).items())
                    _recall_triples[key] = cached
                identity = _recall_identities.get(key)
                current = RecallFreshnessCheckpoint(
                    _instance_id,
                    _recall_generations.get(key, 0),
                    cached,
                    identity[0] if identity is not None else "",
                    identity[1] if identity is not None else "",
                )
                entries = dict(_recall_maps.get(key, {}))
            if current != checkpoint:
                continue
            if recall_policy.recall_policy_identity(root) != (
                current.policy_version,
                current.access_policy_fingerprint,
            ):
                continue
            return current, entries

        cold = _cold_recall_projection_scope_snapshot(root, scope)
        if cold is None:
            continue
        checkpoint, projected_entries, _scope_triple = cold
        return checkpoint, projected_entries


def _cold_recall_projection_scope_snapshot(
    root: Path,
    scope: str,
) -> tuple[
    RecallFreshnessCheckpoint,
    dict[str, FileSignature],
    tuple[int, int, str],
] | None:
    """Build projected and generic scope identities from one cold stat walk.

    ``None`` means a watcher became authoritative before publication; callers
    retry against the live registry instead of publishing a stale cold view.
    """
    from . import recall_policy

    key = _key(root, scope)
    with _lock:
        if event_indexes_enabled() and key in _recall_live:
            return None
    identity = recall_policy.recall_policy_identity(root)
    if scope == "vault":
        from .vault import walk_vault_md

        walk = walk_vault_md(root)
    else:
        from . import find as find_module

        kb = root / kb_dirname()
        walk = find_module._walk_md(kb) if kb.is_dir() else ()
    scope_entries: dict[str, FileSignature] = {}
    projected_entries: dict[str, FileSignature] = {}
    for path in walk:
        admitted = recall_policy.is_recall_candidate(root, path)
        try:
            signature = stat_signature(path)
        except OSError:
            continue
        raw_path = str(path)
        scope_entries[raw_path] = signature
        if admitted:
            projected_entries[raw_path] = signature
    if recall_policy.recall_policy_identity(root) != identity:
        return None
    with _lock:
        if event_indexes_enabled() and key in _recall_live:
            return None
        instance_id = _instance_id
        generation = _recall_generations.get(key, 0)
    return (
        RecallFreshnessCheckpoint(
            instance_id,
            generation,
            triple_from_entries(projected_entries.items()),
            identity[0],
            identity[1],
        ),
        projected_entries,
        triple_from_entries(scope_entries.items()),
    )


def recall_pending_coverage(vault_root: Path):
    """Bounded pending-visibility coverage managed recall must prove before ready.

    The persistent projections above describe what the sidecars have published.
    A committed write whose derived components have not converged yet is covered
    instead by exact durable receipts, and recall may only be declared ready once
    that bounded set has been hydrated and fenced. This is the seam that answers
    it, kept beside the other recall projection identities so a consumer proves
    both through one module.

    Returns the typed coverage; ``ready`` means the whole outstanding set was
    proven, and every other outcome is the caller's cue to return its existing
    warming/temporarily-unavailable result rather than the last published
    catalogue.
    """
    from . import pending_recall

    return pending_recall.overlay(Path(vault_root))


def recall_projection_scope_snapshot(
    vault_root: Path,
    scope: str,
) -> tuple[
    RecallFreshnessCheckpoint,
    dict[str, FileSignature],
    tuple[int, int, str],
]:
    """Return projected paths and generic freshness from one scope snapshot.

    This is the offline ``find`` seam: a direct caller still gets an exact
    bounded fallback, but projected recall and generic cache identity share one
    filesystem walk instead of independently walking the same scope.
    """
    root = Path(vault_root)
    key = _key(root, scope)
    while True:
        with _lock:
            live = event_indexes_enabled() and key in _recall_live
        if live:
            checkpoint, projected_entries = recall_projection_snapshot(root, scope)
            with _lock:
                if not event_indexes_enabled() or key not in _recall_live:
                    continue
                identity = _recall_identities.get(key)
                projected_triple = _recall_triples.get(key)
                if projected_triple is None:
                    projected_triple = triple_from_entries(_recall_maps.get(key, {}).items())
                    _recall_triples[key] = projected_triple
                current = RecallFreshnessCheckpoint(
                    _instance_id,
                    _recall_generations.get(key, 0),
                    projected_triple,
                    identity[0] if identity is not None else "",
                    identity[1] if identity is not None else "",
                )
                scope_triple = _triples.get(key)
                if scope_triple is None:
                    scope_triple = triple_from_entries(_maps.get(key, {}).items())
                    _triples[key] = scope_triple
            if current == checkpoint:
                return checkpoint, projected_entries, scope_triple
            continue
        cold = _cold_recall_projection_scope_snapshot(root, scope)
        if cold is not None:
            return cold


def live_recall_entries(vault_root: Path, scope: str) -> dict[str, FileSignature] | None:
    """Projected live rows for lexical repair; never exposes broad raw Records."""
    # Refresh identity first: access-policy edits have no Markdown event.
    recall_checkpoint(vault_root, scope)
    with _lock:
        key = _key(vault_root, scope)
        if not event_indexes_enabled() or key not in _recall_live:
            return None
        return dict(_recall_maps.get(key, {}))


def recall_is_live(vault_root: Path, scope: str) -> bool:
    """Whether the projected recall stream is event-maintained (no walk)."""
    if not event_indexes_enabled():
        return False
    with _lock:
        return _key(vault_root, scope) in _recall_live


def prepare_recall_publication(
    vault_root: Path,
    scope: str,
    *,
    expected_policy_identity: tuple[str, str] | None = None,
) -> RecallPublicationState | None:
    """Materialize one publishable event-maintained recall checkpoint.

    Cold callers deliberately receive no state.  Policy reprojection runs here,
    outside the eventual publication authority; the hot publication path uses
    :func:`peek_recall_publication` only.
    """
    from . import recall_policy

    root = Path(vault_root)
    key = _key(root, scope)
    for _attempt in range(RECALL_PUBLICATION_PREPARE_ATTEMPTS):
        identity = recall_policy.recall_publication_policy_identity(root)
        if identity is None or (
            expected_policy_identity is not None and identity != expected_policy_identity
        ):
            return None
        with _lock:
            if (
                not event_indexes_enabled()
                or key not in _recall_live
                or _canon(root) in _external_pending
            ):
                return None
        checkpoint = recall_checkpoint(root, scope, max_attempts=1)
        if checkpoint is None:
            continue
        if (checkpoint.policy_version, checkpoint.access_policy_fingerprint) != identity:
            if expected_policy_identity is not None:
                return None
            continue
        if recall_policy.recall_publication_policy_identity(root) != identity:
            if expected_policy_identity is not None:
                return None
            continue
        with _lock:
            if (
                not event_indexes_enabled()
                or key not in _recall_live
                or _canon(root) in _external_pending
                or _recall_generations.get(key, 0) != checkpoint.generation
                or _recall_identities.get(key) != identity
            ):
                continue
            state = RecallPublicationState(checkpoint, key)
            _recall_publications[key] = state
            return state
    return None


def peek_recall_publication(
    vault_root: Path,
    scope: str,
    *,
    expected_policy_identity: tuple[str, str] | None = None,
    ticket: RecallPublicationState | None = None,
) -> RecallPublicationState | None:
    """Return prepared publication state without filesystem or policy work."""
    # Do not canonicalize here: resolve() would violate the strict hot-path
    # contract. A differently spelled root is conservatively cold.
    key = ticket.registry_key if ticket is not None else (str(Path(vault_root)), scope)
    with _lock:
        if (
            (ticket is not None and key[1] != scope)
            or not event_indexes_enabled()
            or key[0] in _external_pending
        ):
            return None
        state = _recall_publications.get(key)
        if ticket is not None and state != ticket:
            return None
        if state is None or key not in _recall_live:
            return None
        checkpoint = state.checkpoint
        if (
            _recall_generations.get(key, 0) != checkpoint.generation
            or _recall_identities.get(key)
            != (checkpoint.policy_version, checkpoint.access_policy_fingerprint)
            or _recall_triples.get(key) != checkpoint.triple
            or (
                expected_policy_identity is not None
                and expected_policy_identity
                != (checkpoint.policy_version, checkpoint.access_policy_fingerprint)
            )
        ):
            return None
        return state


def mark_external_pending(vault_root: Path) -> int:
    """Mark an observed out-of-band event pending before watcher debounce."""
    global _external_pending_clock
    with _lock:
        _external_pending_clock += 1
        epoch = _external_pending_clock
        root = _canon(vault_root)
        _external_pending[root] = epoch
        for scope in SCOPES:
            _recall_publications.pop((root, scope), None)
        return epoch


def clear_external_pending(vault_root: Path, *, through: int) -> None:
    """Clear only the observed external generations a completed flush covered."""
    with _lock:
        root = _canon(vault_root)
        current = _external_pending.get(root)
        if current is not None and current <= through:
            _external_pending.pop(root, None)


def external_pending(vault_root: Path) -> bool:
    """Whether watchdog has observed unpublished external vault changes."""
    with _lock:
        return _canon(vault_root) in _external_pending


def external_pending_epoch(vault_root: Path) -> int | None:
    """Return the latest observed unpublished generation for one vault."""
    with _lock:
        return _external_pending.get(_canon(vault_root))


def recall_delta_since(
    vault_root: Path, scope: str, checkpoint: RecallFreshnessCheckpoint
) -> RecallDelta:
    """Coalesced projected changes, refusing foreign/reprojected history."""
    current = recall_checkpoint(vault_root, scope)
    key = _key(vault_root, scope)
    with _lock:
        if checkpoint.instance_id != _instance_id or checkpoint.generation > current.generation:
            return RecallDelta(checkpoint, current, False, frozenset(), frozenset())
        if checkpoint.generation == current.generation:
            complete = checkpoint == current
            return RecallDelta(checkpoint, current, complete, frozenset(), frozenset())
        history = _recall_history.get(key, [])
        if not history or checkpoint.generation < history[0][0]:
            return RecallDelta(checkpoint, current, False, frozenset(), frozenset())
        changed: set[str] = set()
        deleted: set[str] = set()
        requires_source_proof = False
        for _before, _after, paths, event_requires_source_proof in history:
            if _after <= checkpoint.generation:
                continue
            requires_source_proof |= event_requires_source_proof
            for path in paths:
                if path in _recall_maps.get(key, {}):
                    changed.add(path)
                    deleted.discard(path)
                else:
                    deleted.add(path)
                    changed.discard(path)
        signatures = _recall_maps.get(key, {})
        return RecallDelta(
            checkpoint,
            current,
            True,
            frozenset(changed),
            frozenset(deleted),
            tuple(sorted((path, signatures[path]) for path in changed if path in signatures)),
            requires_source_proof,
        )


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


def _project_recall_entries(
    vault_root: Path, entries: Iterable[tuple[str, FileSignature]]
) -> tuple[dict[str, FileSignature], tuple[str, str]]:
    """Project entries against one stable recall-policy snapshot."""
    from . import recall_policy

    while True:
        identity = recall_policy.recall_policy_identity(vault_root)
        projected = {
            sp: signature
            for sp, signature in entries
            if recall_policy.is_recall_candidate(vault_root, Path(sp))
        }
        if recall_policy.recall_policy_identity(vault_root) == identity:
            return projected, identity


def seed(vault_root: Path, scope: str, entries: Iterable[tuple[str, SignatureLike]]) -> None:
    """Install the full `(path_str, signature)` set for a scope and mark it live.

    Called once per scope at watcher start (from `warm_all`), and by the
    periodic reconcile. Entries must be produced by the SAME walk the fallback
    uses, so the live triple equals the walk triple on an unchanged tree.
    """
    key = _key(vault_root, scope)
    replacement_lock, replacement_epoch = _begin_replacement(key)
    try:
        raw_entries = {
            sp: _normalize_signature(signature) for sp, signature in entries
        }
        recall_entries, recall_identity = _project_recall_entries(
            vault_root, raw_entries.items()
        )
        with _lock:
            if not _replacement_is_current(key, replacement_epoch):
                return
            _merge_replacement_pending(key, raw_entries, recall_entries)
            _maps[key] = raw_entries
            _triples[key] = None
            _live.add(key)
            # A seed is a fresh registry baseline: no consumer can bridge across it,
            # so the retained history starts empty at a new generation.
            _generations[key] = _next_gen()
            _history[key] = []
            _recall_maps[key] = recall_entries
            _recall_triples[key] = None
            _recall_generations[key] = _next_gen()
            _recall_identities[key] = recall_identity
            _recall_publications.pop(key, None)
            _recall_live.add(key)
            _recall_history[key] = []
    finally:
        _finish_replacement(key, replacement_lock, replacement_epoch)


def reconcile(
    vault_root: Path,
    scope: str,
    entries: Iterable[tuple[str, SignatureLike]],
    *,
    publication_guard: AbstractContextManager[object] | None = None,
) -> ReconcileDelta:
    """Replace the map from a fresh walk; return the drift delta.

    The 300s safety net for a missed watchdog event: the walk's result wins.
    A drift means an event was lost between reconciles — logged for visibility,
    never silently dropped. The returned `ReconcileDelta` carries the exact
    changed/deleted paths (this function holds both the old map and the fresh
    walk) so the caller can dispatch them through the event fan-out; the map is
    always fully replaced regardless of what the caller does with the delta.

    Production callers supply the canonical writer's publication guard. The
    walk and policy projection stay off-boundary; only the final map swap waits
    for an in-flight writer's event. Its retained target state then wins over
    any observation made between canonical replacement and event publication.
    """
    from . import recall_policy

    key = _key(vault_root, scope)
    replacement_lock, replacement_epoch = _begin_replacement(key)
    try:
        fresh = {sp: _normalize_signature(signature) for sp, signature in entries}
        recall_fresh, recall_identity = _project_recall_entries(vault_root, fresh.items())
        with (publication_guard if publication_guard is not None else nullcontext()), _lock:
            if (
                not _replacement_is_current(key, replacement_epoch)
                or recall_policy.recall_policy_identity(vault_root) != recall_identity
            ):
                return ReconcileDelta(drifted=False, changed=[], deleted=[], published=False)
            _merge_replacement_pending(key, fresh, recall_fresh)
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
            old_recall = _recall_maps.get(key)
            old_identity = _recall_identities.get(key)
            _recall_maps[key] = recall_fresh
            _recall_triples[key] = None
            _recall_identities[key] = recall_identity
            _recall_publications.pop(key, None)
            if old_recall is None or old_identity != recall_identity:
                # Initialization and policy transitions change the proof
                # authority.  No checkpoint from the prior projection may be
                # resumed through the new one.
                _recall_generations[key] = _next_gen()
                _recall_history[key] = []
            elif old_recall != recall_fresh:
                # The safety-net walk holds both complete same-policy maps, so
                # a missed filesystem event yields a BRIDGEABLE old/new map
                # diff — but never a TRUSTED one: the walk ran off-lock and is
                # not an atomic source snapshot, so a concurrent edit can leave
                # it mixing pre- and post-change observations. The event is
                # therefore recorded with ``requires_source_proof=True``: a
                # consumer may replay the exact diff, but must independently
                # re-prove the resulting target against the complete current
                # source before persisting (blessing) that checkpoint, and
                # request paths refuse it outright. Clearing the history here
                # instead used to strand an exact-current catalog behind a
                # stale persisted checkpoint and launch a whole-vault rebuild
                # on the next restart.
                recall_touched = {
                    path
                    for path in set(old_recall) | set(recall_fresh)
                    if old_recall.get(path) != recall_fresh.get(path)
                }
                _record_recall_event(
                    key,
                    recall_touched,
                    requires_source_proof=True,
                )
            _recall_live.add(key)
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
    finally:
        _finish_replacement(key, replacement_lock, replacement_epoch)
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
        # Drift is the receipt-less case by definition: the walk found a change
        # no governed write announced, so no path set is authoritative and the
        # scope is what gets invalidated. The delta is still dispatched by the
        # caller through the ordinary fan-out; this is the substrate caches'
        # half, and it is the ONE remaining role of a whole-scope key.
        invalidate_scope_for_drift(
            vault_root, scope=scope, reason="receiptless_drift"
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
    removed paths (drop from the maps). Live scopes are patched immediately;
    scopes in a replacement walk retain the target state for its final atomic
    swap. A scope that was never seeded and is not being seeded stays non-live
    and keeps falling back to the walk. Classification is stat-free, so a path
    that vanished between the event and here is still correctly removed.

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
    from . import recall_policy

    # Reproject policy-only changes before classifying an event; never merge
    # event admission judged under a new access snapshot into an old projection.
    # A non-live scope intentionally remains a polling/fallback caller: a
    # watcher event must not turn into an unexpected request-path corpus walk.
    with _lock:
        projected_live = {
            scope for scope in SCOPES if _key(vault_root, scope) in _recall_live
        }
    for _scope in projected_live:
        recall_checkpoint(vault_root, _scope)

    chg_items: list[tuple[str, FileSignature | None, bool, bool, bool]] = []
    for path in changed:
        p = _canonicalize_event_path(vault_root, vr, Path(path))
        in_kb, in_vault = scopes_for(vault_root, p)
        if not (in_kb or in_vault):
            continue
        try:
            signature: FileSignature | None = stat_signature(p)
        except OSError:
            signature = None  # created then gone before we could stat — treat as absent
        chg_items.append(
            (
                str(p),
                signature,
                in_kb,
                in_vault,
                signature is not None and recall_policy.is_recall_candidate(vault_root, p),
            )
        )
    if not (del_items or chg_items):
        return

    # Phase 2 — apply the map mutations under the lock (no syscalls).  A scope
    # being replacement-walked also records the event's target state.  The
    # replacement publisher merges that state before its atomic map swap, so a
    # walk that already passed this path cannot overwrite the newer event.
    with _lock:
        # Paths that actually mutated each scope's map, so the batch advances the
        # generation exactly once per scope and the retained event records the
        # target-state identities the delta will coalesce over.
        touched: dict[tuple[str, str], set[str]] = {}
        recall_touched: dict[tuple[str, str], set[str]] = {}
        for sp, in_kb, in_vault in del_items:
            for scope, member in (("kb", in_kb), ("vault", in_vault)):
                self_key = _key(vault_root, scope)
                if not member:
                    continue
                if self_key in _replacement_active:
                    _replacement_pending.setdefault(self_key, {})[sp] = (None, False)
                if self_key not in _live:
                    continue
                m = _maps.get(self_key)
                if m is not None and m.pop(sp, None) is not None:
                    _triples[self_key] = None
                    touched.setdefault(self_key, set()).add(sp)
                recall = _recall_maps.get(self_key)
                if recall is not None and recall.pop(sp, None) is not None:
                    _recall_triples[self_key] = None
                    recall_touched.setdefault(self_key, set()).add(sp)
        for sp, signature, in_kb, in_vault, is_recall in chg_items:
            for scope, member in (("kb", in_kb), ("vault", in_vault)):
                self_key = _key(vault_root, scope)
                if not member:
                    continue
                if self_key in _replacement_active:
                    _replacement_pending.setdefault(self_key, {})[sp] = (
                        signature,
                        is_recall,
                    )
                if self_key not in _live:
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
                recall = _recall_maps.setdefault(self_key, {})
                previous = recall.get(sp)
                if is_recall and signature is not None:
                    if previous != signature:
                        recall[sp] = signature
                        _recall_triples[self_key] = None
                        recall_touched.setdefault(self_key, set()).add(sp)
                elif previous is not None:
                    recall.pop(sp, None)
                    _recall_triples[self_key] = None
                    recall_touched.setdefault(self_key, set()).add(sp)
        for self_key, paths in touched.items():
            _record_event(self_key, paths)
        for self_key, paths in recall_touched.items():
            _recall_publications.pop(self_key, None)
            _record_recall_event(self_key, paths)


def invalidate(vault_root: Path | None = None) -> None:
    """Drop the registry back to not-live for a vault (or all vaults).

    Called at the end of `reconcile` (the heal-my-drift command) so a
    post-reconcile process re-seeds cleanly rather than trusting stale state.
    """
    with _lock:
        if vault_root is None:
            for key, epoch in _replacement_active.items():
                _replacement_epochs[key] = max(
                    _replacement_epochs.get(key, 0), epoch
                ) + 1
            _maps.clear()
            _triples.clear()
            _live.clear()
            _generations.clear()
            _history.clear()
            _recall_maps.clear()
            _recall_triples.clear()
            _recall_generations.clear()
            _recall_identities.clear()
            _recall_live.clear()
            _recall_history.clear()
            _recall_publications.clear()
            _external_pending.clear()
            return
        root = _canon(vault_root)
        for scope in SCOPES:
            key = (root, scope)
            active_epoch = _replacement_active.get(key)
            if active_epoch is not None:
                _replacement_epochs[key] = max(
                    _replacement_epochs.get(key, 0), active_epoch
                ) + 1
            _maps.pop(key, None)
            _triples.pop(key, None)
            _live.discard(key)
            _generations.pop(key, None)
            _history.pop(key, None)
            _recall_maps.pop(key, None)
            _recall_triples.pop(key, None)
            _recall_generations.pop(key, None)
            _recall_identities.pop(key, None)
            _recall_live.discard(key)
            _recall_history.pop(key, None)
            _recall_publications.pop(key, None)


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


# --------------------------------------------------------------------------- #
# Exact path custody for the read-side substrate caches
# --------------------------------------------------------------------------- #
#
# A governed write already names every path it touched. The whole-scope triple
# above cannot tell a change to ONE page from a change anywhere in the scope, so
# a cache keyed on it discards on every write what the previous write built.
# Design Decision 2 splits the two questions: a change that arrives WITH a
# receipt is applied to a per-path seam on each substrate cache and nothing else
# moves; a change that arrives WITHOUT one (an external edit reconciliation
# finds) invalidates the scope it drifted in, because there is no path set to be
# exact about.
#
# The seams live here rather than in one of the caches because the report has to
# be one report: "an exact update of that page's rows only, rebuild counters
# unchanged" is a statement about ALL of them, and an audit that fails closed has
# to fail closed for the request, not for one cache.


@dataclass(frozen=True, slots=True)
class CustodySeam:
    """One substrate cache's per-path invalidation seam.

    ``apply`` takes exact custody of a receipt's paths: it refreshes or evicts
    the rows for those paths and touches nothing else. ``verify`` reports which
    of a set of paths this cache does NOT hold at the page's current identity,
    changing nothing -- and it is what COUNTS the apply, so a seam that skipped
    its work reports zero updates rather than being believed. ``invalidate_scope``
    is the receipt-less counterpart and is optional: a durable store heals
    through its own repair worker, so only the in-process caches implement it.

    Both callables receive ``digests``: the sha256 of each path's canonical
    bytes right now, or ``None`` for a path that is not a readable file. It is
    computed ONCE per batch and threaded through, because four seams each
    hashing every path meant four full reads of every page a receipt named, on
    the thread that had just written them.
    """

    name: str
    apply: Callable[[Path, tuple[str, ...], tuple[str, ...], Mapping[str, str | None]], None]
    verify: (
        Callable[[Path, tuple[str, ...], Mapping[str, str | None]], tuple[str, ...]] | None
    ) = None
    invalidate_scope: Callable[[Path, str], None] | None = None


@dataclass(frozen=True, slots=True)
class CustodyReport:
    """What one receipt's path set did to every substrate cache."""

    reason: str
    paths: tuple[str, ...]
    updated: Mapping[str, int] = field(default_factory=dict)
    retired: Mapping[str, int] = field(default_factory=dict)
    mismatches: tuple[tuple[str, str], ...] = ()
    rebuilt: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CustodyAudit:
    """A cache-versus-page comparison and whether it had to fail closed."""

    reason: str
    paths: tuple[str, ...]
    mismatches: tuple[tuple[str, str], ...] = ()
    invalidated: bool = False


#: Modules that own a substrate cache, in the order their seams report.
_CUSTODY_SEAM_MODULES = ("find_corpus", "lexstore", "memory_refs")
_CUSTODY_REPORT_HISTORY = 64

_custody_lock = threading.RLock()
_custody_seams: dict[str, CustodySeam] = {}
_custody_seams_loaded = False
_custody_rebuilds: dict[str, int] = {}
#: Bounded like the report history. A long-lived cell that keeps hitting
#: receipt-less drift would otherwise grow this list without limit, and it is
#: diagnostics: the last N are what anyone reads.
_custody_scope_invalidations: deque[tuple[str, str]] = deque(
    maxlen=_CUSTODY_REPORT_HISTORY
)
_custody_reports: deque[CustodyReport] = deque(maxlen=_CUSTODY_REPORT_HISTORY)


def register_custody_seam(seam: CustodySeam) -> None:
    """Register (or replace) one substrate cache's per-path seam."""
    with _custody_lock:
        _custody_seams[seam.name] = seam


def _ensure_custody_seams() -> None:
    """Import the cache modules once so every seam is registered.

    Lazy on purpose: `find_corpus` imports this module, so a top-level import
    the other way would be a cycle. Every entry point below funnels through
    here, so a seam can never be missed because of import order -- which would
    otherwise read as "that cache took no custody" rather than as a defect.
    """
    global _custody_seams_loaded
    with _custody_lock:
        if _custody_seams_loaded:
            return
    from importlib import import_module

    loaded = True
    for name in _CUSTODY_SEAM_MODULES:
        try:
            import_module(f".{name}", __package__).register_path_custody()
        except Exception:  # noqa: BLE001 - retried on the next receipt
            log.warning("custody seam module %s did not register", name, exc_info=True)
            loaded = False
    with _custody_lock:
        _custody_seams_loaded = loaded


def custody_seams() -> tuple[str, ...]:
    _ensure_custody_seams()
    with _custody_lock:
        return tuple(_custody_seams)


def note_custody_rebuild(name: str) -> None:
    """Record that a substrate cache re-derived itself from the whole scope.

    Called by the caches themselves, at the one place each of them rebuilds. It
    is the counter the exact-custody invariant is stated against: a governed
    write that moves this has discarded work an exact receipt already covered.
    """
    with _custody_lock:
        _custody_rebuilds[name] = _custody_rebuilds.get(name, 0) + 1


def custody_rebuilds() -> dict[str, int]:
    with _custody_lock:
        return {name: count for name, count in _custody_rebuilds.items() if count}


def custody_scope_invalidations() -> tuple[tuple[str, str], ...]:
    with _custody_lock:
        return tuple(_custody_scope_invalidations)


def last_custody_report() -> CustodyReport | None:
    with _custody_lock:
        return _custody_reports[-1] if _custody_reports else None


def custody_reports_for(rel_path: str) -> tuple[CustodyReport, ...]:
    """Every retained report whose receipt named this path.

    A move is two receipts -- the destination is an upsert, the source a
    removal -- so a caller asking what happened to one path has to be able to
    see both.
    """
    target = _custody_rel(rel_path)
    with _custody_lock:
        return tuple(report for report in _custody_reports if target in report.paths)


def reset_custody_telemetry() -> None:
    """Drop the counters and the report history; keep the registered seams."""
    with _custody_lock:
        _custody_rebuilds.clear()
        _custody_scope_invalidations.clear()
        _custody_reports.clear()


def _custody_rel(value: object) -> str:
    return str(value or "").replace("\\", "/").lstrip("/")


def custody_digests(vault_root: Path, rel_paths: Iterable[str]) -> dict[str, str | None]:
    """sha256 of each path's canonical bytes now, or None when it is not a file.

    One read per path for the whole batch. Every seam compares against this
    rather than reading the page again: the frontmatter cache, both lexstore
    seams and the reference sidecar were each hashing the same bytes, so a
    receipt naming N pages cost 4N reads on the writer's thread.
    """
    from . import find_corpus

    root = Path(vault_root)
    digests: dict[str, str | None] = {}
    for rel in rel_paths:
        if rel in digests:
            continue
        content = find_corpus._read_page_bytes(root.joinpath(*rel.split("/")), root)
        digests[rel] = None if content is None else hashlib.sha256(content).hexdigest()
    return digests


def _custody_rels(values: Iterable[object]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        rel = _custody_rel(value)
        if rel:
            seen.setdefault(rel, None)
    return tuple(seen)


def apply_receipt_paths(
    vault_root: Path,
    *,
    changed: Iterable[object] = (),
    deleted: Iterable[object] = (),
    reason: str = "governed_write",
) -> CustodyReport:
    """Apply one committed receipt's path set to every substrate cache seam.

    Rows for those paths are refreshed or retired; nothing else moves. A path a
    seam cannot bring to exact custody is a mismatch, and a mismatch fails
    closed to a scope invalidation rather than leaving a row that answers.
    """
    _ensure_custody_seams()
    changed_rels = _custody_rels(changed)
    deleted_rels = _custody_rels(deleted)
    if not (changed_rels or deleted_rels):
        return CustodyReport(reason=reason, paths=())
    with _custody_lock:
        seams = tuple(_custody_seams.values())
        before = dict(_custody_rebuilds)
    all_rels = (*changed_rels, *deleted_rels)
    digests = custody_digests(vault_root, all_rels)
    updated: dict[str, int] = {}
    retired: dict[str, int] = {}
    mismatches: list[tuple[str, str]] = []
    for seam in seams:
        try:
            seam.apply(Path(vault_root), changed_rels, deleted_rels, digests)
            # The count is the PROOF, not the seam's own word for it: a seam
            # that skipped its work leaves a row its verify still calls stale,
            # so the update it did not make cannot be reported as made.
            stale = frozenset(
                seam.verify(Path(vault_root), all_rels, digests)
                if seam.verify is not None
                else ()
            )
        except Exception:  # noqa: BLE001 - one seam must not stop the others
            log.warning("custody seam %s failed to apply", seam.name, exc_info=True)
            # An unapplied seam is not a pass. Name every path it was asked
            # about so the batch fails closed rather than reading as exact.
            mismatches.extend((seam.name, rel) for rel in (*changed_rels, *deleted_rels))
            continue
        updated[seam.name] = sum(1 for rel in changed_rels if rel not in stale)
        retired[seam.name] = sum(1 for rel in deleted_rels if rel not in stale)
        mismatches.extend((seam.name, rel) for rel in sorted(stale))
    with _custody_lock:
        rebuilt = {
            name: count - before.get(name, 0)
            for name, count in _custody_rebuilds.items()
            if count - before.get(name, 0) > 0
        }
    report = CustodyReport(
        reason=reason,
        paths=(*changed_rels, *deleted_rels),
        updated=updated,
        retired=retired,
        mismatches=tuple(mismatches),
        rebuilt=rebuilt,
    )
    with _custody_lock:
        _custody_reports.append(report)
    if mismatches:
        _fail_closed_to_scope_invalidation(vault_root, reason=f"{reason}_mismatch")
    return report


def audit_custody(
    vault_root: Path,
    paths: Iterable[object],
    *,
    reason: str = "custody_audit",
) -> CustodyAudit:
    """Compare every substrate cache's rows against the pages themselves.

    The burst's own check, run again on demand. A row that does not describe
    the page it names is not a slow answer, it is a wrong one, so a mismatch
    invalidates the scope instead of being reported and served through. The
    result names seams and paths and never page content.
    """
    _ensure_custody_seams()
    rels = _custody_rels(paths)
    if not rels:
        return CustodyAudit(reason=reason, paths=())
    with _custody_lock:
        seams = tuple(_custody_seams.values())
    digests = custody_digests(vault_root, rels)
    mismatches: list[tuple[str, str]] = []
    for seam in seams:
        if seam.verify is None:
            continue
        try:
            seam_mismatches = seam.verify(Path(vault_root), rels, digests)
        except Exception:  # noqa: BLE001 - an unverifiable seam is not a pass
            log.warning("custody seam %s failed to verify", seam.name, exc_info=True)
            mismatches.extend((seam.name, rel) for rel in rels)
            continue
        mismatches.extend((seam.name, rel) for rel in seam_mismatches)
    invalidated = False
    if mismatches:
        _fail_closed_to_scope_invalidation(vault_root, reason=f"{reason}_mismatch")
        invalidated = True
    return CustodyAudit(
        reason=reason,
        paths=rels,
        mismatches=tuple(mismatches),
        invalidated=invalidated,
    )


def _fail_closed_to_scope_invalidation(vault_root: Path, *, reason: str) -> None:
    """A cache row that cannot be proven exact takes its whole scope with it."""
    for scope in SCOPES:
        invalidate_scope_for_drift(vault_root, scope=scope, reason=reason)


def invalidate_scope_for_drift(
    vault_root: Path,
    *,
    scope: str,
    reason: str,
) -> None:
    """Invalidate one scope for a change no receipt covers.

    The whole-scope key keeps exactly this role and no other: reconciliation
    found drift, or an audit found a row it cannot prove, and neither can name
    a path set to be exact about.
    """
    _ensure_custody_seams()
    with _custody_lock:
        seams = tuple(_custody_seams.values())
        _custody_scope_invalidations.append((scope, reason))
    for seam in seams:
        if seam.invalidate_scope is None:
            continue
        try:
            seam.invalidate_scope(Path(vault_root), scope)
        except Exception:  # noqa: BLE001 - one seam must not stop the others
            log.warning(
                "custody seam %s failed to invalidate scope %s",
                seam.name,
                scope,
                exc_info=True,
            )


def snapshot() -> dict:
    """Diagnostics: live scopes and their file counts."""
    with _lock:
        return {
            "live": sorted(_live),
            "counts": {k: len(v) for k, v in _maps.items()},
            "external_pending": sorted(_external_pending),
        }


def clear() -> None:
    """Test hook: return to the never-seeded state.

    Mints a fresh process-instance id so any checkpoint held across the reset
    reads as foreign — the same signal a genuine process restart would give.
    """
    global _external_pending_clock, _instance_id
    invalidate(None)
    reset_custody_telemetry()
    with _lock:
        _instance_id = uuid.uuid4().hex
        _external_pending_clock = 0
