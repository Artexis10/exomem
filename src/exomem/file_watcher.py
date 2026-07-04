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

import logging
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from . import freshness, index_sync

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 0.5
# How often the watcher re-walks and reconciles the freshness registry against
# disk truth, bounding how long a dropped watchdog event can leave it stale.
RECONCILE_INTERVAL_SECONDS = 300.0

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

    def __init__(self, vault_root: Path, *, debounce_seconds: float = DEBOUNCE_SECONDS) -> None:
        self._vault_root = vault_root
        self._kb_root = vault_root / "Knowledge Base"
        self._debounce = debounce_seconds
        self._lock = threading.Lock()
        self._pending_upsert: set[Path] = set()
        self._pending_delete: set[Path] = set()
        self._last_change = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer = None
        self._reconcile_thread: threading.Thread | None = None

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
        """Record a `.md` change. Coalesces rapid events for the same path."""
        if path.suffix.lower() != ".md":
            return  # only markdown is embedded; ignore attachments / sidecars-of-binaries churn
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

    def _drain(self) -> tuple[list[Path], list[str]]:
        with self._lock:
            ups = sorted(self._pending_upsert)
            dels = sorted(self._pending_delete)
            self._pending_upsert.clear()
            self._pending_delete.clear()
        del_rels = [r for r in (self._rel(p) for p in dels) if r]
        return ups, del_rels

    def _flush(self) -> None:
        """Dispatch the coalesced batch: publish freshness/inbound for every
        changed path (vault-wide), and re-embed only the Knowledge Base subset."""
        ups, del_rels = self._drain()

        # Freshness + inbound: the whole vault, since both index sibling folders too.
        if ups or del_rels:
            deleted_paths = [self._vault_root / r for r in del_rels]
            try:
                freshness.on_files_changed(self._vault_root, changed=ups, deleted=deleted_paths)
            except Exception:  # noqa: BLE001 — a bad batch must never kill the watcher
                log.exception("file watcher: freshness publish failed")
            up_rels = [r for r in (self._rel(p) for p in ups) if r]
            try:
                from . import vault as vault_module

                vault_module.on_inbound_files_changed(
                    self._vault_root, up_rels, del_rels
                )
            except Exception:  # noqa: BLE001
                log.exception("file watcher: inbound publish failed")
            try:
                from . import find as find_module

                find_module.on_resolver_files_changed(
                    self._vault_root, up_rels, del_rels
                )
            except Exception:  # noqa: BLE001
                log.exception("file watcher: resolver publish failed")

        # Index sidecars (embedding + lexical): Knowledge Base markdown only.
        kb_ups = [p for p in ups if self._is_kb(p)]
        kb_del_rels = [r for r in del_rels if r.startswith("Knowledge Base/")]
        if kb_ups:
            try:
                index_sync.upsert_after_write(self._vault_root, kb_ups)
            except Exception:  # noqa: BLE001
                log.exception("file watcher: upsert_after_write failed for %d file(s)", len(kb_ups))
        if kb_del_rels:
            try:
                index_sync.delete_after_remove(self._vault_root, kb_del_rels)
            except Exception:  # noqa: BLE001
                log.exception("file watcher: delete_after_remove failed for %d file(s)", len(kb_del_rels))

    # ---- debounce loop ----

    def _run_dispatch(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.is_set():
                break
            # Wait for a quiet window so a burst of saves (or a git pull) coalesces
            # into one batch instead of one upsert per FS event.
            while not self._stop.is_set():
                time.sleep(self._debounce)
                with self._lock:
                    quiet = (time.monotonic() - self._last_change) >= self._debounce
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
                yield (str(p), p.stat().st_mtime_ns)
            except OSError:
                continue

    def _reconcile_once(self, *, seed: bool) -> None:
        """Re-derive the freshness maps from a fresh walk. `seed=True` on the
        first pass installs the maps and marks the scopes live; later passes
        heal any drift from a missed watchdog event."""
        for scope in freshness.SCOPES:
            try:
                if seed:
                    freshness.seed(self._vault_root, scope, self._walk_entries(scope))
                else:
                    freshness.reconcile(self._vault_root, scope, self._walk_entries(scope))
            except Exception:  # noqa: BLE001 — reconcile must never kill the watcher
                log.exception("file watcher: freshness reconcile failed (scope=%s)", scope)

    def _run_reconcile(self) -> None:
        # Seed immediately (off the boot path — this is the watcher's own
        # daemon thread), then re-walk every RECONCILE_INTERVAL to bound drift.
        self._reconcile_once(seed=True)
        while not self._stop.wait(RECONCILE_INTERVAL_SECONDS):
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
