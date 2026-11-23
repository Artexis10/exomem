"""Live file-watcher — re-embed out-of-band edits in ~1s instead of waiting for `reconcile`.

The vault is edited *around* the server — directly in Obsidian, on mobile, or via a
filesystem write (Obsidian Sync, a git pull). Those bypass the writer hooks, so the
embedding sidecar drifts until someone runs `reconcile`. This watcher closes that gap:
it watches the complete vault for freshness and vault-scope lexical maintenance, while
re-embedding only the `Knowledge Base/` subset through the SAME
`index_sync.upsert_after_write` dispatch the writers (and `reconcile`) use — deletes go
through `index_sync.delete_after_remove`. Each index remains behind its own availability
and scope gates.

Mirrors `MediaWorker`'s thread+queue shape: a single daemon dispatch thread coalesces
rapid events behind a ~500ms debounce (a single Obsidian save fires several FS events;
a `git pull` rewrites a batch at once) and then dispatches one batched upsert/delete.

Lazy + soft-fail: `watchdog` is imported only in `start()`. If it isn't installed the
watcher is a no-op and the server runs normally (mirrors how `media_worker`/`embeddings`
soft-fail on missing optional deps).

Self-write suppression: the server's own writers already refresh the embedding
sidecar (`vault.batch_atomic_write` → `upsert_after_write`; delete/move paths →
`delete_after_remove`), so their filesystem mutations would echo through the watcher
and re-embed the same markdown a second time. Writers register those mutations in the
module-level suppression registry below and `_record` drops a MATCHING event instead
of enqueueing it. The contract: an upsert event is suppressed only while the file's
(mtime_ns, size) signature still equals what the writer produced — a later external
edit changes the signature and dispatches normally; delete suppressions live behind a
short TTL (there is nothing left to stat). Entries are bounded and expire, so the
registry is opportunistic: a missed registration merely costs the old harmless echo.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import json
import logging
import os
import queue
import stat
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from . import (
    call_spans,
    capture_inventory,
    foreground_priority,
    freshness,
    graph_read_publication,
    index_sync,
    media_previews,
    media_processing,
    mode,
    mutation_lock,
    semantic_contract,
    semantic_writes,
    state_paths,
)
from .kbdir import managed_join, managed_root, managed_split

if TYPE_CHECKING:
    from .vault_store.importer import Importer, ImportReport

log = logging.getLogger(__name__)


def _media_mutation_guard(
    vault_root: Path,
    *,
    operation: str = "background_media_reconcile",
):
    from .writer_lease import get_manager

    return get_manager().mutation_guard(
        vault_root,
        operation=operation,
        holder_kind="background",
    )


def _reconcile_background_media(vault_root: Path, binary: Path) -> object:
    """Plan one artifact before taking authority for its bounded commit."""
    return media_processing.reconcile_media(
        vault_root,
        binary,
        explicit=False,
        commit_guard=lambda: _media_mutation_guard(vault_root),
        enqueue_preview=False,
    )


DEBOUNCE_SECONDS = 0.5
# How often the watcher re-walks and reconciles the freshness registry against
# disk truth, bounding how long a dropped watchdog event can leave it stale.
RECONCILE_INTERVAL_SECONDS = 300.0
# Upper bound on how many KB files one reconcile cycle re-embeds. Drift deltas
# are normally a handful of files (a missed event or two per 300s window); this
# only fires on a pathological drift (e.g. watchdog was dead while a large sync
# landed). The freshness map is ALWAYS fully healed regardless — only the embed
# dispatch is capped, and it logs the cap so the remainder can be closed with an
# explicit `reconcile`.
RECONCILE_MAX_EMBED_FILES = 500
RECONCILE_MAX_MEDIA_FILES = media_processing.DEFAULT_RECONCILE_LIMIT
# How long the compare-and-ack withdrawal keeps asking a busy mutation boundary
# before giving the epoch back to periodic recovery.
#
# The withdrawal contends with `epistemic_graph_drain_paths` -- the incremental
# drain, holding the same vault boundary while it repairs the very pages this
# dispatch is reacting to. Losing that race is the *expected* outcome on a cell
# whose queue is working, and it is explicitly retryable: the refusal carries
# `status: "retryable"` and a `retry_after_ms`.
#
# Abandoning it on the first refusal is what made a healthy drain disable the
# incremental path. `ack_ready` goes false, `clear_external_pending` is skipped,
# and the in-memory flag stays set -- which declines the graph read snapshot,
# which fails the dispatch's predecessor probe, which sends *every* subsequent
# write down a whole-vault rebuild until a reconcile cycle finds the boundary
# free. Measured on a live cell: a 4.9 s drain hold cost 43 s rebuilds on every
# write for the rest of the window.
#
# Bounded rather than unbounded because this runs on the debounce thread, and a
# boundary that stays busy this long is sustained contention -- a different
# condition, which periodic recovery and the exhaustion log below both report.
#
# This is the retry budget, NOT the wall-clock bound a caller waits. The
# deadline is only consulted after an attempt has already been refused, and
# each attempt first spends the mutation coordinator's own timeout waiting for
# the lock (`_DEFAULT_TIMEOUT_SECONDS`, 5.0 s today). So the real bound is this
# budget plus one coordinator timeout -- about 20 s at today's defaults, and
# measured at 20.00 s. `suspend_reads()` takes no timeout parameter to pass a
# shortened one through, so the honest thing is to state the number rather than
# imply a tighter one. Size this against watcher latency accordingly, and note
# that `test_a_permanently_busy_boundary_gives_up_within_a_bounded_wall_time`
# pins the bound against an absolute ceiling that does not scale with it.
GRAPH_WITHDRAWAL_RETRY_SECONDS = 15.0


def _background_deferred_limit(
    policy: mode.WatcherPolicy, remaining: int | None
) -> int | None:
    """Keep old queued work inside the smaller live-publication envelope.

    The reconcile cap governs real disk drift.  It is deliberately larger in
    performance mode, but reusing it for deferred repair turned a 500-file
    allowance into one 250-file full-index transaction.  A failed component
    then expanded that transaction into serial replay.  Background queue work
    has no freshness deadline, so it takes the smaller live cap and converges
    over later passes instead.
    """
    live_cap = policy.max_embed_files_per_batch
    if live_cap is not None:
        # Zero may defer a live burst, but background correctness still needs
        # one convergence slot. A zero remaining reconcile budget continues to
        # win below, so drift admission is never overspent.
        live_cap = max(1, live_cap)
    caps = [cap for cap in (remaining, live_cap) if cap is not None]
    return min(caps) if caps else None

# ---- Self-write suppression registry (module-level: available to writers even
# when no FileWatcher is running; keyed by (resolved vault root, vault-rel path)) ----
UPSERT_SUPPRESS_TTL_SECONDS = 30.0
DELETE_SUPPRESS_TTL_SECONDS = 5.0
_SUPPRESS_MAX_ENTRIES = 4096
_SUPPRESS_LOCK = threading.Lock()
# (root, rel) -> (mtime_ns, size, monotonic deadline)
_SELF_UPSERTS: dict[tuple[str, str], tuple[int, int, float]] = {}
# (root, rel) -> monotonic deadline
_SELF_DELETES: dict[tuple[str, str], float] = {}
PUBLICATION_INTENT_TTL_SECONDS = UPSERT_SUPPRESS_TTL_SECONDS
PUBLICATION_INTENT_MAX_BYTES = 4 * 1024 * 1024


@dataclass(slots=True, eq=False)
class _PublicationIntent:
    key: tuple[str, str]
    content_hash: str
    size: int
    deadline: float
    before_content_hash: str | None = None
    before_size: int | None = None
    phase: str = "prepared"
    disposition: str = "active"
    inflight: int = 0
    observers: list[_PublicationObservation] = field(default_factory=list)


@dataclass(slots=True)
class _PublicationObservation:
    watcher: weakref.ReferenceType[FileWatcher]
    path: Path
    fenced: bool = False


# Kept through the normal suppression window after finalization so a hash that
# started before commit completion still resolves against the same token.
_PUBLICATION_INTENTS: dict[tuple[str, str], _PublicationIntent] = {}
_WATCHERS_LOCK = threading.Lock()
_WATCHERS: dict[str, weakref.WeakSet[FileWatcher]] = {}


def observing_inbound_recovery_stop(vault_root: Path) -> threading.Event | None:
    """The stop signal for a seeded file watcher with live event delivery."""
    from .vault_store.mode import store_mode_for_reads

    if not freshness.event_indexes_enabled() or store_mode_for_reads(vault_root):
        return None
    root = _canon_root(vault_root)
    with _WATCHERS_LOCK:
        watchers = tuple(_WATCHERS.get(root, ()))
    for watcher in watchers:
        stop = watcher._stop
        dispatch = watcher._thread
        reconcile = watcher._reconcile_thread
        observer_alive = getattr(watcher._observer, "is_alive", None)
        if (
            watcher._importer is None and watcher._optional_recovery_armed
            and watcher._seed_complete.is_set() and watcher._seed_succeeded
            and dispatch is not None and dispatch.is_alive()
            and reconcile is not None and reconcile.is_alive()
            and observer_alive is not None and observer_alive()
            and not stop.is_set()
        ):
            return stop
    return None


def _canon_root(vault_root: Path) -> str:
    try:
        return str(vault_root.resolve())
    except OSError:
        return str(vault_root)


def _rel_posix(vault_root: Path, path: Path) -> str | None:
    """Vault-relative POSIX path, tolerant of already-deleted files."""
    try:
        # The vault root is configuration, so a scope may reuse it; the path resolves live.
        return path.resolve().relative_to(
            state_paths.resolved_vault_path(vault_root, expanduser=False)
        ).as_posix()
    except (ValueError, OSError):
        try:
            return path.relative_to(vault_root).as_posix()
        except ValueError:
            return None


def _publication_key(vault_root: Path, path: Path) -> tuple[str, str] | None:
    rel = _rel_posix(vault_root, path)
    if rel is None:
        return None
    return _canon_root(vault_root), rel


def _notify_publication_observers(
    observers: Iterable[_PublicationObservation],
    intent: _PublicationIntent,
    disposition: str,
) -> None:
    for observer in observers:
        watcher = observer.watcher()
        if watcher is None:
            continue
        try:
            watcher._publication_intent_finalized(observer.path, intent, disposition)
        except Exception:  # noqa: BLE001 - watcher custody must not block writers
            log.debug("publication intent observer failed", exc_info=True)


def _intent_observers_locked(intent: _PublicationIntent) -> list[_PublicationObservation]:
    intent.observers[:] = [
        observer for observer in intent.observers if observer.watcher() is not None
    ]
    if intent.disposition != "succeeded":
        for observer in intent.observers:
            observer.fenced = True
    observers = list(intent.observers)
    intent.observers.clear()
    return observers


def _bind_publication_observer_locked(
    intent: _PublicationIntent, watcher: FileWatcher, path: Path
) -> _PublicationObservation:
    for observer in intent.observers:
        if observer.watcher() is watcher and observer.path == path:
            return observer
    observer = _PublicationObservation(weakref.ref(watcher), path)
    intent.observers.append(observer)
    return observer


def _expire_publication_intent_locked(
    intent: _PublicationIntent, now: float
) -> list[_PublicationObservation]:
    if intent.disposition == "active" and intent.deadline <= now:
        intent.disposition = "expired"
        return _intent_observers_locked(intent)
    return []


def _bounded_descriptor_digest(
    path: Path, expected_size: int | None
) -> tuple[str, int, int] | None:
    """Read one stable, bounded regular-file descriptor without registry locks."""
    if expected_size is not None and (
        expected_size < 0 or expected_size > PUBLICATION_INTENT_MAX_BYTES
    ):
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        size = before.st_size
        if (
            not stat.S_ISREG(before.st_mode)
            or size > PUBLICATION_INTENT_MAX_BYTES
            or (expected_size is not None and size != expected_size)
        ):
            return None
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                return None
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError:
            return None
        if (
            after.st_size != size
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_dev != current.st_dev
            or before.st_ino != current.st_ino
            or after.st_mtime_ns != current.st_mtime_ns
        ):
            return None
        return digest.hexdigest(), after.st_mtime_ns, after.st_size
    except OSError:
        return None
    finally:
        os.close(descriptor)


def register_publication_intents(
    vault_root: Path,
    artifacts: Iterable[
        tuple[Path, int, str] | tuple[Path, int, str, str, int]
    ],
) -> tuple[_PublicationIntent, ...]:
    """Reserve bounded raw-byte proof tokens before canonical replacement."""
    candidates: list[tuple[tuple[str, str], str, int, str | None, int | None]] = []
    for artifact in artifacts:
        path, descriptor, digest, *before = artifact
        path = Path(path)
        if path.suffix.lower() != ".md" or len(digest) != 64:
            continue
        key = _publication_key(vault_root, path)
        if key is None:
            continue
        try:
            info = os.fstat(descriptor)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > PUBLICATION_INTENT_MAX_BYTES:
            continue
        before_hash: str | None = None
        before_size: int | None = None
        if before:
            if len(before) != 2:
                continue
            before_hash, before_size = before
            if (
                len(before_hash) != 64
                or before_size < 0
                or before_size > PUBLICATION_INTENT_MAX_BYTES
            ):
                continue
        candidates.append((key, digest, info.st_size, before_hash, before_size))
    now = time.monotonic()
    notifications: list[tuple[_PublicationIntent, list[_PublicationObservation]]] = []
    registered: list[_PublicationIntent] = []
    with _SUPPRESS_LOCK:
        _prune_locked(now)
        for key, digest, size, before_hash, before_size in candidates:
            existing = _PUBLICATION_INTENTS.get(key)
            if existing is not None:
                expired = _expire_publication_intent_locked(existing, now)
                if expired:
                    notifications.append((existing, expired))
            if (
                existing is not None and existing.disposition == "active"
            ) or len(_PUBLICATION_INTENTS) >= _SUPPRESS_MAX_ENTRIES:
                continue
            intent = _PublicationIntent(
                key=key,
                content_hash=digest,
                size=size,
                deadline=now + PUBLICATION_INTENT_TTL_SECONDS,
                before_content_hash=before_hash,
                before_size=before_size,
            )
            _PUBLICATION_INTENTS[key] = intent
            registered.append(intent)
    for intent, observers in notifications:
        _notify_publication_observers(observers, intent, intent.disposition)
    return tuple(registered)


def _observe_publication_intent(
    vault_root: Path,
    path: Path,
    *,
    watcher: FileWatcher | None = None,
    expected_intent: _PublicationIntent | None = None,
) -> tuple[str, _PublicationIntent | None]:
    """Classify one event without holding the registry lock across file I/O."""
    key = _publication_key(vault_root, path)
    if key is None:
        return "external", None
    for attempt in range(2):
        now = time.monotonic()
        notifications: list[_PublicationObservation] = []
        with _SUPPRESS_LOCK:
            intent = _PUBLICATION_INTENTS.get(key)
            if intent is None or (
                expected_intent is not None and intent is not expected_intent
            ):
                return "external", expected_intent
            notifications = _expire_publication_intent_locked(intent, now)
            observation = (
                _bind_publication_observer_locked(intent, watcher, path)
                if watcher is not None
                else None
            )
            start_disposition = intent.disposition
            start_phase = intent.phase
            intent.inflight += 1
        if notifications:
            _notify_publication_observers(notifications, intent, intent.disposition)
        proof = _bounded_descriptor_digest(path, None)
        notifications = []
        with _SUPPRESS_LOCK:
            intent.inflight -= 1
            notifications = _expire_publication_intent_locked(intent, time.monotonic())
            disposition = intent.disposition
            phase = intent.phase
            fenced = observation.fenced if observation is not None else False
            changed = _PUBLICATION_INTENTS.get(key) is not intent
        if notifications:
            _notify_publication_observers(notifications, intent, disposition)
        if changed and not fenced and expected_intent is None and attempt == 0:
            continue
        if fenced or changed:
            return "external", intent
        matches_after = (
            proof is not None
            and proof[0] == intent.content_hash
            and proof[2] == intent.size
        )
        # The snapshot timestamp echo can be read after installation begins
        # and before the rename lands; the file still holds the BEFORE bytes.
        matches_before = (
            proof is not None
            and disposition == "active"
            and phase in _NOT_YET_RENAMED
            and proof[0] == intent.before_content_hash
            and proof[2] == intent.before_size
        )
        if (
            not (matches_before or matches_after)
            and (proof is None or disposition != start_disposition or phase != start_phase)
            and not fenced
            and not changed
            and attempt == 0
        ):
            # Replacement can invalidate an open BEFORE descriptor entirely.
            # The rename can precede the token's phase update; retry that read too.
            # Re-read once after a publication boundary; after the rename only
            # exact current AFTER bytes remain admissible.
            continue
        if not (matches_before or matches_after):
            return "external", intent
        if disposition == "active":
            # AFTER bytes before the rename are someone else's, unless the
            # write leaves the page unchanged and they are also the snapshot.
            if matches_after and phase == "prepared" and not matches_before:
                return "external", intent
            return "held", intent
        if disposition == "succeeded" and phase == "postpublish_verified" and matches_after:
            return "suppressed", intent
        return "external", intent
    return "external", None


def _publication_intent_disposition(intent: _PublicationIntent) -> str:
    notifications: list[_PublicationObservation] = []
    with _SUPPRESS_LOCK:
        notifications = _expire_publication_intent_locked(intent, time.monotonic())
        disposition = intent.disposition
    if notifications:
        _notify_publication_observers(notifications, intent, disposition)
    return disposition


def publication_intent_deadline(intent: _PublicationIntent) -> float | None:
    with _SUPPRESS_LOCK:
        if intent.disposition != "active":
            return None
        return intent.deadline


#: Phases in which the destination still holds the bytes the writer
#: snapshotted: `installing` is entered just before the canonical rename.
#: Watcher events and registry samples both admit BEFORE bytes only here.
_NOT_YET_RENAMED = frozenset({"prepared", "installing"})
#: Phases in which a governed writer may already have renamed its staged bytes
#: into place. `prepared` precedes the rename, so AFTER bytes on disk then are
#: not the writer's.
_INSTALLING_OR_LATER = frozenset({"installing", "installed", "postpublish_verified"})

_StatPair = tuple[int, int]


def inflight_publications_explain(
    vault_root: Path,
    candidates: Mapping[str, tuple[_StatPair | None, _StatPair | None]],
) -> set[str]:
    """Vault-relative paths whose sampled difference is an in-flight governed write.

    `candidates` maps a vault-relative POSIX path to two `(mtime_ns, size)`
    pairs: what the caller sampled from disk, and what the registry recorded
    (either None when absent). The sample was taken earlier than this call, so
    the writer may have advanced since. An outstanding publication intent --
    registered before the writer touches the file, not yet expired, aborted or
    superseded -- explains a path when the file, re-read now through one
    bounded, stable descriptor, is one of:

    * the BEFORE bytes the writer snapshotted, with the sampled mtime and size,
      the sample's mtime and size being the registry's, and the writer not yet
      renamed (`prepared` or `installing`). Capturing the snapshot restores the
      file's timestamps through its descriptor, which moves only the ctime.
    * the AFTER bytes the writer staged, the writer having begun installing,
      and either the sample being of those bytes (same mtime and size) or the
      sample being BEFORE-shaped as above. In the second case the writer
      renamed between the sample and this proof, and its publication records
      the current bytes next.

    Any other bytes on the path -- a foreign edit landing while the write is
    in flight, even one of identical size, or one that also restores the
    mtime -- explain nothing, and neither does a write whose publication
    failed. What this cannot separate: a foreign writer producing exactly the
    governed writer's bytes, which is not a content difference, and a foreign
    edit that restored the mtime *before* the writer snapshotted it, since the
    snapshot is then of the foreign bytes.
    """
    root = _canon_root(vault_root)
    now = time.monotonic()
    notifications: list[tuple[_PublicationIntent, list[_PublicationObservation]]] = []
    pending: list[tuple[str, _PublicationIntent, _StatPair, bool]] = []
    with _SUPPRESS_LOCK:
        for rel, (sampled, recorded) in candidates.items():
            if sampled is None:
                continue
            intent = _PUBLICATION_INTENTS.get((root, rel))
            if intent is None:
                continue
            expired = _expire_publication_intent_locked(intent, now)
            if expired:
                notifications.append((intent, expired))
            if intent.disposition != "active":
                continue
            before_shaped = (
                intent.before_content_hash is not None
                and recorded == sampled
                and sampled[1] == intent.before_size
            )
            if before_shaped or sampled[1] == intent.size:
                pending.append((rel, intent, sampled, before_shaped))
    for intent, observers in notifications:
        _notify_publication_observers(observers, intent, intent.disposition)
    explained: set[str] = set()
    for rel, intent, sampled, before_shaped in pending:
        proof = _bounded_descriptor_digest(Path(root) / rel, None)
        if proof is None:
            continue
        digest, mtime_ns, size = proof
        as_sampled = (mtime_ns, size) == sampled
        holds_before = before_shaped and as_sampled and digest == intent.before_content_hash
        holds_after = (
            digest == intent.content_hash
            and size == intent.size
            and (as_sampled or before_shaped)
        )
        if not (holds_before or holds_after):
            continue
        with _SUPPRESS_LOCK:
            # Re-read the token after the file: still the same one, not failed
            # meanwhile, and in a phase consistent with the bytes read. One
            # that succeeded meanwhile published its AFTER bytes.
            if _PUBLICATION_INTENTS.get(intent.key) is not intent:
                continue
            if holds_before and intent.disposition == "active" and (
                intent.phase in _NOT_YET_RENAMED
            ):
                explained.add(rel)
            elif holds_after and (
                intent.disposition == "succeeded"
                or (intent.disposition == "active" and intent.phase in _INSTALLING_OR_LATER)
            ):
                explained.add(rel)
    return explained


def begin_publication_installation(intents: Iterable[_PublicationIntent]) -> None:
    """Advance exact tokens immediately before their canonical replacement."""
    with _SUPPRESS_LOCK:
        for intent in intents:
            if (
                _PUBLICATION_INTENTS.get(intent.key) is intent
                and intent.disposition == "active"
                and intent.phase == "prepared"
            ):
                intent.phase = "installing"


def mark_publication_installed(intents: Iterable[_PublicationIntent]) -> None:
    """Advance exact tokens immediately after their canonical replacement."""
    with _SUPPRESS_LOCK:
        for intent in intents:
            if (
                _PUBLICATION_INTENTS.get(intent.key) is intent
                and intent.disposition == "active"
                and intent.phase == "installing"
            ):
                intent.phase = "installed"


def _verify_publication_intents_after_publish(
    intents: Iterable[_PublicationIntent],
) -> tuple[set[_PublicationIntent], set[tuple[str, str]]]:
    """Reprove exact AFTER bytes after corpus publication without registry I/O."""
    verified: set[_PublicationIntent] = set()
    failed: set[tuple[str, str]] = set()
    for intent in intents:
        with _SUPPRESS_LOCK:
            current = _PUBLICATION_INTENTS.get(intent.key)
            if (
                current is not intent
                or intent.disposition != "active"
                or intent.phase != "installed"
            ):
                failed.add(intent.key)
                continue
        proof = _bounded_descriptor_digest(Path(intent.key[0]) / intent.key[1], intent.size)
        with _SUPPRESS_LOCK:
            if (
                _PUBLICATION_INTENTS.get(intent.key) is intent
                and intent.disposition == "active"
                and intent.phase == "installed"
                and proof is not None
                and proof[0] == intent.content_hash
                and proof[2] == intent.size
            ):
                intent.phase = "postpublish_verified"
                verified.add(intent)
            else:
                failed.add(intent.key)
    return verified, failed


def publication_in_flight(vault_root: Path, paths: Iterable[Path | str]) -> bool:
    """Whether a write in this process has reserved a page of `paths` and not yet published it.

    An intent is reserved before the canonical replacement and closes once the
    registry holds the write's event, so it spans the gap in which the disk is
    ahead of the registry. An intent past its deadline no longer counts: a
    writer that died leaves no event to wait for.
    """
    now = time.monotonic()
    root = _canon_root(vault_root)
    with _SUPPRESS_LOCK:
        reserved = {
            key[1]
            for key, intent in _PUBLICATION_INTENTS.items()
            if key[0] == root and intent.disposition == "active" and intent.deadline > now
        }
    if not reserved:
        return False
    return any(_rel_posix(vault_root, Path(path)) in reserved for path in paths)


def finalize_publication_intents(
    intents: Iterable[_PublicationIntent],
    *,
    succeeded: Iterable[_PublicationIntent] | None = (),
) -> None:
    """Set terminal token disposition and immediately replay held failures."""
    succeeded_set = set(succeeded or ())
    notifications: list[tuple[_PublicationIntent, list[_PublicationObservation]]] = []
    with _SUPPRESS_LOCK:
        for intent in intents:
            if _PUBLICATION_INTENTS.get(intent.key) is not intent:
                continue
            if intent.disposition != "active":
                continue
            intent.disposition = (
                "succeeded"
                if intent in succeeded_set and intent.phase == "postpublish_verified"
                else "aborted"
            )
            if intent.disposition != "succeeded":
                _SELF_UPSERTS.pop(intent.key, None)
            notifications.append((intent, _intent_observers_locked(intent)))
    for intent, observers in notifications:
        _notify_publication_observers(observers, intent, intent.disposition)


def abort_publication_intents(
    intents: Iterable[_PublicationIntent], *, force_paths: Iterable[Path] = ()
) -> None:
    intents = tuple(intents)
    force_paths = tuple(force_paths)
    finalize_publication_intents(intents)
    roots = {intent.key[0] for intent in intents}
    forced = {
        key
        for root in roots
        for path in force_paths
        if (key := _publication_key(Path(root), Path(path))) is not None
    }
    with _SUPPRESS_LOCK:
        remnants = [
            intent
            for intent in intents
            if intent.disposition in {"aborted", "expired"}
            and intent.key in forced
        ]
    by_root: dict[str, list[Path]] = {}
    for intent in remnants:
        root, rel = intent.key
        by_root.setdefault(root, []).append(Path(root) / rel)
    for root, paths in by_root.items():
        with _WATCHERS_LOCK:
            watchers = tuple(_WATCHERS.get(root, ()))
        if not watchers:
            freshness.mark_external_pending(Path(root), paths=paths)
            continue
        for watcher in watchers:
            for path in paths:
                watcher._replay_publication_remnant(path)


def _prune_locked(now: float) -> None:
    for k in [k for k, v in _SELF_UPSERTS.items() if v[2] <= now]:
        _SELF_UPSERTS.pop(k, None)
    for k in [k for k, v in _SELF_DELETES.items() if v <= now]:
        _SELF_DELETES.pop(k, None)
    for key, intent in list(_PUBLICATION_INTENTS.items()):
        if (
            intent.disposition != "active"
            and intent.inflight == 0
            and intent.deadline <= now
        ):
            _PUBLICATION_INTENTS.pop(key, None)
    if len(_SELF_UPSERTS) > _SUPPRESS_MAX_ENTRIES:
        for k in sorted(_SELF_UPSERTS, key=lambda k: _SELF_UPSERTS[k][2])[
            : len(_SELF_UPSERTS) - _SUPPRESS_MAX_ENTRIES
        ]:
            _SELF_UPSERTS.pop(k, None)
    if len(_SELF_DELETES) > _SUPPRESS_MAX_ENTRIES:
        for k in sorted(_SELF_DELETES, key=lambda k: _SELF_DELETES[k])[
            : len(_SELF_DELETES) - _SUPPRESS_MAX_ENTRIES
        ]:
            _SELF_DELETES.pop(k, None)


def _semantic_markdown(root: Path, path: Path) -> bool:
    from . import vault

    relative = capture_inventory.raw_event_path(root, path)
    spelling = relative if relative is not None else _rel_posix(root, path)
    # Sync-conflict filenames are excluded by the maintained Markdown scan contract.
    return spelling is not None and not vault.in_excluded_scan_dir(spelling) and ".sync-conflict-" not in path.name


def _publish_registry_change(
    vault_root: Path, changed: list[Path], deleted_rels: list[str]
) -> bool:
    """Update freshness + inbound for a server-authored change.

    A self-write's watcher echo is suppressed (redundant re-embed), but the
    write DID change the vault — so the freshness/inbound registries must still
    see it, or `find` would serve stale results for that file until the next
    reconcile. A live registry is patched immediately; an in-flight replacement
    seed retains the target state for its final swap (guards inside)."""
    witness = capture_inventory.raw_witness(vault_root)
    if witness is not None:
        for path in (*changed, *(vault_root / rel for rel in deleted_rels)):
            relative = capture_inventory.raw_event_path(vault_root, path)
            if relative is not None:
                witness.observe(relative)
    if not freshness.event_indexes_enabled():
        # Kill switch on: don't even pay the resolve() syscalls in _rel_posix.
        return False
    with call_spans.span("commit_tail.self_write.corpus"):
        corpus_published = index_sync.publish_corpus_delta(
            vault_root,
            changed=[path for path in changed if _semantic_markdown(vault_root, path)],
            deleted=[rel for rel in deleted_rels if _semantic_markdown(vault_root, vault_root / rel)],
            attempts=2,
        )
    if witness is not None:
        with _WATCHERS_LOCK:
            publishers = tuple(watcher for watcher in _WATCHERS.get(_canon_root(vault_root), ()) if watcher._raw_witness is witness)
        for publisher in publishers:
            publisher._wake.set()
            publisher._flush_raw()
    changed_rels = [r for r in (_rel_posix(vault_root, p) for p in changed) if r]
    try:
        from . import vault as vault_module

        with call_spans.span("commit_tail.self_write.inbound"):
            vault_module.on_inbound_files_changed(vault_root, changed_rels, deleted_rels)
    except Exception:  # noqa: BLE001
        log.debug("self-write inbound publish failed", exc_info=True)
    try:
        from . import find as find_module

        with call_spans.span("commit_tail.self_write.resolver"):
            find_module.on_resolver_files_changed(vault_root, changed_rels, deleted_rels)
    except Exception:  # noqa: BLE001
        log.debug("self-write resolver publish failed", exc_info=True)
    return corpus_published


def register_self_write(
    vault_root: Path,
    paths: Iterable[Path],
    *,
    return_publication_result: bool = False,
) -> set[_PublicationIntent] | tuple[set[_PublicationIntent], bool]:
    """Record server-authored markdown replacements so their watcher echo is
    dropped. Best-effort: unreadable/gone files are skipped (they simply won't
    be suppressed). Also publishes the change to the freshness/inbound
    registries, since the suppressed watcher echo won't."""
    paths = list(paths)
    # Every file the service writes joins the free-text census, Markdown or not.
    from .governance import artifact_census

    artifact_census.note_created(vault_root, paths)
    now = time.monotonic()
    signatures: list[tuple[tuple[str, str], int, int, _PublicationIntent | None]] = []
    notifications: list[tuple[_PublicationIntent, list[_PublicationObservation]]] = []
    intended: dict[tuple[str, str], _PublicationIntent] = {}
    failed_keys: set[tuple[str, str]] = set()
    for p in paths:
        p = Path(p)
        if p.suffix.lower() != ".md":
            continue
        key = _publication_key(vault_root, p)
        if key is None:
            continue
        with _SUPPRESS_LOCK:
            intent = _PUBLICATION_INTENTS.get(key)
            if intent is not None:
                expired = _expire_publication_intent_locked(intent, now)
                if expired:
                    notifications.append((intent, expired))
        if intent is not None:
            intended[key] = intent
            proof = _bounded_descriptor_digest(p, intent.size)
            if (
                proof is None
                or proof[0] != intent.content_hash
                or proof[2] != intent.size
            ):
                # A foreign replacement after the final writer guard is never
                # admitted by a same-size/stat suppression signature.
                failed_keys.add(key)
                continue
            _digest, mtime_ns, size = proof
        else:
            try:
                info = p.stat()
            except OSError:
                continue
            mtime_ns, size = info.st_mtime_ns, info.st_size
        signatures.append((key, mtime_ns, size, intent))
    for intent, observers in notifications:
        _notify_publication_observers(observers, intent, intent.disposition)
    registered: set[_PublicationIntent] = set()
    with _SUPPRESS_LOCK:
        for key, mtime_ns, size, intent in signatures:
            if intent is not None and (
                _PUBLICATION_INTENTS.get(key) is not intent
                or intent.disposition != "active"
                or intent.phase != "installed"
            ):
                failed_keys.add(key)
                continue
            _SELF_UPSERTS[key] = (
                mtime_ns,
                size,
                now + UPSERT_SUPPRESS_TTL_SECONDS,
            )
            if intent is not None:
                registered.add(intent)
        _prune_locked(now)
    published = _publish_registry_change(
        vault_root, changed=paths, deleted_rels=[]
    )
    if published:
        registered, verification_failures = _verify_publication_intents_after_publish(
            intended.values()
        )
        failed_keys.update(verification_failures)
    else:
        failed_keys.update(key for key in intended if key not in failed_keys)
        registered.clear()
    if failed_keys:
        with _SUPPRESS_LOCK:
            for key in failed_keys:
                _SELF_UPSERTS.pop(key, None)
    corpus_published = published and not failed_keys
    if return_publication_result:
        return registered, corpus_published
    return registered


