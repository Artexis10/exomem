"""Live file-watcher — re-embed out-of-band edits in ~1s instead of waiting for `reconcile`.

The vault is edited *around* the server — directly in Obsidian, on mobile, or via a
filesystem write (Obsidian Sync, a git pull). Those bypass the writer hooks, so the
embedding sidecar drifts until someone runs `reconcile`. This watcher closes that gap:
it watches `<vault>/Knowledge Base/` for `.md` changes and re-embeds them through the
SAME `embeddings.upsert_after_write` path the writers (and `reconcile`) use — deletes
go through `embeddings.delete_after_remove`.

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

from . import embeddings

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 0.5

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


def register_self_write(vault_root: Path, paths: Iterable[Path]) -> None:
    """Record server-authored markdown replacements so their watcher echo is
    dropped. Best-effort: unreadable/gone files are skipped (they simply won't
    be suppressed)."""
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


def register_self_delete(vault_root: Path, rel_paths: Iterable[str]) -> None:
    """Record server-authored markdown removals (delete/trash/move-away) so
    their watcher echo is dropped. TTL-bounded — there is no file left to
    signature-match."""
    root = _canon_root(vault_root)
    now = time.monotonic()
    with _SUPPRESS_LOCK:
        for rel in rel_paths:
            rel_posix = str(rel).replace("\\", "/")
            if not rel_posix.lower().endswith(".md"):
                continue
            _SELF_DELETES[(root, rel_posix)] = now + DELETE_SUPPRESS_TTL_SECONDS
        _prune_locked(now)


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
        """Dispatch the coalesced batch through the SAME paths the writers use."""
        ups, del_rels = self._drain()
        if ups:
            try:
                embeddings.upsert_after_write(self._vault_root, ups)
            except Exception:  # noqa: BLE001 — a bad batch must never kill the watcher
                log.exception("file watcher: upsert_after_write failed for %d file(s)", len(ups))
        if del_rels:
            try:
                embeddings.delete_after_remove(self._vault_root, del_rels)
            except Exception:  # noqa: BLE001
                log.exception("file watcher: delete_after_remove failed for %d file(s)", len(del_rels))

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
        if not self._kb_root.is_dir():
            log.info("file watcher: %s not found; not watching", self._kb_root)
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
            self._observer.schedule(_Handler(), str(self._kb_root), recursive=True)
            self._observer.start()
        except Exception as e:  # noqa: BLE001 — watcher must never break the server
            log.warning("file watcher: observer failed to start (%s); live re-embed disabled", e)
            self._stop.set()
            self._wake.set()
            return False
        log.info("file watcher started on %s", self._kb_root)
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
