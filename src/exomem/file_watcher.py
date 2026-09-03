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

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from . import freshness, index_sync, media_processing, mode, semantic_writes
from .kbdir import kb_dirname, kb_prefix

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


def _canon_root(vault_root: Path) -> str:
    try:
        return str(vault_root.resolve())
    except OSError:
        return str(vault_root)


def _rel_posix(vault_root: Path, path: Path) -> str | None:
    """Vault-relative POSIX path, tolerant of already-deleted files."""
    try:
        return path.resolve().relative_to(vault_root.resolve()).as_posix()
    except (ValueError, OSError):
        try:
            return path.relative_to(vault_root).as_posix()
        except ValueError:
            return None


def _prune_locked(now: float) -> None:
    for k in [k for k, v in _SELF_UPSERTS.items() if v[2] <= now]:
        _SELF_UPSERTS.pop(k, None)
    for k in [k for k, v in _SELF_DELETES.items() if v <= now]:
        _SELF_DELETES.pop(k, None)
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


def _publish_registry_change(
    vault_root: Path, changed: list[Path], deleted_rels: list[str]
) -> None:
    """Update freshness + inbound for a server-authored change.

    A self-write's watcher echo is suppressed (redundant re-embed), but the
    write DID change the vault — so the freshness/inbound registries must still
    see it, or `find` would serve stale results for that file until the next
    reconcile. A live registry is patched immediately; an in-flight replacement
    seed retains the target state for its final swap (guards inside)."""
    if not freshness.event_indexes_enabled():
        # Kill switch on: don't even pay the resolve() syscalls in _rel_posix.
        return
    index_sync.publish_corpus_delta(
        vault_root,
        changed=changed,
        deleted=deleted_rels,
        attempts=2,
    )
    changed_rels = [r for r in (_rel_posix(vault_root, p) for p in changed) if r]
    try:
        from . import vault as vault_module

        vault_module.on_inbound_files_changed(vault_root, changed_rels, deleted_rels)
    except Exception:  # noqa: BLE001
        log.debug("self-write inbound publish failed", exc_info=True)
    try:
        from . import find as find_module

        find_module.on_resolver_files_changed(vault_root, changed_rels, deleted_rels)
    except Exception:  # noqa: BLE001
        log.debug("self-write resolver publish failed", exc_info=True)


def register_self_write(vault_root: Path, paths: Iterable[Path]) -> None:
    """Record server-authored markdown replacements so their watcher echo is
    dropped. Best-effort: unreadable/gone files are skipped (they simply won't
    be suppressed). Also publishes the change to the freshness/inbound
    registries, since the suppressed watcher echo won't."""
    paths = list(paths)
    root = _canon_root(vault_root)
    now = time.monotonic()
    with _SUPPRESS_LOCK:
        for p in paths:
            p = Path(p)
            if p.suffix.lower() != ".md":
                continue
            rel = _rel_posix(vault_root, p)
            if rel is None:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            _SELF_UPSERTS[(root, rel)] = (
                st.st_mtime_ns,
                st.st_size,
                now + UPSERT_SUPPRESS_TTL_SECONDS,
            )
        _prune_locked(now)
    _publish_registry_change(vault_root, changed=paths, deleted_rels=[])


def register_self_delete(vault_root: Path, rel_paths: Iterable[str]) -> None:
    """Record server-authored markdown removals (delete/trash/move-away) so
    their watcher echo is dropped. TTL-bounded — there is no file left to
    signature-match. Also publishes the removal to the freshness/inbound
    registries, since the suppressed watcher echo won't."""
    rel_paths = list(rel_paths)
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


def _import_watchdog():
    """Import watchdog lazily. Returns (Observer, FileSystemEventHandler).

    Isolated into a tiny function so `start()` can catch a missing dep and so tests
    can patch it to simulate watchdog being absent.
    """
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    return Observer, FileSystemEventHandler


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