def _register_self_write_and_close(
    vault_root: Path,
    paths: list[Path],
    intents: tuple[_PublicationIntent, ...],
) -> bool:
    """`register_self_write_and_close`'s body, shared by its inline and worker routes."""
    published = False
    try:
        registered, published = register_self_write(
            vault_root,
            paths,
            return_publication_result=True,
        )
        if published:
            finalize_publication_intents(intents, succeeded=registered)
        elif intents:
            abort_publication_intents(intents, force_paths=paths)
    except Exception:  # noqa: BLE001 — suppression is best-effort
        if intents:
            abort_publication_intents(intents, force_paths=paths)
        log.debug("self-write suppression registration failed", exc_info=True)
    except BaseException:  # noqa: BLE001
        # An interrupt-level escape (e.g. ReadBudgetExhausted under the
        # worker's copied request context) still owns the intents: abort them
        # so none stays active, then let the interrupt through.
        if intents:
            abort_publication_intents(intents, force_paths=paths)
        raise
    return published


#: How long a commit under the vault boundary waits for its self-write
#: publication before handing the rest to the worker below. The publication's
#: inner locks (`_CORPUS_CONTEXT_UPDATE_LOCK`, `_INBOUND_*`, `_RESOLVER_LOCK`)
#: can sit behind another long hold; the boundary's tail must stay bounded.
_IN_BOUNDARY_PUBLICATION_WAIT_SECONDS = 3.0


