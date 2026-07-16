"""Live file-watcher — re-embed out-of-band edits in ~1s instead of waiting for `reconcile`.

The vault is edited *around* the server — directly in Obsidian, on mobile, or via a
filesystem write (Obsidian Sync, a git pull). Those bypass the writer hooks, so the
embedding sidecar drifts until someone runs `reconcile`. This watcher closes that gap:
it watches `<vault>/Knowledge Base/` for `.md` changes and re-embeds them through the
SAME `index_sync.upsert_after_write` dispatch the writers (and `reconcile`) use —
deletes go through `index_sync.delete_after_remove`. The dispatch fans out to every
index sidecar (embedding AND lexical), each behind its own availability gates.

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
from collections.abc import Iterable
from pathlib import Path

from . import freshness, index_sync, media_processing, mode, semantic_writes
from .kbdir import kb_dirname, kb_prefix

log = logging.getLogger(__name__)


def _media_mutation_guard(vault_root: Path):
    from .writer_lease import get_manager

    return get_manager().mutation_guard(vault_root)

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
    reconcile. No-op when the registries aren't live (guards inside)."""
    if not freshness.event_indexes_enabled():
        # Kill switch on: don't even pay the resolve() syscalls in _rel_posix.
        return
    try:
        deleted_paths = [vault_root / r for r in deleted_rels]
        freshness.on_files_changed(vault_root, changed=changed, deleted=deleted_paths)
    except Exception:  # noqa: BLE001 — bookkeeping must never break a write
        log.debug("self-write freshness publish failed", exc_info=True)
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
        self._last_change = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer = None
        self._reconcile_thread: threading.Thread | None = None

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

    def _drain(self) -> tuple[list[Path], list[Path], list[str]]:
        with self._lock:
            media = sorted(self._pending_media)
            ups = sorted(self._pending_upsert)
            dels = sorted(self._pending_delete)
            self._pending_media.clear()
            self._pending_upsert.clear()
            self._pending_delete.clear()
        del_rels = [r for r in (self._rel(p) for p in dels) if r]
        return media, ups, del_rels

    def _flush(self) -> None:
        """Dispatch the coalesced batch: publish freshness/inbound for every
        changed path (vault-wide), and re-embed only the Knowledge Base subset."""
        media, ups, del_rels = self._drain()
        if not (media or ups or del_rels):
            return

        for path in media:
            try:
                with _media_mutation_guard(self._vault_root):
                    media_processing.reconcile_media(self._vault_root, path, explicit=False)
            except Exception:  # noqa: BLE001 - a bad artifact must never kill the watcher
                log.exception("file watcher: media reconciliation failed for %s", path)
        if not (ups or del_rels):
            return

        # Freshness: the whole vault, since both index sibling folders too.
        deleted_paths = [self._vault_root / r for r in del_rels]
        try:
            freshness.on_files_changed(self._vault_root, changed=ups, deleted=deleted_paths)
        except Exception:  # noqa: BLE001 — a bad batch must never kill the watcher
            log.exception("file watcher: freshness publish failed")
        up_rels = [r for r in (self._rel(p) for p in ups) if r]
        self._dispatch_batch(ups, up_rels, del_rels, cap=False)

    # ---- debounce loop ----

    def _run_dispatch(self) -> None:
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

    def _reconcile_once(self, *, seed: bool) -> None:
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
        for scope in freshness.SCOPES:
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
                log.exception("file watcher: freshness reconcile failed (scope=%s)", scope)
        if not seed:
            try:
                with _media_mutation_guard(self._vault_root):
                    media_processing.reconcile_all_media(
                        self._vault_root,
                        limit=RECONCILE_MAX_MEDIA_FILES,
                    )
            except Exception:  # noqa: BLE001 - discovery must never kill the watcher
                log.exception("file watcher: periodic media reconciliation failed")
        if seed or not drifted:
            return
        # Maps are healed; fan the drift delta out and pre-warm the triple-keyed
        # bm25 corpus. Each step is belt-and-suspenders exception-safe — a bad
        # batch must never kill the reconcile loop.
        try:
            self._dispatch_reconcile_delta(list(changed_union), list(deleted_union))
        except Exception:  # noqa: BLE001
            log.exception("file watcher: reconcile drift dispatch failed")
        if self._watcher_policy().defer_expensive_indexes:
            log.info("file watcher: quiet reconcile deferred expensive warm-up")
            return
        from . import bm25

        for scope in freshness.SCOPES:
            try:
                bm25.warm(self._vault_root, scope)
            except Exception:  # noqa: BLE001
                log.exception("file watcher: reconcile bm25 warm failed (scope=%s)", scope)

    def _dispatch_reconcile_delta(self, changed: list[str], deleted: list[str]) -> None:
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

        self._dispatch_batch(changed_paths, up_rels, del_rels, cap=True)

    def _dispatch_batch(
        self,
        ups: list[Path],
        up_rels: list[str],
        del_rels: list[str],
        *,
        cap: bool,
    ) -> None:
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
            return

        kb_ups = [p for p in ups if self._is_kb(p)]
        if kb_ups:
            try:
                posthoc = semantic_writes.evaluate_posthoc_batch(
                    self._vault_root,
                    paths=kb_ups,
                    operation="watcher",
                )
                summary = posthoc.as_dict()
                logger = (
                    log.warning
                    if summary["semantic_contract_findings"]
                    else log.info
                )
                logger(
                    "file watcher: semantic posthoc batch %s",
                    json.dumps(summary, ensure_ascii=True, sort_keys=True),
                )
            except Exception:  # noqa: BLE001 — posthoc reporting never blocks fan-out
                log.exception("file watcher: semantic posthoc evaluation failed")

        # Inbound + resolver: the whole vault (both index sibling folders). The
        # resolver patch also restamps its freshness triple, so the next graph
        # query HITS the cache instead of paying the full-vault rebuild.
        try:
            from . import vault as vault_module

            vault_module.on_inbound_files_changed(self._vault_root, up_rels, del_rels)
        except Exception:  # noqa: BLE001
            log.exception("file watcher: inbound publish failed")
        try:
            from . import find as find_module

            find_module.on_resolver_files_changed(self._vault_root, up_rels, del_rels)
        except Exception:  # noqa: BLE001
            log.exception("file watcher: resolver publish failed")

        # Index sidecars (embedding + lexical): Knowledge Base markdown only.
        kb_del_rels = [r for r in del_rels if r.startswith(kb_prefix())]
        policy = self._watcher_policy()
        defer_semantic = False
        if cap and not policy.defer_expensive_indexes:
            max_files = policy.max_reconcile_embed_files
            if max_files is not None and len(kb_ups) > max_files:
                log.warning(
                    "file watcher: reconcile drift re-embed capped at %d of %d KB file(s); "
                    "the freshness registry is fully healed — run `reconcile` to re-embed "
                    "the remainder",
                    max_files,
                    len(kb_ups),
                )
                kb_ups = kb_ups[:max_files]
        elif not cap and not policy.defer_expensive_indexes:
            max_files = policy.max_embed_files_per_batch
            if max_files is not None and len(kb_ups) > max_files:
                log.warning(
                    "file watcher: live import/sync burst has %d KB file(s), above "
                    "EXOMEM_WATCHER_MAX_EMBED_FILES=%d; lexical indexes updated but "
                    "semantic indexing deferred. Run `exomem index --scope vault` "
                    "after the import.",
                    len(kb_ups),
                    max_files,
                )
                defer_semantic = True
        elif policy.defer_expensive_indexes and kb_ups:
            log.info(
                "file watcher: quiet mode deferring semantic indexing for %d KB file(s)",
                len(kb_ups),
            )
        if kb_ups:
            try:
                index_sync.upsert_after_write(
                    self._vault_root, kb_ups, defer_semantic=defer_semantic
                )
            except Exception:  # noqa: BLE001
                log.exception("file watcher: upsert_after_write failed for %d file(s)", len(kb_ups))
        if kb_del_rels:
            try:
                index_sync.delete_after_remove(self._vault_root, kb_del_rels)
            except Exception:  # noqa: BLE001
                log.exception(
                    "file watcher: delete_after_remove failed for %d file(s)",
                    len(kb_del_rels),
                )

    def _run_reconcile(self) -> None:
        # Seed immediately (off the boot path — this is the watcher's own
        # daemon thread), then re-walk every RECONCILE_INTERVAL to bound drift.
        self._reconcile_once(seed=True)
        while not self._stop.wait(self._reconcile_interval_seconds()):
            self._reconcile_once(seed=False)

    # ---- lifecycle ----

    def start(self) -> bool:
        """Start watching. Returns False (no-op) when watchdog is unavailable.

        Soft-fail: a missing `watchdog` dep leaves the server fully functional — edits
        just won't be live-re-embedded until the next `reconcile`.
        """
        try:
            Observer, FileSystemEventHandler = _import_watchdog()
        except Exception as e:  # noqa: BLE001 — optional dep
            log.info(
                "file watcher: watchdog not available (%s); live re-embed disabled (no-op). "
                "Out-of-band edits re-embed on the next reconcile.",
                e,
            )
            return False
        if not self._vault_root.is_dir():
            log.info("file watcher: %s not found; not watching", self._vault_root)
            return False

        # Hosted quiesce/resume deliberately reuses the watcher instance. A
        # stopped threading.Event is sticky, so reset both loop controls before
        # recreating the dispatch/observer threads.
        self._stop.clear()
        self._wake.clear()

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
            return False
        if freshness.event_indexes_enabled():
            self._reconcile_thread = threading.Thread(
                target=self._run_reconcile, name="kb-freshness-reconcile", daemon=True
            )
            self._reconcile_thread.start()
        log.info("file watcher started on %s", self._vault_root)
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
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