class FileWatcher:
    """Watch Knowledge Base/ for `.md` changes and re-embed them, debounced."""

    def __init__(self, vault_root: Path, *, debounce_seconds: float | None = None) -> None:
        self._vault_root = vault_root
        self._kb_root = vault_root / kb_dirname()
        self._debounce_override = debounce_seconds
        self._lock = threading.Lock()
        self._pending_upsert: set[Path] = set()
        self._pending_delete: set[Path] = set()
        self._pending_media: set[Path] = set()
        self._pending_external_epoch = 0
        self._pending_access_policy = False
        self._last_change = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer = None
        self._reconcile_thread: threading.Thread | None = None
        self._seed_published = threading.Event()
        self._seed_complete = threading.Event()
        self._seed_succeeded = False
        self._startup_recovery_started = False
        self._dispatch_waits_for_seed = False

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

    def _record(self, path: Path, *, deleted: bool) -> None:
        """Record a Markdown or supported-media change, coalesced by path."""
        from .vault import in_excluded_scan_dir

        rel = self._rel(path)
        if rel == f"{kb_prefix()}_access.yaml":
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
        if rel is not None and in_excluded_scan_dir(rel):
            # _trash/_archive/_Schema/…: every full walk skips these, so the
            # event path must too — else a delete's move-to-trash re-embeds
            # the trashed note under its trash path.
            return
        if path.suffix.lower() != ".md":
            if deleted or not self._is_kb(path) or media_processing.classify_media(path) is None:
                return
            with self._lock:
                self._pending_media.add(path)
                self._last_change = time.monotonic()
            self._wake.set()
            return
        if _is_self_write_event(self._vault_root, path, deleted=deleted):
            log.debug("file watcher: suppressed self-write echo for %s", path)
            return
        with self._lock:
            if deleted:
                self._pending_upsert.discard(path)
                self._pending_delete.add(path)
            else:
                # A re-create after a delete in the same window is a modify.
                self._pending_delete.discard(path)
                self._pending_upsert.add(path)
            self._pending_external_epoch = max(
                self._pending_external_epoch,
                freshness.mark_external_pending(self._vault_root),
            )
            self._last_change = time.monotonic()
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

    def _flush(self) -> None:
        """Dispatch the coalesced batch: publish freshness/inbound for every
        changed path (vault-wide), and re-embed only the Knowledge Base subset."""
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
            self._wake.wait()
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
        for p in paths:
            try:
                yield (str(p), freshness.stat_signature(p))
            except OSError:
                continue

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
        changed_union: dict[str, None] = {}  # insertion-ordered dedupe across scopes
        deleted_union: dict[str, None] = {}
        drifted = False
        baselines_current = True
        pending_epoch = None if seed else freshness.external_pending_epoch(self._vault_root)
        for scope in freshness.SCOPES:
            if self._stop.is_set():
                baselines_current = False
                break
            try:
                if seed:
                    freshness.seed(self._vault_root, scope, self._walk_entries(scope))
                else:
                    delta = freshness.reconcile(self._vault_root, scope, self._walk_entries(scope))
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
            return baselines_current and all(
                freshness.recall_is_live(self._vault_root, scope)
                for scope in freshness.SCOPES
            )
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
        try:
            index_sync.drain_deferred_work(self._vault_root, limit=drain_limit)
        except Exception:  # noqa: BLE001 - queued work remains retryable
            log.exception("file watcher: deferred index drain failed")
        if not drifted:
            return baselines_current
        if policy.defer_expensive_indexes:
            log.info("file watcher: quiet reconcile deferred expensive warm-up")
            return baselines_current
        from . import bm25

        for scope in freshness.SCOPES:
            try:
                bm25.warm(self._vault_root, scope)
            except Exception:  # noqa: BLE001
                log.exception("file watcher: reconcile bm25 warm failed (scope=%s)", scope)
        return baselines_current

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
        if not self._validate_existing_graph_on_seed():
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
        from . import epistemic_graph, graph_sync
        from . import find as find_module
        from . import vault as vault_module

        graph = epistemic_graph.EpistemicGraphIndex(self._vault_root)
        graph_exists = graph.path.exists()
        rebuild_graph = epistemic_graph.graph_enabled() and graph_exists
        try:
            find_module.evict_resolver_caches(self._vault_root)
            vault_module.evict_inbound_index(self._vault_root)
            if graph_exists:
                graph.withdraw_availability()
        except Exception:  # noqa: BLE001 - leave the epoch dirty for the next cycle
            log.exception("file watcher: pending epoch cache withdrawal failed")
            return

        freshness.clear_external_pending(self._vault_root, through=pending_epoch)
        if freshness.external_pending(self._vault_root) or not rebuild_graph:
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

    def _validate_existing_graph_on_seed(self) -> bool:
        """Validate an existing graph after startup's exact disk baselines.

        A process can die before watchdog delivers an edit or while the event is
        still debouncing, before any in-memory epoch can persist a read barrier.
        Filesystem metadata can also collide across such an edit, so the graph
        does need a proof of source bytes and resolver topology across that
        crash boundary — but a whole-vault REBUILD is not that proof, it is the
        repair. Paying the repair unconditionally cost 12-30 minutes of
        suspended reads on every restart of a 3.3k-file vault, turning every
        restart into an outage.

        Validation is therefore bounded and layered: an O(1) durable check
        first, then a non-suspending source-bytes proof. The second step is
        `available()`, which for a cold reader re-proves the sidecar against
        canonical source bytes and resolver topology — O(corpus) hashing, but
        it neither suspends reads nor rebuilds. Reads are suspended and the
        graph rebuilt only when one of those proofs fails, which is the
        genuine-incoherence case.

        The O(1) check is a conservative negative pre-filter: when durable
        state already proves a rebuild is needed, it short-circuits ahead of
        the source-bytes proof so the expensive hashing is not paid before a
        rebuild that was already certain.
        """
        from . import epistemic_graph, graph_sync
        from . import find as find_module
        from . import vault as vault_module

        if not epistemic_graph.sidecar_path(self._vault_root).exists():
            return True
        graph = epistemic_graph.EpistemicGraphIndex(self._vault_root)
        try:
            if not epistemic_graph.graph_enabled():
                return True
            if graph.durable_checkpoint_is_coherent() and graph.available():
                log.info("file watcher: startup graph validation admitted a coherent graph")
                return True
            graph.suspend_reads()
            find_module.evict_resolver_caches(self._vault_root)
            vault_module.evict_inbound_index(self._vault_root)
            if not index_sync.recover_full_receipt_graph_epoch(self._vault_root, build=False):
                raise RuntimeError("startup graph epoch recovery did not complete")
            graph.rebuild_all()
            if not graph.available():
                raise RuntimeError("startup graph rebuild did not publish availability")
            return True
        except graph_sync.GraphRebuildInProgress:
            # The external owner will publish after the initial startup fence.
            # A second suspension here could land after that publication.
            log.info("file watcher: startup graph validation joined an active external owner")
            return False
        except Exception:  # noqa: BLE001 - persisted barrier keeps reads fail-closed
            try:
                graph.suspend_reads()
            except Exception:  # noqa: BLE001 - rebuild failure already withdrew markers
                pass
            log.exception("file watcher: startup graph validation failed")
            return False

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
            if Path(sp).exists():
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

        A non-retryable failure still propagates unchanged, and exhausting the
        budget still leaves the epoch pending for periodic recovery -- but it
        says so, because sustained contention is a different condition from
        losing one race and should not look like it in a log.

        The wall-clock bound is `GRAPH_WITHDRAWAL_RETRY_SECONDS` plus one
        mutation-coordinator timeout, roughly 20 s at today's defaults, because
        the deadline is checked only after an attempt has already spent that
        timeout being refused. See the constant for why it is stated rather
        than tightened.
        """
        from .cli_ops import OpError

        deadline = time.monotonic() + GRAPH_WITHDRAWAL_RETRY_SECONDS
        attempts = 0
        while True:
            attempts += 1
            try:
                candidate.suspend_reads()
                if attempts > 1:
                    log.info(
                        "file watcher: graph availability withdrawal succeeded after "
                        "waiting out a busy boundary attempts=%s",
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
                        "file watcher: graph availability withdrawal gave up on a busy "
                        "boundary attempts=%s holder=%s; epoch stays pending for "
                        "periodic recovery",
                        attempts,
                        holder.get("operation") if isinstance(holder, dict) else None,
                    )
                    raise
                advised_ms = error.details.get("retry_after_ms")
                advised = (
                    float(advised_ms) / 1000.0
                    if isinstance(advised_ms, (int, float))
                    else 0.25
                )
                time.sleep(max(0.01, min(remaining, advised)))

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
        upsert/delete. Each step keeps its own exception guard — a bad batch
        must never kill the watcher or the reconcile loop.

        `cap`: when True (reconcile only), the KB re-embed list fed to
        `index_sync.upsert_after_write` is bounded by
        `RECONCILE_MAX_EMBED_FILES` and logs when exceeded — the resolver and
        inbound publishes still get the FULL lists regardless of `cap`; only
        the embed dispatch is ever bounded, and the freshness/registry maps are
        always fully healed independent of this cap.
        """
        if not (up_rels or del_rels):
            return 0

        # Publish the complete vault-wide batch before any path-local consumer
        # sees it. A transient failure retries this same batch; a persistent
        # failure makes every checkpoint consumer cold before fan-out.
        publication_current = True
        if publish_corpus_change:
            publication_current = index_sync.publish_corpus_delta(
                self._vault_root,
                changed=ups,
                deleted=[self._vault_root / rel for rel in del_rels],
                attempts=2,
            )

        kb_ups = [p for p in ups if self._is_kb(p)]
        semantic_kb_ups: list[Path] = []
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
                log.exception("file watcher: semantic admission evaluation failed")
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

        # The authoritative registry and resolver now cover the drained
        # external generation. Withdraw an existing graph marker before
        # compare-and-ack: a same-metadata edit can leave its projection
        # identity unchanged, so pending must not disappear while stale edges
        # are still publicly readable. The following fan-out republishes the
        # marker from the exact checkpoint (or rebuilds).
        from . import epistemic_graph

        guarded_graph: epistemic_graph.EpistemicGraphIndex | None = None
        ack_ready = (
            pending_epoch is not None
            and publication_current
            and inbound_current
            and resolver_current
        )
        if ack_ready:
            candidate = epistemic_graph.EpistemicGraphIndex(self._vault_root)
            if candidate.path.exists():
                try:
                    self._suspend_reads_for_acknowledgement(candidate)
                except Exception:  # noqa: BLE001 - retain pending for periodic recovery
                    ack_ready = False
                    log.exception("file watcher: graph availability withdrawal failed")
                else:
                    guarded_graph = candidate
        if ack_ready:
            freshness.clear_external_pending(
                self._vault_root,
                through=pending_epoch,
            )

        # Lexstore serves both KB and vault scopes, so it receives the complete
        # vault-wide generation. index_sync keeps every heavier derived lane
        # KB-scoped while applying that one combined lexical mutation.
        kb_del_rels = [r for r in del_rels if r.startswith(kb_prefix())]
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

                    lexstore.request_repair(self._vault_root)
                except Exception:  # noqa: BLE001 - the periodic reconcile remains the final belt
                    pass
            else:
                lexical_batch_complete = any(
                    component.component == "lexstore" and component.outcome == "completed"
                    for component in getattr(report, "components", ())
                )
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
                    freshness.mark_external_pending(self._vault_root)
                    log.warning(
                        "file watcher: graph fan-out incomplete; periodic recovery re-armed %s",
                        _graph_incompleteness_fields(self._vault_root),
                    )
        return admitted_semantic

    def _run_reconcile(self) -> None:
        # Seed immediately (off the boot path — this is the watcher's own
        # daemon thread), then re-walk every RECONCILE_INTERVAL to bound drift.
        succeeded = False
        try:
            succeeded = self._reconcile_once(seed=True)
        except Exception:  # noqa: BLE001 - completion must unblock activation
            log.exception("file watcher: startup freshness seed failed")
        finally:
            self._seed_succeeded = succeeded
            self._seed_published.set()
            if not self._dispatch_waits_for_seed:
                self._seed_complete.set()
        while not self._stop.wait(self._reconcile_interval_seconds()):
            self._reconcile_once(seed=False)

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

    def _start_reconcile_thread(self) -> None:
        if self._reconcile_thread is not None and self._reconcile_thread.is_alive():
            return
        self._reconcile_thread = threading.Thread(
            target=self._run_reconcile,
            name="kb-freshness-reconcile",
            daemon=True,
        )
        self._reconcile_thread.start()

    # ---- lifecycle ----

    def start(self) -> bool:
        """Start observation, or reconcile-only polling when watchdog is absent."""
        if not self._vault_root.is_dir():
            log.info("file watcher: %s not found; not watching", self._vault_root)
            return False
        kb_root = self._vault_root / kb_dirname()
        if not kb_root.is_dir():
            log.info("file watcher: %s not found; not watching", kb_root)
            return False

        # Hosted quiesce/resume deliberately reuses the watcher instance. A
        # stopped threading.Event is sticky, so reset both loop controls before
        # recreating the dispatch/observer threads.
        self._stop.clear()
        self._wake.clear()
        self._seed_published.clear()
        self._seed_complete.clear()
        self._seed_succeeded = False
        self._startup_recovery_started = False
        self._dispatch_waits_for_seed = False

        try:
            Observer, FileSystemEventHandler = _import_watchdog()
        except Exception as e:  # noqa: BLE001 — optional dep
            if freshness.event_indexes_enabled():
                log.info(
                    "file watcher: watchdog not available (%s); using reconcile-only polling",
                    e,
                )
                self._start_reconcile_thread()
                return True
            log.info("file watcher: watchdog not available (%s); watcher disabled", e)
            return False

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):  # noqa: ANN001
                if not event.is_directory:
                    watcher._record(Path(event.src_path), deleted=False)

            def on_modified(self, event):  # noqa: ANN001
                if not event.is_directory:
                    watcher._record(Path(event.src_path), deleted=False)

            def on_deleted(self, event):  # noqa: ANN001
                if not event.is_directory:
                    watcher._record(Path(event.src_path), deleted=True)

            def on_moved(self, event):  # noqa: ANN001
                if not event.is_directory:
                    watcher._record(Path(event.src_path), deleted=True)
                    watcher._record(Path(event.dest_path), deleted=False)

        self._dispatch_waits_for_seed = freshness.event_indexes_enabled()
        self._thread = threading.Thread(
            target=self._run_dispatch, name="kb-file-watcher", daemon=True
        )
        self._thread.start()
        try:
            self._observer = Observer()
            # Watch the whole vault: freshness (vault scope) and inbound links
            # index sibling folders too. The embed dispatch in _flush stays
            # KB-filtered, so only Knowledge Base/ markdown is re-embedded.
            self._observer.schedule(_Handler(), str(self._vault_root), recursive=True)
            self._observer.start()
        except Exception as e:  # noqa: BLE001 — watcher must never break the server
            log.warning("file watcher: observer failed to start (%s); live re-embed disabled", e)
            self._stop.set()
            self._wake.set()
            self._thread.join(timeout=2)
            self._thread = None
            if freshness.event_indexes_enabled():
                self._dispatch_waits_for_seed = False
                self._stop.clear()
                self._wake.clear()
                self._start_reconcile_thread()
                return True
            return False
        if freshness.event_indexes_enabled():
            self._start_reconcile_thread()
        log.info("file watcher started on %s", self._vault_root)
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._seed_published.set()
        self._seed_complete.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:  # noqa: BLE001
                log.debug("file watcher: observer stop failed", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(timeout=2)