class _PublicationHandedOff:
    """Closed result of `register_self_write_and_close` for the handed-off case.

    Distinct from ``False``: the corpus delta is still owed, but the publish
    worker owns it, so a caller must not republish the same delta inline.
    Callers compare with ``is`` / ``is False``, never a truthiness test.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "PUBLICATION_HANDED_OFF"


PUBLICATION_HANDED_OFF = _PublicationHandedOff()

_PUBLISH_REQUESTS: queue.Queue = queue.Queue()
_PUBLISH_WORKER_LOCK = threading.Lock()
_PUBLISH_WORKER_STARTED = False


def _self_write_publish_loop() -> None:
    """Run each handed-off publication in submission order, forever.

    One worker keeps publications in commit order: a later commit's delta never
    lands before the one queued ahead of it. Each item runs in the context its
    submitter captured, so spans and request context attribute as they did."""
    while True:
        context, work, done, outcome, deferred = _PUBLISH_REQUESTS.get()
        try:
            outcome.append(context.run(work))
        except BaseException as error:  # noqa: BLE001 - report it to the waiter, never kill the worker
            outcome.append(error)
        finally:
            done.set()
            if deferred is not None:
                deferred()


def _self_write_publish_queue() -> queue.Queue:
    global _PUBLISH_WORKER_STARTED
    with _PUBLISH_WORKER_LOCK:
        if not _PUBLISH_WORKER_STARTED:
            threading.Thread(
                target=_self_write_publish_loop,
                name="exomem-self-write-publish",
                daemon=True,
            ).start()
            _PUBLISH_WORKER_STARTED = True
    return _PUBLISH_REQUESTS


def register_self_write_and_close(
    vault_root: Path, paths: Iterable[Path], intents: Iterable[_PublicationIntent]
) -> bool | _PublicationHandedOff:
    """Register a batch's self-writes, then close its publication intents.

    Returns whether the corpus delta was published. A published delta finalizes
    the intents, so a later event with the same bytes is the writer's own echo
    and one with different bytes stays an outside edit. Otherwise every intent
    aborts and its paths replay to the watchers. Best-effort: a failure aborts
    the intents too. The synchronous fan-out and a batch under derived custody
    both close their intents here, so readers see the published delta before
    the reply in either route.

    Called while the vault mutation boundary is held, the whole body runs on
    one module-level worker instead of inline: its registry locks can sit
    behind another holder's work, and the boundary's tail must stay bounded.
    The caller waits up to `_IN_BOUNDARY_PUBLICATION_WAIT_SECONDS`; past that
    it returns `PUBLICATION_HANDED_OFF` — still owed, like False, but the
    worker owns the delta and the intents (finalizing them on a publish,
    aborting them with `force_paths` otherwise) — so a caller must not touch
    the intents or republish the delta inline after handing them here. A cold
    whole-vault corpus build then runs on the worker: the boundary is not held
    for it beyond that bound.
    """
    paths = list(paths)
    intents = tuple(intents)
    if not mutation_lock.current_thread_holds_boundary():
        return _register_self_write_and_close(vault_root, paths, intents)
    # The read association's begin/finish only take their try-hold reentrantly,
    # so the begin must run on this thread while the boundary is held. The
    # worker's publish adopts it (ADOPTED_OBSERVATION) instead of beginning its
    # own, and the finish lands here when the publish answered within the
    # bound, or on the worker — with a bounded wait for the boundary this
    # thread is about to release — once it is handed off.
    changed = tuple(path for path in paths if _semantic_markdown(vault_root, path))
    observation = (
        graph_read_publication.begin_observation(vault_root, changed, ())
        if changed
        else None
    )
    claimed = threading.Event()
    resolved = threading.Event()
    token = graph_read_publication.ADOPTED_OBSERVATION.set(observation)
    try:
        context = contextvars.copy_context()
    finally:
        graph_read_publication.ADOPTED_OBSERVATION.reset(token)
    done = threading.Event()
    outcome: list[Any] = []
    deferred = None
    if observation is not None:

        def deferred() -> None:
            # The caller claims the finish itself when its wait saw `done`: a
            # reentrant finish inside its own hold lets the staged proof
            # complete at that hold's release, as an inline publish did. The
            # worker finishes only once the caller is resolved without
            # claiming — the timed-out handoff — and the caller's flag set
            # makes a late wake a no-op either way.
            resolved.wait(_IN_BOUNDARY_PUBLICATION_WAIT_SECONDS + 5.0)
            if claimed.is_set():
                return
            claimed.set()
            graph_read_publication.finish_observation(
                vault_root,
                observation,
                timeout_seconds=_IN_BOUNDARY_PUBLICATION_WAIT_SECONDS,
                complete_retry_seconds=10.0,
            )

    _self_write_publish_queue().put(
        (
            context,
            functools.partial(
                _register_self_write_and_close,
                vault_root,
                paths,
                intents,
            ),
            done,
            outcome,
            deferred,
        )
    )
    with call_spans.span("commit_tail.self_write.wait"):
        finished = done.wait(_IN_BOUNDARY_PUBLICATION_WAIT_SECONDS)
    if finished and observation is not None:
        claimed.set()
    resolved.set()
    if not finished:
        log.info("self-write publication handed off past the boundary")
        return PUBLICATION_HANDED_OFF
    if observation is not None:
        graph_read_publication.finish_observation(vault_root, observation)
    return outcome[0] is True


def register_self_delete(vault_root: Path, rel_paths: Iterable[str]) -> None:
    """Record server-authored markdown removals (delete/trash/move-away) so
    their watcher echo is dropped. TTL-bounded — there is no file left to
    signature-match. Also publishes the removal to the freshness/inbound
    registries, since the suppressed watcher echo won't."""
    rel_paths = list(rel_paths)
    from .governance import artifact_census

    artifact_census.note_touched(vault_root, rel_paths)
    root = _canon_root(vault_root)
    now = time.monotonic()
    with _SUPPRESS_LOCK:
        for rel in rel_paths:
            rel_posix = str(rel).replace("\\", "/")
            if not rel_posix.lower().endswith(".md"):
                continue
            _SELF_DELETES[(root, rel_posix)] = now + DELETE_SUPPRESS_TTL_SECONDS
        _prune_locked(now)
    _publish_registry_change(
        vault_root, changed=[], deleted_rels=[str(r).replace("\\", "/") for r in rel_paths]
    )


def _is_self_write_event(vault_root: Path, path: Path, *, deleted: bool) -> bool:
    """True when this event matches a registered self-authored mutation."""
    rel = _rel_posix(vault_root, path)
    if rel is None:
        return False
    key = (_canon_root(vault_root), rel)
    now = time.monotonic()
    with _SUPPRESS_LOCK:
        _prune_locked(now)
        if deleted:
            deadline = _SELF_DELETES.get(key)
            return deadline is not None and deadline > now
        entry = _SELF_UPSERTS.get(key)
    if entry is None:
        return False
    mtime_ns, size, deadline = entry
    if deadline <= now:
        return False
    try:
        st = path.stat()
    except OSError:
        # Can't verify the signature — let the event dispatch (safe: the
        # duplicate upsert is idempotent; hiding a real edit is not).
        return False
    return st.st_mtime_ns == mtime_ns and st.st_size == size


def clear_self_write_registry() -> None:
    """Test hook: drop all suppression entries."""
    with _SUPPRESS_LOCK:
        _SELF_UPSERTS.clear()
        _SELF_DELETES.clear()
        _PUBLICATION_INTENTS.clear()


def _import_watchdog():
    """Import watchdog lazily. Returns (Observer, FileSystemEventHandler).

    Isolated into a tiny function so `start()` can catch a missing dep and so tests
    can patch it to simulate watchdog being absent. Where watchdog picks inotify,
    the Observer is the one whose watches never follow a directory symlink.
    """
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    # Only the inotify emitter takes a kernel mask. watchdog already imported
    # that module to pick it, so the observer below cannot fail to import.
    if Observer.__module__ == "watchdog.observers.inotify":
        return _no_follow_inotify_observer(), FileSystemEventHandler
    return Observer, FileSystemEventHandler


@functools.cache
def _no_follow_inotify_observer() -> type:
    """The inotify observer, with watches that never follow a directory symlink.

    watchdog adds IN_DONT_FOLLOW to its default mask, but not to the mask that it
    derives from an event filter. Without that bit, the watch that watchdog adds
    for a symlinked directory inside a new directory follows the link. Writes
    outside the vault then reach the watcher, and writes in an in-vault target
    are reported under the link's path until a restart.
    """
    from watchdog.observers.api import DEFAULT_OBSERVER_TIMEOUT, BaseObserver
    from watchdog.observers.inotify import InotifyEmitter
    from watchdog.observers.inotify_c import InotifyConstants

    class NoFollowInotifyEmitter(InotifyEmitter):
        def get_event_mask_from_filter(self) -> int | None:
            mask = super().get_event_mask_from_filter()
            # None selects watchdog's default mask, which already holds the bit.
            return None if mask is None else mask | InotifyConstants.IN_DONT_FOLLOW

    class NoFollowInotifyObserver(BaseObserver):
        def __init__(self, *, timeout: float = DEFAULT_OBSERVER_TIMEOUT) -> None:
            super().__init__(NoFollowInotifyEmitter, timeout=timeout)

    return NoFollowInotifyObserver


def _graph_incompleteness_fields(vault_root: Path) -> str:
    """The state that explains an unclassified fan-out incompleteness.

    Root-causing one of these meant reading the graph's own sidecars after the
    fact and inferring what the watcher had seen. These are the fields that
    inference reconstructed every time, so the event carries them instead: the
    epoch the graph is in, the state and generation it reports, how much repair
    is queued, and whether that repair is whole-vault -- a changed scope the
    incremental path could not determine -- or a known path list.

    Each read is taken once and each field degrades to `?` on its own.
    Diagnostics must never be why a drain fails, and describing a situation
    should not cost three walks of the sidecars it is describing.
    """
    from . import deferred_index, graph_sync

    def read(produce: Callable[[], object]) -> object:
        try:
            return produce()
        except Exception:  # noqa: BLE001 - a missing field beats a failed drain
            return "?"

    status = read(lambda: graph_sync.status(vault_root))
    fields = {
        "epoch": read(lambda: graph_sync.classify_epoch(vault_root).kind),
        "state": status.get("state") if isinstance(status, dict) else status,
        "generation": status.get("generation") if isinstance(status, dict) else status,
        "queued": read(lambda: deferred_index.graph_status(vault_root).get("count")),
        "scope": read(
            lambda: "full"
            if deferred_index.graph_full_rebuild_pending(vault_root) is not None
            else "paths"
        ),
    }
    return " ".join(f"{name}={value}" for name, value in fields.items())


def _graph_incompleteness_scope(vault_root: Path, rels: Iterable[str]) -> list[Path] | None:
    """The paths an unclassified fan-out incompleteness affects, or None for the vault.

    `_graph_incompleteness_fields` reports the same judgement as `scope=paths` or
    `scope=full`; this returns it as a value, so the external mark the drain
    raises can carry its scope (`seamless-managed-worker-handoff` D3).

    The scope is *this drain's own batch* -- the paths whose fan-out just failed
    to converge -- and deliberately not the durable repair queue. The queue is a
    backlog, not an observation: it only grows while repair is outstanding, so
    marking it makes the fenced set grow monotonically until it covers whatever
    the next write touches, which is the same self-sustaining loop in a narrower
    costume.

    None means "whole vault": a drain that can name no path has not established
    a scope, and a mark whose scope cannot be established has to fence
    everything. A standing whole-vault marker is no longer a reason to widen:
    `_graph_repair_owned` reads it as the owner of the repair, and the watcher
    then marks nothing.
    """
    paths = [vault_root / rel for rel in rels]
    return paths or None


def _graph_repair_owned(vault_root: Path, *, batch_owned: bool) -> bool | None:
    """Whether the graph already owes an incomplete fan-out its repair; None if unknown.

    A standing whole-vault marker owes every path, and `batch_owned` says the
    batch's own dispatch left registered or queued repair
    (`index_sync.graph_repair_is_owned`). Marking an owned repair as an outside
    edit fenced later writes to those paths, or, unscoped, every write. None
    means the marker cannot be read, which is an unknown scope.
    """
    from . import deferred_index

    try:
        marker_stands = deferred_index.graph_full_rebuild_pending(vault_root) is not None
    except Exception:  # noqa: BLE001 - an unreadable marker is an unknown scope
        return None
    return marker_stands or batch_owned


def _background_graph_caller(method: Callable[..., Any]) -> Callable[..., Any]:
    """Run a watcher step as a background graph caller.

    Nobody waits on the watcher for a converged graph: the durable queue owns
    the repair its dispatch defers. Inside `graph_sync.standalone_join_waived`
    its graph dispatch stops at queued instead of joining a whole-vault pass.
    """

    @functools.wraps(method)
    def run(self: FileWatcher, *args: Any, **kwargs: Any) -> Any:
        from . import graph_sync

        with graph_sync.standalone_join_waived():
            return method(self, *args, **kwargs)

    return run


class FileWatcher:
    """Watch Knowledge Base/ for `.md` changes and re-embed them, debounced.

    With an ``importer`` (a store-mode vault) the watcher keeps its observer, debounce, reconcile
    interval and census notes, and hands every path that the store owns to the importer instead of
    the file-mode freshness, graph and index machinery, which reads files the store no longer owns.
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        debounce_seconds: float | None = None,
        importer: Importer | None = None,
    ) -> None:
        from .writer_lease import active_manager

        self._vault_root = vault_root
        self._raw_witness = capture_inventory.new_raw_witness(vault_root)
        # Retain the invoking manager: background threads do not inherit its
        # ContextVar, and publication must contend with that same writer.
        self._freshness_manager = active_manager()
        self._kb_root = managed_root(vault_root)
        self._debounce_override = debounce_seconds
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._pending_upsert: set[Path] = set()
        self._pending_delete: set[Path] = set()
        self._pending_media: set[Path] = set()
        self._pending_preview_removals: set[str] = set()
        self._managed_media_events: dict[Path, bool] = {}
        self._pending_publication_intents: dict[Path, _PublicationIntent] = {}
        self._pending_external_epoch = 0
        self._pending_access_policy = False
        self._importer = importer
        #: Store-mode paths that an event named, and the renames that move events paired.
        self._pending_import: set[str] = set()
        self._pending_moves: dict[str, str] = {}
        self._last_change = 0.0
        self._wake = threading.Event()
        self._stop = foreground_priority.StopEvent()
        self._thread: threading.Thread | None = None
        self._observer = None
        self._observer_arming: threading.Thread | None = None
        self._observer_armed = threading.Event()
        self._carried_graph = None
        self._reconcile_thread: threading.Thread | None = None
        self._seed_published = threading.Event()
        self._seed_complete = threading.Event()
        self._seed_succeeded = False
        self._startup_recovery_started = False
        self._optional_recovery_armed = False
        self._dispatch_waits_for_seed = False
        from . import vault

        vault.register_inbound_index(vault_root)
        with _WATCHERS_LOCK:
            _WATCHERS.setdefault(_canon_root(vault_root), weakref.WeakSet()).add(self)

    def _watcher_policy(self) -> mode.WatcherPolicy:
        return mode.watcher_policy()

    def _debounce_seconds(self) -> float:
        if self._debounce_override is not None:
            return self._debounce_override
        return self._watcher_policy().debounce_seconds

    def _reconcile_interval_seconds(self) -> float:
        return self._watcher_policy().reconcile_interval_seconds

    def _is_kb(self, path: Path) -> bool:
        """True when `path` is under Knowledge Base/ — the subset that gets
        embedded. The watcher observes the whole vault (for freshness/inbound)
        but only KB markdown is re-embedded into the KB sidecar."""
        try:
            path.resolve().relative_to(self._kb_root.resolve())
            return True
        except (ValueError, OSError):
            try:
                path.relative_to(self._kb_root)
                return True
            except ValueError:
                return False

    # ---- change recording (called by the watchdog handler AND by tests) ----

    def _record_raw(self, path: Path, *, directory: bool = False) -> bool:
        if self._importer is not None:
            return False
        relative = capture_inventory.raw_event_path(self._vault_root, path, directory=directory)
        if relative is None:
            return False
        self._raw_witness.observe(relative)
        with self._lock:
            self._last_change = time.monotonic()
        self._wake.set()
        return True

    def _flush_raw(self, *, census: bool = False) -> bool:
        if self._importer is not None:
            return True
        _epoch, pending, _seeded = self._raw_witness.snapshot()
        # A directory event also covers active child publications; child events are not guaranteed.
        with _SUPPRESS_LOCK:
            active = tuple((key[1], intent) for key, intent in _PUBLICATION_INTENTS.items()
                           if key[0] == _canon_root(self._vault_root) and intent.disposition == "active")
        for path, intent in active:
            if capture_inventory.raw_path(self._vault_root, path) and (census or any(
                not prefix or path == prefix or path.startswith(prefix + "/") for prefix in pending
            )):
                if path not in pending:
                    self._record_raw(self._vault_root / path)
                self._hold_publication_intent(self._vault_root / path, intent)
        _epoch, pending, _seeded = self._raw_witness.snapshot()
        for path in pending:
            if capture_inventory.raw_path(self._vault_root, path):
                disposition, intent = _observe_publication_intent(self._vault_root, self._vault_root / path, watcher=self)
                if disposition == "held" and intent is not None:
                    self._hold_publication_intent(self._vault_root / path, intent)
        with self._lock:
            held = frozenset(filter(None, (self._rel(path) for path in self._pending_publication_intents)))
        return semantic_contract.publish_capture_inventory(self._vault_root, self._raw_witness, census=census, excluded=held)

    def _record_external_locked(self, path: Path, *, deleted: bool) -> None:
        from . import media_bindings

        try:
            media_bindings.external_change(self._vault_root)
        except Exception:  # noqa: BLE001 - pending external custody still fences eviction below
            log.warning("physical binding inventory was not invalidated before debounce", exc_info=True)
        if deleted:
            self._pending_upsert.discard(path)
            self._pending_delete.add(path)
        else:
            self._pending_delete.discard(path)
            self._pending_upsert.add(path)
        # Path-scoped (`seamless-managed-worker-handoff` D3). The scope this
        # method already knows -- one file -- is the scope the mark now
        # carries, so a governed write elsewhere is not fenced by it. Reads
        # that require a current projection still refuse on any unrepaired
        # mark; the compare-and-ack below is unchanged and retires the epoch,
        # and with it every path marked at or before it.
        self._pending_external_epoch = max(
            self._pending_external_epoch,
            freshness.mark_external_pending(self._vault_root, paths=(path,)),
        )
        self._last_change = time.monotonic()

    def _publication_intent_finalized(
        self, path: Path, intent: _PublicationIntent, disposition: str
    ) -> None:
        """Resolve or fence an observed publication as its transaction closes."""
        if disposition != "succeeded":
            self._record_raw(path)
        with self._lock:
            if self._pending_publication_intents.get(path) is not intent:
                if disposition == "succeeded":
                    return
            else:
                self._pending_publication_intents.pop(path, None)
            if disposition != "succeeded" and _semantic_markdown(self._vault_root, path):
                self._record_external_locked(path, deleted=False)
        self._wake.set()

    def _replay_publication_remnant(self, path: Path) -> None:
        """Fence a failed transaction even if watchdog never delivered its echo."""
        self._record_raw(path)
        with self._lock:
            self._pending_publication_intents.pop(path, None)
            if not _semantic_markdown(self._vault_root, path):
                return
            if path in self._pending_upsert or path in self._pending_delete:
                return
            self._record_external_locked(path, deleted=False)
        self._wake.set()

    def _hold_publication_intent(self, path: Path, intent: _PublicationIntent) -> None:
        with self._lock:
            self._pending_publication_intents[path] = intent
            self._last_change = time.monotonic()
        # The transaction can close between token observation and watcher
        # custody. Rechecking its retained disposition closes that tiny race.
        disposition = _publication_intent_disposition(intent)
        if disposition != "active":
            self._publication_intent_finalized(path, intent, disposition)
        else:
            self._wake.set()

    def _resolve_held_publication_intents(self) -> None:
        with self._lock:
            held = list(self._pending_publication_intents.items())
        for path, intent in held:
            disposition, observed = _observe_publication_intent(
                self._vault_root,
                path,
                expected_intent=intent,
            )
            if observed is not intent:
                disposition = "external"
            if disposition != "held":
                self._publication_intent_finalized(
                    path,
                    intent,
                    "succeeded" if disposition == "suppressed" else "aborted",
                )

    def _held_publication_intent_timeout(self) -> float | None:
        with self._lock:
            intents = tuple(self._pending_publication_intents.values())
        deadlines = [
            deadline
            for intent in intents
            if (deadline := publication_intent_deadline(intent)) is not None
        ]
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - time.monotonic())

    def _notify_view_change(self, path: Path, *, destination=None, directory=False) -> None:
        from .collection_store import runtime

        try:
            runtime.notify_view_change(self._vault_root, path, destination=destination, directory=directory)
        except Exception:  # noqa: BLE001 - a failed hint must not stop the existing index publishers
            log.debug("collection view notification failed", exc_info=True)

    def _note_directories(self, *paths: str | bytes) -> None:
        """Name directories that an event touched, for the next census rebuild."""
        for path in paths:
            self._record_raw(Path(os.fsdecode(path)), directory=True)
        from .governance import artifact_census

        rels = [rel for rel in (self._rel(Path(os.fsdecode(path))) for path in paths) if rel]
        artifact_census.note_touched(self._vault_root, rels)

    def _import_target(self, path: Path) -> str | None:
        """The vault path that the importer settles for this event, or None.

        In store mode that is each path the store owns, and each Markdown file that it does not own,
        whose lexical rows the importer keeps current. File mode has no importer.
        """
        if self._importer is None:
            return None
        from .vault_store import ownership

        rel = self._rel(path)
        if rel is None:
            return None
        if self._importer.takes(rel) or ownership.is_markdown(rel):
            return rel
        return None

    def _pend_import(self, rel: str) -> None:
        with self._lock:
            self._pending_import.add(rel)
            self._last_change = time.monotonic()
        self._wake.set()

    def _note_move(self, source: Path, destination: Path) -> None:
        """Remember that a move event renamed ``source`` to ``destination`` for the importer."""
        if self._importer is None:
            return
        old, new = self._rel(source), self._rel(destination)
        if old is None or new is None:
            return
        with self._lock:
            self._pending_moves[new] = old

    def _record(self, path: Path, *, deleted: bool, notify_views: bool = True) -> None:
        """Record a Markdown or supported-media change, coalesced by path."""
        from .vault import in_excluded_scan_dir

        raw_observed = self._record_raw(path)
        owned_rel = self._import_target(path)
        if self._importer is None and path.suffix.lower() != ".md" and self._is_kb(path):
            from . import media_store

            try:
                disposition = media_store.managed_event(self._vault_root, path)
            except Exception:  # noqa: BLE001 - an unjudged event is recorded as an ordinary change, which the debounce re-reads
                log.warning("a media event could not be matched to the media store", exc_info=True)
                disposition = None
            if disposition == media_store.DEFER:
                with self._lock:
                    self._managed_media_events[path] = deleted
                    self._last_change = time.monotonic()
                self._wake.set()
                return
            if disposition == media_store.SUPPRESS:
                return
        if notify_views:
            # A same-stat external save can match index suppression; edit-back still needs its hint.
            self._notify_view_change(path)
        rel = self._rel(path)
        if rel is not None:
            # Any file event reaches the free-text census, ahead of the filters
            # below that keep only Markdown and media for the indexes.
            from .governance import artifact_census

            if deleted:
                artifact_census.note_touched(self._vault_root, [rel])
            else:
                artifact_census.note_created(self._vault_root, [rel])
        if owned_rel is not None:
            self._pend_import(owned_rel)
        if rel == managed_join("_access.yaml"):
            # Access policy changes are a publication boundary despite not
            # being Markdown.  Do this before the generic non-Markdown return
            # so graph reads fail closed until policy projection is reconciled.
            with self._lock:
                self._pending_access_policy = True
                self._pending_external_epoch = max(
                    self._pending_external_epoch,
                    freshness.mark_external_pending(self._vault_root),
                )
                self._last_change = time.monotonic()
            self._wake.set()
            return
        if rel is not None:
            # Instance overlays can use configured paths; the cache owns the
            # exact path match and drops only snapshots that read this file.
            from .vocabulary import invalidate as invalidate_vocabulary

            invalidate_vocabulary(self._vault_root, path=rel)
        if self._importer is not None:
            return  # the importer settles it from the store's records, not the file-mode machinery
        if rel is not None and (in_excluded_scan_dir(rel) or ".sync-conflict-" in path.name):
            if raw_observed and not deleted:
                disposition, intent = _observe_publication_intent(self._vault_root, path, watcher=self)
                if disposition == "held" and intent is not None:
                    self._hold_publication_intent(path, intent)
            # _trash/_archive/_Schema/…: every full walk skips these, so the
            # event path must too — else a delete's move-to-trash re-embeds
            # the trashed note under its trash path.
            return
        if path.suffix.lower() != ".md":
            if not self._is_kb(path):
                return
            if deleted:
                if not path.is_relative_to(self._vault_root) or not media_previews.supports(path):
                    return
            elif media_processing.classify_media(path) is None:
                return
            with self._lock:
                if deleted:
                    self._pending_preview_removals.add(path.relative_to(self._vault_root).as_posix())
                else:
                    self._pending_media.add(path)
                self._last_change = time.monotonic()
            self._wake.set()
            return
        if not deleted:
            disposition, intent = _observe_publication_intent(
                self._vault_root,
                path,
                watcher=self,
            )
            if disposition == "suppressed":
                log.debug("file watcher: suppressed completed publication intent for %s", path)
                return
            if disposition == "held" and intent is not None:
                self._hold_publication_intent(path, intent)
                return
            if intent is not None:
                # An exact-intent mismatch is external by definition. It must
                # not fall through to the legacy stat signature, which cannot
                # distinguish foreign bytes with restored metadata.
                with self._lock:
                    self._record_external_locked(path, deleted=False)
                self._wake.set()
                return
        if _is_self_write_event(self._vault_root, path, deleted=deleted):
            log.debug("file watcher: suppressed self-write echo for %s", path)
            return
        with self._lock:
            self._record_external_locked(path, deleted=deleted)
        self._wake.set()

    def _rel(self, path: Path) -> str | None:
        """Vault-relative POSIX path (no resolve()-on-missing surprises for deletes)."""
        try:
            return path.resolve().relative_to(self._vault_root.resolve()).as_posix()
        except (ValueError, OSError):
            try:
                return path.relative_to(self._vault_root).as_posix()
            except ValueError:
                return None

    def _drain(self) -> tuple[list[Path], list[Path], list[str], int, bool]:
        with self._lock:
            media = sorted(self._pending_media)
            ups = sorted(self._pending_upsert)
            dels = sorted(self._pending_delete)
            pending_epoch = self._pending_external_epoch
            access_policy = self._pending_access_policy
            self._pending_media.clear()
            self._pending_upsert.clear()
            self._pending_delete.clear()
            self._pending_external_epoch = 0
            self._pending_access_policy = False
        del_rels = [r for r in (self._rel(p) for p in dels) if r]
        return media, ups, del_rels, pending_epoch, access_policy

    @_background_graph_caller
    def _flush(self) -> None:
        """Dispatch the coalesced batch: publish freshness/inbound for every
        changed path (vault-wide), and re-embed only the Knowledge Base subset."""
        self._resolve_held_publication_intents()
        self._flush_raw()
        if self._importer is not None:
            self._import_pending()
        with self._lock:
            managed = self._managed_media_events
            self._managed_media_events = {}
        for path, deleted in managed.items():
            self._record(path, deleted=deleted)
        with self._lock:
            preview_removals, self._pending_preview_removals = self._pending_preview_removals, set()
        if preview_removals:
            # Hand binary removals straight to the guarded preview cleanup.
            try:
                media_previews.retire(self._vault_root, preview_removals)
            except Exception as error:  # noqa: BLE001 - the periodic sweep retries failed cleanup
                log.warning("file watcher: preview cleanup deferred (%s)", type(error).__name__)
        media, ups, del_rels, pending_epoch, access_policy = self._drain()
        if access_policy:
            self._reconcile_access_policy(pending_epoch)
        if not (media or ups or del_rels):
            return

        for path in media:
            try:
                media_processing.reconcile_media(
                    self._vault_root,
                    path,
                    explicit=False,
                    commit_guard=lambda: _media_mutation_guard(
                        self._vault_root, operation="background_media_event"
                    ),
                )
            except Exception:  # noqa: BLE001 - a bad artifact must never kill the watcher
                log.exception("file watcher: media reconciliation failed for %s", path)
        if not (ups or del_rels):
            return

        up_rels = [r for r in (self._rel(p) for p in ups) if r]
        self._dispatch_batch(
            ups,
            up_rels,
            del_rels,
            cap=False,
            pending_epoch=pending_epoch,
        )

    def _import_pending(self) -> None:
        """Hand the importer the paths that events named since the last batch."""
        importer = self._importer
        assert importer is not None
        with self._lock:
            rels, self._pending_import = self._pending_import, set()
            moves, self._pending_moves = self._pending_moves, {}
        if not rels:
            return
        try:
            report = importer.import_paths(rels, moves)
        except Exception:  # noqa: BLE001 - the next sweep settles these paths from their hashes
            log.exception("file watcher: importing %d outside change(s) failed", len(rels))
            return
        self._after_import(report)

    def _after_import(self, report: ImportReport) -> None:
        """Tell the caches that read a changed path that the store now holds new bytes there."""
        from .vocabulary import invalidate as invalidate_vocabulary

        for rel in (*report.imported, *report.moved, *report.moved.values()):
            invalidate_vocabulary(self._vault_root, path=rel)
        if report.settled or report.unjudged:
            log.info(
                "file watcher: imported %d, renamed %d, held %d, removed %d, cleared %d, "
                "refreshed %d, left %d outside change(s)",
                len(report.imported), len(report.moved), len(report.held), len(report.removed),
                len(report.cleared), len(report.refreshed), len(report.unjudged),
            )
        if report.media_derived or report.media_failed or report.media_attempted:
            log.info(
                "file watcher: derived the media rows of %d imported file(s), attempted %d more, "
                "failed %d",
                len(report.media_derived), report.media_attempted, len(report.media_failed),
            )
        for rel in report.deferred:
            self._pend_import(rel)  # the media store is moving it; the next batch looks again

    def _reconcile_access_policy(self, pending_epoch: int) -> None:
        """Reproject one observed `_access.yaml` edit before clearing its barrier."""
        try:
            for scope in freshness.SCOPES:
                if freshness.recall_is_live(self._vault_root, scope):
                    freshness.recall_checkpoint(self._vault_root, scope)
            self._recover_external_pending(pending_epoch)
        except Exception:  # noqa: BLE001 - keep the observation pending for retry
            log.exception("file watcher: access-policy reconciliation failed")

    # ---- debounce loop ----

    def _run_dispatch(self) -> None:
        from .governance.principal import library_scope

        # An owner-local maintenance worker: no request principal crosses into this thread.
        with library_scope():
            self._dispatch()

    def _dispatch(self) -> None:
        if self._dispatch_waits_for_seed:
            # Observation is armed before the long seed.  Keep those events in
            # the coalescing buffer until both scope maps are published, then
            # replay them against the live generation instead of dropping them
            # while ``on_files_changed`` has no authoritative map to patch.
            while not self._stop.is_set() and not self._seed_published.wait(0.1):
                pass
            if not self._stop.is_set():
                try:
                    # Admission waits for this catch-up flush, not merely for
                    # replacement-map publication.  Every event observed while
                    # the seed was walking is therefore reflected before the
                    # catalogue warm can prove a checkpoint.
                    self._flush()
                except Exception:  # noqa: BLE001 - failed catch-up is a failed seed
                    self._seed_succeeded = False
                    log.exception("file watcher: startup event catch-up failed")
                finally:
                    self._seed_complete.set()
        while not self._stop.is_set():
            self._wake.wait(timeout=self._held_publication_intent_timeout())
            if self._stop.is_set():
                break
            # Wait for a quiet window so a burst of saves (or a git pull) coalesces
            # into one batch instead of one upsert per FS event.
            while not self._stop.is_set():
                debounce = self._debounce_seconds()
                time.sleep(debounce)
                with self._lock:
                    quiet = (time.monotonic() - self._last_change) >= debounce
                if quiet:
                    break
            if not self._stop.is_set():
                # Events that arrive during the wait join this batch.
                foreground_priority.give_way()
            self._wake.clear()
            self._flush()
        # Final drain so nothing pending is lost on shutdown.
        self._flush()

    # ---- freshness registry seed + periodic reconcile ----

    def _walk_entries(self, scope: str):
        """(str(abs_path), mtime_ns) pairs for a scope — the same walks
        `find`'s fallback uses, so the seeded triple is walk-identical."""
        from . import find as find_module
        from .vault import walk_vault_md

        if scope == "vault":
            paths = walk_vault_md(self._vault_root)
        else:
            paths = find_module._walk_md(self._kb_root) if self._kb_root.is_dir() else ()
        for p in foreground_priority.yielding_in_bulk(paths):
            try:
                yield (str(p), freshness.stat_signature(p))
            except OSError:
                continue

    @_background_graph_caller
    def _reconcile_once(self, *, seed: bool) -> bool:
        """Re-derive the freshness maps from a fresh walk. `seed=True` on the
        first pass installs the maps and marks the scopes live; later passes
        heal any drift from a missed watchdog event AND dispatch that drift
        delta through the same event fan-out `_flush` uses, so the derived
        indexes (resolver, bm25, keyword, embeddings) heal off the query path
        instead of rebuilding lazily on the next `find`.

        Ordering matters: every scope's registry map is replaced FIRST (below),
        THEN the deduped union of the deltas is dispatched — so a query racing
        the short dispatch window pays at most today's rebuild cost, never
        worse. Seed is NOT drift: the boot pass dispatches nothing (it must not
        re-embed the whole vault)."""
        if self._importer is not None:
            return self._sweep_store()
        if seed and not self._stop.is_set():
            self._raw_witness.activate()
            self._raw_witness.withdraw()
        self._flush_raw(census=True)
        changed_union: dict[str, None] = {}  # insertion-ordered dedupe across scopes
        deleted_union: dict[str, None] = {}
        drifted = False
        baselines_current = True
        pending_epoch = None if seed else freshness.external_pending_epoch(self._vault_root)
        # The periodic pass is bulk work: its walks wait for a request in flight, one budget across
        # its scopes. Only the walk runs in the scope (the replacement lock it holds is waited on
        # inside `awaiting_bulk`); the seed gates admission, so it never yields.
        budget = None if seed else foreground_priority.PassBudget()
        with contextlib.nullcontext() if seed else foreground_priority.bulk(budget=budget):
            for scope in freshness.SCOPES:
                if self._stop.is_set():
                    baselines_current = False
                    break
                try:
                    if seed:
                        # Bracket the walk itself, not the outer per-scope `try`,
                        # so the pending mark clears on EVERY exit — including the
                        # `except Exception` below catching a mid-walk failure —
                        # and never covers `freshness.reconcile`'s own periodic
                        # (non-seed) pass, which this seam is not about.
                        freshness.announce_seed_pending(self._vault_root, scope)
                        try:
                            freshness.seed(self._vault_root, scope, self._walk_entries(scope))
                        finally:
                            freshness.clear_seed_pending(self._vault_root, scope)
                    else:
                        delta = freshness.reconcile(
                            self._vault_root,
                            scope,
                            self._walk_entries(scope),
                            publication_guard=self._freshness_manager.consistency_guard(
                                self._vault_root,
                                operation="watcher_reconcile_freshness",
                                holder_kind="background",
                            ),
                        )
                        if not delta.published:
                            baselines_current = False
                        if delta.drifted:
                            drifted = True
                            # vault ⊇ kb, so a KB file lands in both deltas — dedupe
                            # to dispatch it at most once per cycle.
                            for sp in delta.changed:
                                changed_union.setdefault(sp, None)
                            for sp in delta.deleted:
                                deleted_union.setdefault(sp, None)
                except Exception:  # noqa: BLE001 — reconcile must never kill the watcher
                    baselines_current = False
                    log.exception("file watcher: freshness reconcile failed (scope=%s)", scope)
        if not seed:
            try:
                media_processing.reconcile_all_media(
                    self._vault_root,
                    limit=RECONCILE_MAX_MEDIA_FILES,
                    reconcile_one=lambda binary: _reconcile_background_media(
                        self._vault_root, binary
                    ),
                )
            except Exception:  # noqa: BLE001 - discovery must never kill the watcher
                log.exception("file watcher: periodic media reconciliation failed")
        if seed:
            seeded = baselines_current and all(
                freshness.recall_is_live(self._vault_root, scope)
                for scope in freshness.SCOPES
            )
            if seeded:
                from . import lexstore

                # Seeding must not wait for catalog publication. A busy skip
                # leaves any later repair to the serving worker.
                lexstore.rebase_current_catalog_lineage(
                    self._vault_root, publication_timeout=0.0
                )
            return seeded
        policy = self._watcher_policy()
        drift_admission = 0
        if drifted:
            # Maps are healed; fan the drift delta out. Each step is
            # belt-and-suspenders exception-safe — a bad batch must never kill
            # the reconcile loop.
            try:
                drift_admission = self._dispatch_reconcile_delta(
                    list(changed_union), list(deleted_union), policy
                )
            except Exception:  # noqa: BLE001
                log.exception("file watcher: reconcile drift dispatch failed")
        if pending_epoch is not None and baselines_current:
            self._recover_external_pending(pending_epoch)
        if baselines_current and not freshness.external_pending(self._vault_root):
            self._recover_suspended_graph()
        remaining = (
            None
            if policy.max_reconcile_embed_files is None
            else max(0, policy.max_reconcile_embed_files - drift_admission)
        )
        drain_limit = _background_deferred_limit(policy, remaining)
        foreground_priority.give_way(budget)
        try:
            index_sync.drain_deferred_work(self._vault_root, limit=drain_limit)
        except Exception:  # noqa: BLE001 - queued work remains retryable
            log.exception("file watcher: deferred index drain failed")
        if baselines_current:
            self._prime_inbound_if_needed()
            self._publish_census_if_needed()
        if not drifted:
            return baselines_current
        if policy.defer_expensive_indexes:
            log.info("file watcher: quiet reconcile deferred expensive warm-up")
            return baselines_current
        from . import bm25

        for scope in freshness.SCOPES:
            foreground_priority.give_way(budget)
            try:
                bm25.warm(self._vault_root, scope)
            except Exception:  # noqa: BLE001
                log.exception("file watcher: reconcile bm25 warm failed (scope=%s)", scope)
        return baselines_current

    def _sweep_store(self) -> bool:
        """Store mode's reconcile pass: hash every owned file against the store, then the census.

        The first pass also settles what changed while the service was down (git, a restore). True
        means that the sweep ran to its end; a failed sweep is retried at the next interval.
        """
        importer = self._importer
        assert importer is not None
        try:
            self._after_import(importer.sweep())
        except Exception:  # noqa: BLE001 - a failed sweep is retried at the next interval
            log.exception("file watcher: the store sweep failed")
            return False
        self._publish_census_if_needed()
        return True

    def arm_optional_recovery(self) -> None:
        """Permit optional metadata work only after runtime workers have started."""
        self._optional_recovery_armed = True

    def _prime_inbound_if_needed(self) -> None:
        from . import foreground_priority, readiness, vault

        if (
            not self._optional_recovery_armed
            or self._stop.is_set()
            or readiness.is_warming()
        ):
            return
        try:
            with foreground_priority.bulk(stop=self._stop):
                vault.prime_inbound_index(
                    self._vault_root, eager=mode.preload_cpu_caches(),
                )
        except foreground_priority.BulkCancelled:
            return
        except Exception:  # noqa: BLE001 - optional links cannot stop recovery
            log.exception("file watcher: optional inbound warm failed")

    def _publish_census_if_needed(self) -> None:
        """Rebuild the free-text census that requests read, off the request path."""
        from . import foreground_priority, readiness
        from .governance import artifact_census

        if (
            not self._optional_recovery_armed
            or self._stop.is_set()
            or readiness.is_warming()
        ):
            return
        try:
            with foreground_priority.bulk(stop=self._stop):
                artifact_census.rebuild_published(
                    self._vault_root,
                    eager=mode.preload_cpu_caches(),
                    between=foreground_priority.yield_to_foreground,
                )
        except foreground_priority.BulkCancelled:
            return
        except Exception:  # noqa: BLE001 - requests keep walking the vault
            log.exception("file watcher: free-text census rebuild failed")

    def finish_startup_recovery(self) -> None:
        """Drain deferred whole-index work after required admission.

        Projection seeding is the startup critical path.  Graph validation and
        deferred fan-out can be substantial, so runtime activation calls this
        only after retrieval and semantic state have reached a terminal
        admission result.  The method is idempotent for liveness/timer races.
        """
        with self._lock:
            if self._startup_recovery_started:
                return
            self._startup_recovery_started = True
        from . import foreground_priority

        try:
            with foreground_priority.bulk(stop=self._stop):
                if (
                    self._importer is None and self._current_startup_graph(finished=True) is None
                    and not self._validate_existing_graph_on_seed()
                ):
                    return
        except foreground_priority.BulkCancelled:
            log.info("file watcher: startup graph recovery cancelled")
            return
        if self._stop.is_set():
            return
        policy = self._watcher_policy()
        full_limit = _background_deferred_limit(
            policy, policy.max_reconcile_embed_files
        )
        try:
            index_sync.drain_deferred_work(
                self._vault_root,
                limit=full_limit,
            )
        except Exception:  # noqa: BLE001 - queued work remains retryable
            log.exception("file watcher: startup deferred index drain failed")

    def _recover_external_pending(self, pending_epoch: int) -> None:
        """Recover one drained watcher epoch after exact periodic baselines.

        A persistent corpus-publication failure deliberately makes every event
        consumer cold while preserving the observed external epoch. The next
        periodic full walk can establish new exact baselines even when there is
        no old map from which to report drift. Withdraw every mutable cache and
        graph marker before compare-and-ack, then rebuild the graph directly.
        A newer watcher event keeps the vault pending and prevents publication.
        """
        if freshness.external_pending_epoch(self._vault_root) is None:
            return
        from . import deferred_index, epistemic_graph, graph_sync
        from . import find as find_module
        from . import vault as vault_module

        graph = epistemic_graph.EpistemicGraphIndex(self._vault_root)
        graph_exists = graph.path.exists()
        rebuild_graph = epistemic_graph.graph_enabled() and graph_exists
        try:
            decline = epistemic_graph.recovery_decline_reason(self._vault_root) if rebuild_graph else None
            marker_owns_repair = decline == epistemic_graph.RECOVERY_DECLINE_MARKER_OWNS_REPAIR
            if decline == epistemic_graph.RECOVERY_DECLINE_QUEUE_OWNS_REPAIR:
                observation = freshness.external_observation_stamp()
                paths = freshness.external_pending_paths(self._vault_root)
                if 0 < len(paths) <= graph_sync._PATH_LIMIT:
                    rels = {
                        rel for path in paths
                        if (rel := freshness.registry_rel(self._vault_root, path)) is not None
                    }
                    receipts = deferred_index.snapshot_graph(self._vault_root, paths=rels)
                    if (
                        {receipt.rel_path for receipt in receipts} == rels
                        and freshness.external_pending_only_for(self._vault_root, rels)
                        and graph.drain_epoch_admission() is not False
                        and freshness.external_pending_epoch(self._vault_root) == pending_epoch
                        and freshness.external_observation_stamp() == observation
                    ):
                        # The ordinary drain at the end of reconcile can commit
                        # each observed path before retiring its exact epoch.
                        return
            find_module.evict_resolver_caches(self._vault_root)
            vault_module.evict_inbound_index(self._vault_root)
            if graph_exists and not marker_owns_repair:
                graph.withdraw_availability()
        except Exception:  # noqa: BLE001 - leave the epoch dirty for the next cycle
            log.exception("file watcher: pending epoch recovery preparation failed")
            return

        freshness.clear_external_pending(self._vault_root, through=pending_epoch)
        if freshness.external_pending(self._vault_root) or not rebuild_graph or marker_owns_repair:
            return
        if epistemic_graph.publication_refusal_active(self._vault_root):
            # The same publication was refused recently; re-paying a full
            # rebuild every 300 s is exactly what the contract's R2 forbids.
            # The persisted barrier keeps the graph fenced meanwhile.
            return
        try:
            graph.rebuild_all()
            if not graph.available():
                raise epistemic_graph.GraphPublicationUnavailable(
                    "rebuilt graph did not publish an available marker"
                )
        except Exception as error:  # noqa: BLE001 - retain a retry signal and fail closed
            if isinstance(error, graph_sync.GraphRebuildInProgress):
                log.info("file watcher: graph recovery joined an active external owner")
                return
            # A refused publication is Class B: the registry observed and
            # recorded every event, and only this projection failed to publish.
            # Re-marking here would allocate a fresh external-pending epoch on
            # every cycle and defeat the compare-and-ack above (contract R1),
            # which is why this loop kept the vault permanently pending.
            if epistemic_graph.may_mark_external_pending(error):
                if not freshness.external_pending(self._vault_root):
                    freshness.mark_external_pending(self._vault_root)
            else:
                epistemic_graph.record_publication_recovery_state(self._vault_root)
            log.exception("file watcher: pending epoch graph recovery failed")

    def _recover_suspended_graph(self) -> None:
        """Repair a persisted graph barrier left by a crash or failed fan-out.

        The body moved to `epistemic_graph.recover_suspended_graph` so the graph
        drain daemon can run it too: a stopped rebuild is terminal, and this
        periodic lane is 300s and optional, which left the barrier standing
        indefinitely wherever the watcher was absent.
        """
        from . import epistemic_graph

        epistemic_graph.recover_suspended_graph(self._vault_root)

    def _await_admissible_graph(self, graph) -> bool | Literal["full_marker_required"]:
        """Wait for the actual writer, or use the graph owner's fenced verdict."""
        from . import foreground_priority

        while True:
            foreground_priority.check_cancelled()
            result = graph.validate_startup_snapshot()
            if result is not None:
                return result
            foreground_priority.wait_for_retry(0.25)

    def _validate_existing_graph_on_seed(self) -> bool:
        """Recover stable startup incoherence after exact disk baselines."""
        from . import epistemic_graph

        if (
            not epistemic_graph.graph_enabled()
            or not epistemic_graph.sidecar_path(self._vault_root).exists()
        ):
            return True
        graph = epistemic_graph.EpistemicGraphIndex(self._vault_root)
        return graph.recover_startup_graph(self._await_admissible_graph(graph))

    def _dispatch_reconcile_delta(
        self,
        changed: list[str],
        deleted: list[str],
        policy: mode.WatcherPolicy | None = None,
    ) -> int:
        """Fan a reconcile drift delta out through the per-batch event path.

        Mirrors `_flush` MINUS the freshness publish — `reconcile` already
        replaced the freshness map, so re-publishing it would be redundant. The
        delta paths are the registry map's own absolute-path-string keys.

        The kb/vault scope walks that produced `changed`/`deleted` are two
        separate, non-atomic snapshots — a file deleted+recreated (or vice
        versa) between them can land in BOTH lists. Resolve that split-brain by
        trusting the filesystem NOW: a path present in both is routed by
        `Path(sp).exists()` (exists -> changed only, absent -> deleted only), so
        a live file never loses its index rows to a delete dispatched after its
        upsert.

        A path that matches a still-live self-write registration is dropped:
        the writer already fanned it out via `register_self_write`, so
        re-dispatching would double-embed (normally moot — the 30s suppression
        TTL has expired by the 300s reconcile — but correct under a tight
        race).

        The abs-string guard above only catches a conflict when both event
        forms are the literal SAME string. Two DIFFERENT abs-path forms of one
        file (e.g. a Windows 8.3 short name vs. the long form, #126) evade it
        but can still collapse to the SAME rel once `_rel()` resolves them —
        so the same split-brain tie-break is repeated at the rel level below,
        after computing `up_rels`/`del_rels`: a rel the filesystem still has on
        disk is a change, not a delete."""
        changed_set = set(changed)
        deleted_set = set(deleted)
        for sp in changed_set & deleted_set:
            from .originals import evicted

            if Path(sp).exists() or evicted(self._vault_root, Path(sp)):
                deleted_set.discard(sp)
            else:
                changed_set.discard(sp)

        changed_paths = [
            p
            for p in (Path(sp) for sp in changed_set)
            if not _is_self_write_event(self._vault_root, p, deleted=False)
        ]
        deleted_paths = [
            p
            for p in (Path(sp) for sp in deleted_set)
            if not _is_self_write_event(self._vault_root, p, deleted=True)
        ]
        changed_rel_pairs: list[tuple[Path, str]] = []
        for p in changed_paths:
            r = self._rel(p)
            if r:
                changed_rel_pairs.append((p, r))
        up_rels = [r for _, r in changed_rel_pairs]
        del_rels = [r for r in (self._rel(p) for p in deleted_paths) if r]

        rel_conflicts = set(up_rels) & set(del_rels)
        if rel_conflicts:
            exists_now = {r for r in rel_conflicts if (self._vault_root / r).is_file()}
            gone_now = rel_conflicts - exists_now
            if exists_now:
                del_rels = [r for r in del_rels if r not in exists_now]
            if gone_now:
                up_rels = [r for r in up_rels if r not in gone_now]
                changed_rel_pairs = [(p, r) for p, r in changed_rel_pairs if r not in gone_now]
                changed_paths = [p for p, _ in changed_rel_pairs]

        return self._dispatch_batch(
            changed_paths,
            up_rels,
            del_rels,
            cap=True,
            publish_corpus_change=False,
            pending_epoch=None,
            policy=policy,
        )

    def _suspend_reads_for_acknowledgement(self, candidate) -> None:
        """Withdraw graph availability for compare-and-ack, waiting out a busy boundary.

        The withdrawal has to happen before `clear_external_pending`: a
        same-metadata edit can leave the projection identity unchanged, so the
        epoch must not be retired while stale edges are still publicly
        readable. That ordering is correct and is not what is being changed
        here -- a failed withdrawal must still keep the epoch pending.

        What changes is treating a *retryable* refusal as a refusal at all. The
        boundary this contends for is usually held by
        `epistemic_graph_drain_paths`, the incremental drain repairing the very
        pages this dispatch is reacting to, and the refusal says so: it carries
        `status: "retryable"` and a `retry_after_ms` that nothing was reading.
        Honouring it costs a few seconds on a background thread; abandoning it
        cost every subsequent write a whole-vault rebuild, because the epoch
        stayed pending, the flag declined the graph's read snapshot, and the
        dispatch's predecessor probe could no longer prove anything.

        The retry itself, and its bound, are `_suspend_reads_within_budget`;
        exhausting it leaves the epoch pending for periodic recovery.
        """
        self._suspend_reads_within_budget(
            candidate,
            context="graph availability withdrawal",
            exhausted="epoch stays pending for periodic recovery",
        )

    def _suspend_reads_within_budget(
        self, candidate, *, context: str, exhausted: str
    ) -> None:
        """Take the read barrier, waiting out a retryable refusal inside the budget.

        One attempt is not a bound, it is a coin toss against whatever holds the
        boundary. The refusal says so itself: it carries `status: "retryable"`
        and a `retry_after_ms` the caller is meant to honour.

        The wall-clock bound is `GRAPH_WITHDRAWAL_RETRY_SECONDS` plus one
        mutation-coordinator timeout, roughly 20 s at today's defaults, because
        the deadline is checked only after an attempt has already spent that
        timeout being refused. See the constant for why it is stated rather
        than tightened. A non-retryable failure propagates unchanged, and
        exhausting the budget still raises -- but it says so, because sustained
        contention is a different condition from losing one race and should not
        look like it in a log.
        """
        from . import foreground_priority
        from .cli_ops import OpError

        deadline = time.monotonic() + GRAPH_WITHDRAWAL_RETRY_SECONDS
        attempts = 0
        while True:
            foreground_priority.check_cancelled()
            attempts += 1
            try:
                candidate.suspend_reads()
                if attempts > 1:
                    log.info(
                        "file watcher: %s succeeded after waiting out a busy "
                        "boundary attempts=%s",
                        context,
                        attempts,
                    )
                return
            except OpError as error:
                if error.details.get("status") != "retryable":
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    holder = error.details.get("holder") or {}
                    log.warning(
                        "file watcher: %s gave up on a busy boundary attempts=%s "
                        "holder=%s; %s",
                        context,
                        attempts,
                        holder.get("operation") if isinstance(holder, dict) else None,
                        exhausted,
                    )
                    raise
                advised_ms = error.details.get("retry_after_ms")
                advised = (
                    float(advised_ms) / 1000.0
                    if isinstance(advised_ms, (int, float))
                    else 0.25
                )
                foreground_priority.wait_for_retry(max(0.01, min(remaining, advised)))

    def _dispatch_batch(
        self,
        ups: list[Path],
        up_rels: list[str],
        del_rels: list[str],
        *,
        cap: bool,
        publish_corpus_change: bool = True,
        pending_epoch: int | None = None,
        policy: mode.WatcherPolicy | None = None,
    ) -> int:
        """Shared fan-out tail for `_flush` and `_dispatch_reconcile_delta`:
        inbound publish -> resolver publish -> KB-filtered index_sync
        upsert/delete -> cold corpus warm -> semantic posthoc report. Each step
        keeps its own exception guard — a bad batch must never kill the watcher
        or the reconcile loop.

        `cap`: when True (reconcile only), the KB re-embed list fed to
        `index_sync.upsert_after_write` is bounded by
        `RECONCILE_MAX_EMBED_FILES` and logs when exceeded — the resolver and
        inbound publishes still get the FULL lists regardless of `cap`; only
        the embed dispatch is ever bounded, and the freshness/registry maps are
        always fully healed independent of this cap.
        """
        if not (up_rels or del_rels):
            return 0

        observed = (
            freshness.external_pending_path_epochs(self._vault_root, (*ups, *del_rels))
            if pending_epoch is not None else {}
        )
        # Publish the complete vault-wide batch before any path-local consumer
        # sees it. A transient failure retries this same batch; a persistent
        # failure makes every checkpoint consumer cold before fan-out.
        publication_current = True
        if publish_corpus_change:
            # A cold corpus is warmed after the apply below, not here: from this
            # publication until the lexical apply, the catalogue is behind the
            # registry and readiness refuses, and a cold build takes a minute
            # on a live vault.
            publication_current = index_sync.publish_corpus_delta(
                self._vault_root,
                changed=ups,
                deleted=[self._vault_root / rel for rel in del_rels],
                attempts=2,
                populate_on_miss=False,
            )

        kb_ups = [p for p in ups if self._is_kb(p)]
        semantic_kb_ups: list[Path] = []
        admission_failed = False
        if kb_ups:
            try:
                from . import recall_policy

                # A watchdog event can race the final stat after an editor's
                # atomic rename.  Keep that legacy unknown case in the semantic
                # accounting; live Records are classified (without opening the
                # body) and therefore never consume a cap.
                semantic_kb_ups = [
                    path
                    for path in kb_ups
                    if not path.exists()
                    or recall_policy.is_recall_candidate(self._vault_root, path)
                ]
            except Exception:  # noqa: BLE001 — fail closed for expensive work
                admission_failed = True
                log.exception("file watcher: semantic admission evaluation failed")
        # Whether this batch can change a graph row. The graph derives rows only
        # from recall candidates (the same test its purge uses), so a batch of
        # existing KB files that recall policy rejects -- the Records views a
        # collection write republishes -- cannot leave an edge stale.
        # Withdrawing availability for one raised a barrier no repair would lift,
        # and the drain answered it with a whole-vault rebuild of a current
        # graph. Deletions, files outside KB (the resolver is vault-wide) and an
        # unreadable admission all keep the withdrawal.
        graph_input = (
            bool(del_rels)
            or admission_failed
            or bool(semantic_kb_ups)
            or len(kb_ups) < len(ups)
        )

        # Inbound + resolver: the whole vault (both index sibling folders). The
        # resolver patch also restamps its freshness triple, so the next graph
        # query HITS the cache instead of paying the full-vault rebuild.
        from . import vault as vault_module

        inbound_current = True
        try:
            vault_module.on_inbound_files_changed(self._vault_root, up_rels, del_rels)
        except Exception:  # noqa: BLE001
            inbound_current = False
            try:
                vault_module.evict_inbound_index(self._vault_root)
            except Exception:  # noqa: BLE001 - pending epoch remains the safety boundary
                pass
            log.exception("file watcher: inbound publish failed")
        from . import find as find_module

        resolver_current = True
        try:
            find_module.on_resolver_files_changed(self._vault_root, up_rels, del_rels)
        except Exception:  # noqa: BLE001
            resolver_current = False
            try:
                find_module.evict_resolver_caches(self._vault_root)
            except Exception:  # noqa: BLE001 - pending epoch remains the safety boundary
                pass
            log.exception("file watcher: resolver publish failed")

        # Withdraw the graph marker before retiring observations from this
        # batch. A same-metadata edit can preserve the projection identity.
        # The graph drain owns observations that still have receipts.
        from . import epistemic_graph

        guarded_graph: epistemic_graph.EpistemicGraphIndex | None = None
        ack_ready = (
            pending_epoch is not None
            and publication_current
            and inbound_current
            and resolver_current
        )
        if ack_ready and (
            graph_input or not freshness.external_pending_only_for(self._vault_root, ups)
        ):
            candidate = epistemic_graph.EpistemicGraphIndex(self._vault_root)
            if candidate.path.exists():
                try:
                    self._suspend_reads_for_acknowledgement(candidate)
                except Exception:  # noqa: BLE001 - retain pending for periodic recovery
                    ack_ready = False
                    log.exception("file watcher: graph availability withdrawal failed")
                else:
                    guarded_graph = candidate
        if ack_ready and pending_epoch is not None:
            from . import deferred_index

            observed_rels = {
                path: freshness.registry_rel(self._vault_root, path) for path in observed
            }
            # The queue owns a path's repair only while a graph drain can
            # actually commit it. With the index disabled or its sidecar
            # absent every receipt is unowned dead work — the same
            # `rebuild_graph` gate `_recover_external_pending` applies — so
            # retaining those marks would fence reads until the next periodic
            # reconcile for no repair that can ever run.
            drain_owns_repair = (
                epistemic_graph.graph_enabled()
                and epistemic_graph.EpistemicGraphIndex(self._vault_root).path.exists()
            )
            queued = {
                receipt.rel_path for receipt in deferred_index.snapshot_graph(
                    self._vault_root, paths={rel for rel in observed_rels.values() if rel is not None},
                )
            } if observed_rels and drain_owns_repair else set()
            freshness.retire_external_pending_paths(
                self._vault_root,
                {path: epoch for path, epoch in observed.items()
                 if epoch <= pending_epoch and observed_rels[path] not in queued},
            )

        # Lexstore serves both KB and vault scopes, so it receives the complete
        # vault-wide generation. index_sync keeps every heavier derived lane
        # KB-scoped while applying that one combined lexical mutation.
        kb_del_rels = [r for r in del_rels if managed_split(r) is not None]
        if not (kb_ups or kb_del_rels) and (up_rels or del_rels):
            # Graph sources live in KB, but their resolver is vault-wide. A
            # sibling-folder title/path change can alter KB edge resolution
            # without producing a KB embedding/index event of its own.
            try:
                epistemic_graph.upsert_after_write(
                    self._vault_root,
                    [self._vault_root / rel for rel in (*up_rels, *del_rels)],
                )
            except Exception:  # noqa: BLE001 - graph remains fail-closed by checkpoint
                log.exception("file watcher: vault-wide graph repair failed")
        policy = policy or self._watcher_policy()
        defer_semantic = False
        admitted_semantic = len(semantic_kb_ups)
        if cap and not policy.defer_expensive_indexes:
            max_files = policy.max_reconcile_embed_files
            if max_files is not None and len(semantic_kb_ups) > max_files:
                log.warning(
                    "file watcher: reconcile drift has %d admitted semantic file(s), above "
                    "the cap of %d; identity/purge dispatch stays full and semantic indexing "
                    "is deferred",
                    len(semantic_kb_ups),
                    max_files,
                )
                defer_semantic = True
                admitted_semantic = 0
        elif not cap and not policy.defer_expensive_indexes:
            max_files = policy.max_embed_files_per_batch
            if max_files is not None and len(semantic_kb_ups) > max_files:
                log.warning(
                    "file watcher: live import/sync burst has %d admitted semantic file(s), above "
                    "EXOMEM_WATCHER_MAX_EMBED_FILES=%d; lexical indexes updated but "
                    "semantic indexing deferred. Run `exomem index --scope vault` "
                    "after the import.",
                    len(semantic_kb_ups),
                    max_files,
                )
                defer_semantic = True
                admitted_semantic = 0
        elif policy.defer_expensive_indexes and semantic_kb_ups:
            log.info(
                "file watcher: quiet mode deferring semantic indexing for %d admitted semantic file(s)",
                len(semantic_kb_ups),
            )
            defer_semantic = True
            admitted_semantic = 0
        lexical_batch_complete = False
        graph_batch_owned = False
        if ups or del_rels:
            try:
                report = index_sync.upsert_after_write(
                    self._vault_root,
                    ups,
                    defer_semantic=defer_semantic,
                    publish_corpus_change=False,
                    watcher_deleted_rel_paths=del_rels,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "file watcher: lexical watcher handoff failed for %d path(s)",
                    len(ups) + len(del_rels),
                )
                try:
                    from . import lexstore

                    lexstore.request_repair(
                        self._vault_root, cause=lexstore.RepairCause.WATCHER_HANDOFF_FAILED
                    )
                except Exception:  # noqa: BLE001 - the periodic reconcile remains the final belt
                    pass
            else:
                lexical_batch_complete = any(
                    component.component == "lexstore" and component.outcome == "completed"
                    for component in getattr(report, "components", ())
                )
                try:
                    graph_batch_owned = any(
                        index_sync.graph_repair_is_owned(
                            self._vault_root, component, {*up_rels, *del_rels}
                        )
                        for component in getattr(report, "components", ())
                    )
                except Exception:  # noqa: BLE001 - unproven ownership keeps the mark
                    log.exception("file watcher: graph repair ownership unreadable")
        downstream_del_rels = kb_del_rels
        if downstream_del_rels:
            try:
                index_sync.delete_after_remove(
                    self._vault_root,
                    downstream_del_rels,
                    publish_corpus_change=False,
                    dispatch_lexstore=not lexical_batch_complete,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "file watcher: delete_after_remove failed for %d file(s)",
                    len(downstream_del_rels),
                )
        if (
            guarded_graph is not None
            and epistemic_graph.graph_enabled()
            and not freshness.external_pending(self._vault_root)
        ):
            try:
                graph_current = guarded_graph.available()
            except Exception:  # noqa: BLE001 - availability is the required outcome
                graph_current = False
            if not graph_current:
                # The third door onto `external_pending`, and the one that made
                # the loop self-sustaining: this drain withdrew the marker at
                # the top, compare-and-acked the epoch, and now finds the graph
                # still not current. If the fan-out's publication was *refused*
                # that is Class B -- the registry recorded every event and only
                # this projection failed to publish -- so re-assert the barrier
                # the graph owns instead of allocating a fresh epoch on every
                # drain cycle (contract R1). Any other incompleteness keeps the
                # previous behaviour.
                if epistemic_graph.publication_refusal_active(self._vault_root):
                    epistemic_graph.record_publication_recovery_state(self._vault_root)
                    log.warning(
                        "file watcher: graph publication refused during fan-out; "
                        "graph barrier re-asserted, vault freshness untouched"
                    )
                elif not epistemic_graph.graph_scheduling_enabled():
                    # The graph is deliberately not being maintained, so "not
                    # current" is the configured outcome rather than a failure
                    # to classify. The barrier withdrawn at the top of this
                    # drain still fences every reader; marking on top of it
                    # would cool the registry on every cycle for as long as the
                    # mitigation is deployed, which is exactly the cost this
                    # contract exists to remove.
                    log.info(
                        "file watcher: graph scheduling disabled; graph stays fenced "
                        "by its barrier and vault freshness is untouched"
                    )
                else:
                    # Neither a refusal nor a disabled scheduler: the fan-out ran
                    # and the graph is still not current, for a reason this
                    # branch cannot name. That is the branch a diagnosis
                    # actually lands on, so it carries the state instead of
                    # asserting the outcome.
                    # Scoped to the repair the queue is actually holding. This
                    # branch fires on every drain that leaves the graph behind,
                    # and an unscoped mark here fenced the *next* governed write
                    # into a whole-vault rebuild, whose deferral queued more
                    # paths, which left the next fan-out incomplete: the loop
                    # `seamless-managed-worker-handoff` exists to break. A scope
                    # this helper cannot establish still fences the whole vault.
                    # A repair the graph already owes is not an unknown edit:
                    # the drain converges it, and a mark only fences writes.
                    owned = _graph_repair_owned(self._vault_root, batch_owned=graph_batch_owned)
                    if owned:
                        log.info(
                            "file watcher: graph fan-out incomplete; its repair is already "
                            "owned %s",
                            _graph_incompleteness_fields(self._vault_root),
                        )
                    else:
                        scope = (
                            _graph_incompleteness_scope(self._vault_root, (*up_rels, *del_rels))
                            if owned is False
                            else None
                        )
                        freshness.mark_external_pending(
                            self._vault_root, paths=scope if scope is not None else ()
                        )
                        log.warning(
                            "file watcher: graph fan-out incomplete; periodic recovery "
                            "re-armed %s mark_scope=%s",
                            _graph_incompleteness_fields(self._vault_root),
                            "paths" if scope is not None else "full",
                        )
        # After every consumer has applied the batch. Both steps may build the
        # whole corpus, and neither changes what the consumers apply: posthoc
        # reads the Markdown and the vault's own definitions, not any index.
        if publish_corpus_change and publication_current:
            try:
                semantic_contract.populate_cold_corpus_context(self._vault_root)
                self._flush_raw()
            except Exception:  # noqa: BLE001 - a cold corpus is built by its next reader
                log.exception("file watcher: corpus warm after the batch failed")
        if semantic_kb_ups:
            try:
                posthoc = semantic_writes.evaluate_posthoc_batch(
                    self._vault_root,
                    paths=semantic_kb_ups,
                    operation="watcher",
                )
                summary = posthoc.as_dict()
                logger = log.warning if summary["semantic_contract_findings"] else log.info
                logger(
                    "file watcher: semantic posthoc batch %s",
                    json.dumps(summary, ensure_ascii=True, sort_keys=True),
                )
            except Exception:  # noqa: BLE001 — posthoc reporting never blocks fan-out
                log.exception("file watcher: semantic posthoc evaluation failed")
        return admitted_semantic

    def _run_reconcile(self) -> None:
        # Seed immediately (off the boot path — this is the watcher's own
        # daemon thread), then re-walk every RECONCILE_INTERVAL to bound drift.
        succeeded = False
        try:
            succeeded = self._reconcile_once(seed=True)
            carried = self._current_startup_graph()
            if succeeded and carried is not None:
                carried.bind(self._vault_root)
        except Exception:  # noqa: BLE001 - completion must unblock activation
            log.exception("file watcher: startup freshness seed failed")
        finally:
            self._seed_succeeded = succeeded
            self._seed_published.set()
            if not self._dispatch_waits_for_seed:
                self._seed_complete.set()
        while not self._stop.wait(self._reconcile_interval_seconds()):
            self._reconcile_once(seed=False)
            try:
                from . import brief_watermarks, served_context

                served_context.sweep(self._vault_root)
                brief_watermarks.sweep(self._vault_root)
            except Exception:  # noqa: BLE001 - private-record retention never stops watcher convergence
                log.debug("served context retention unavailable")

    @property
    def seeds_recall_projections(self) -> bool:
        """Whether the startup pass seeds the recall projections: a store-mode watcher's first pass is the
        importer's whole-vault sweep, which seeds none, so nothing waits on it for recall."""
        return self._importer is None

    def wait_until_seeded(self, timeout: float | None = None) -> bool:
        """Wait for the startup seed's terminal result.

        ``False`` means either the wait timed out or at least one recall scope
        failed to become authoritative.  Callers must then keep retrieval
        unadmitted; they must never turn the failed seed into reader-thread
        fallback work.
        """
        if not self._seed_complete.wait(timeout):
            return False
        return self._seed_succeeded

    def _runs_reconcile(self) -> bool:
        # A store-mode sweep protects the store's pages, so it runs where event indexing is off.
        return self._importer is not None or freshness.event_indexes_enabled()

    def _start_reconcile_thread(self) -> None:
        self._reconcile_thread = threading.Thread(
            target=self._run_reconcile,
            name="kb-freshness-reconcile",
            daemon=True,
        )
        self._reconcile_thread.start()

    # ---- lifecycle ----
    def start(self) -> bool:
        """Start dispatch and seed, or reconcile-only polling without an observer."""
        with self._lifecycle_lock:
            return self._start()

    def _start(self) -> bool:
        observer_alive = getattr(self._observer, "is_alive", None)
        # A pending promotion arm owns observer liveness itself: `start` then
        # re-judges it through `arming.is_alive()` and the observation stamp,
        # which a degraded backend fails even while the observer thread lives.
        if any(
            worker is not None and worker.is_alive() for worker in (self._thread, self._reconcile_thread)
        ) or (
            self._observer_arming is None and observer_alive is not None and observer_alive()
        ):
            log.warning("file watcher: a previous worker is still running; restart deferred")
            return False
        self._current_startup_graph()
        if not self._vault_root.is_dir() or not self._kb_root.is_dir():
            return False
        arming = self._observer_arming
        if arming is not None:
            if self._stop.is_set():
                return False
            observation_started = arming.is_alive() or self.promotion_observation_stamp() is not None
        else:
            observation_started = self._arm_observer()
        if not observation_started:
            if not self._runs_reconcile():
                return False
            self._start_reconcile_thread()
            return True
        self._dispatch_waits_for_seed = self._importer is None and freshness.event_indexes_enabled()
        self._thread = threading.Thread(
            target=self._run_dispatch, name="kb-file-watcher", daemon=True
        )
        self._thread.start()
        if self._runs_reconcile():
            self._start_reconcile_thread()
        log.info("file watcher started on %s", self._vault_root)
        return True

    def _arm_observer(self, *, reset: bool = True) -> bool:
        """Start the normal observer while dispatch and seeding stay deferred."""
        if self._observer is not None:
            return self._observer.is_alive()
        if not self._vault_root.is_dir():
            log.info("file watcher: %s not found; not watching", self._vault_root)
            return False
        kb_root = managed_root(self._vault_root)
        if not kb_root.is_dir():
            log.info("file watcher: %s not found; not watching", kb_root)
            return False

        if reset:
            # A restarted watcher gets a new stop signal. A rebuild from the
            # previous run must still see its own run's shutdown.
            self._raw_witness = capture_inventory.new_raw_witness(self._vault_root)
            self._stop = foreground_priority.StopEvent()
            self._wake.clear()
            self._seed_published.clear()
            self._seed_complete.clear()
            self._seed_succeeded = False
            self._startup_recovery_started = False
            self._dispatch_waits_for_seed = False

        elif self._stop.is_set():
            return False

        try:
            Observer, FileSystemEventHandler = _import_watchdog()
            from watchdog import events as watchdog_events
        except Exception as e:  # noqa: BLE001 — optional dep
            log.info("file watcher: watchdog not available (%s)", e)
            return False

        watcher = self

        class _Handler(FileSystemEventHandler):
            # The event classes that the on_* methods below consume: a closed set
            # fixed by watchdog's API. As the schedule's event filter, it also
            # narrows the Linux inotify mask, so page reads (open and
            # close-without-write) never reach this process. DirModifiedEvent
            # keeps IN_CLOSE_WRITE in that mask.
            consumed_events = [
                watchdog_events.FileCreatedEvent,
                watchdog_events.DirCreatedEvent,
                watchdog_events.FileModifiedEvent,
                watchdog_events.DirModifiedEvent,
                watchdog_events.FileDeletedEvent,
                watchdog_events.DirDeletedEvent,
                watchdog_events.FileMovedEvent,
                watchdog_events.DirMovedEvent,
            ]

            def on_created(self, event):  # noqa: ANN001
                if event.is_directory:
                    watcher._notify_view_change(Path(event.src_path), directory=True)
                    watcher._note_directories(event.src_path)
                else:
                    watcher._record(Path(event.src_path), deleted=False)

            def on_modified(self, event):  # noqa: ANN001
                if event.is_directory:
                    watcher._notify_view_change(Path(event.src_path), directory=True)
                else:
                    watcher._record(Path(event.src_path), deleted=False)

            def on_deleted(self, event):  # noqa: ANN001
                if event.is_directory:
                    watcher._notify_view_change(Path(event.src_path), directory=True)
                    watcher._note_directories(event.src_path)
                else:
                    watcher._record(Path(event.src_path), deleted=True)

            def on_moved(self, event):  # noqa: ANN001
                watcher._notify_view_change(Path(event.src_path), destination=Path(event.dest_path),
                                            directory=event.is_directory)
                if event.is_directory:
                    watcher._note_directories(event.src_path, event.dest_path)
                if not event.is_directory:
                    watcher._note_move(Path(event.src_path), Path(event.dest_path))
                    watcher._record(Path(event.src_path), deleted=True, notify_views=False)
                    watcher._record(Path(event.dest_path), deleted=False, notify_views=False)

        observer = None
        try:
            observer = Observer()
            # Watch the whole vault: freshness (vault scope) and inbound links
            # index sibling folders too. The embed dispatch in _flush stays
            # KB-filtered, so only Knowledge Base/ markdown is re-embedded.
            observer.schedule(
                _Handler(),
                str(self._vault_root),
                recursive=True,
                event_filter=_Handler.consumed_events,
            )
            observer.start()
        except Exception as e:  # noqa: BLE001 — watcher must never break the server
            log.warning("file watcher: observer failed to start (%s); live re-embed disabled", e)
            self._stop_observer(observer)
            return False
        with self._lock:
            if not self._stop.is_set() and observer.is_alive():
                self._observer = observer
                return True
        self._stop_observer(observer)
        return False

    def arm_promotion_observation(self, *, deadline: float) -> bool:
        """Start normally, but leave a slow backend to this watcher's startup."""
        if self._importer is not None or time.monotonic() >= deadline:
            return False

        def arm() -> None:
            try:
                self._arm_observer(reset=False)
            finally:
                self._observer_armed.set()

        self._observer_arming = threading.Thread(
            target=arm, name="kb-file-observer-arming", daemon=True,
        )
        self._observer_arming.start()
        self._observer_armed.wait(max(0.0, deadline - time.monotonic()))
        return time.monotonic() < deadline and self.promotion_observation_stamp() is not None

    def carry_startup_graph(self, proof) -> None:
        self._carried_graph = proof

    def _current_startup_graph(self, *, finished: bool = False):
        carried = self._carried_graph
        current = carried is not None and carried.handoff_current(self._vault_root, finished=finished)
        if finished or not current:
            self._carried_graph = None
        return carried if current else None

    def promotion_observation_stamp(self) -> tuple[object, int] | None:
        observer = self._observer
        if observer is None or not observer.is_alive() or self._stop.is_set():
            return None
        keep_running = getattr(observer, "should_keep_running", None)
        if keep_running is not None and not keep_running():
            return None
        emitters = getattr(observer, "emitters", None)
        backends = None if emitters is None else frozenset(emitters)
        if backends is not None:
            if not backends or not all(e.is_alive() and e.should_keep_running() for e in backends):
                return None
        with self._raw_witness.lock:
            if self._observer is not observer:
                return None
            return (observer, backends, self._raw_witness.instance), self._raw_witness.epoch

    @staticmethod
    def _stop_observer(observer) -> None:
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(timeout=2)
        except Exception:  # noqa: BLE001 - partially started observers may not be joinable
            log.debug("file watcher: observer stop failed", exc_info=True)

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_workers()

    def _stop_workers(self) -> None:
        self._raw_witness.withdraw(stopped=True)
        with self._lock:
            self._stop.set()
            observer, self._observer = self._observer, None
        self._observer_armed.set()
        self._wake.set()
        self._seed_published.set()
        self._seed_complete.set()
        # No owner keeps the published census current once the watcher stops.
        from .governance import artifact_census

        artifact_census.withdraw(self._vault_root)
        self._stop_observer(observer)
        arming = self._observer_arming
        if arming is not None:
            arming.join(timeout=2)
            if not arming.is_alive():
                self._observer_arming = None
        self._carried_graph = None
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(timeout=2)
