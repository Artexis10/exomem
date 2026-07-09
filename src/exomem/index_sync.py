"""One post-write dispatch for every index a markdown change must reach.

Writers, the file watcher, and reconcile used to call
`embeddings.upsert_after_write` / `delete_after_remove` directly. Those entry
points are (correctly) gated by `EXOMEM_DISABLE_EMBEDDINGS` and the torch
import memo -- gates the lexical sidecar must NOT sit behind, because the
bm25/keyword lanes it serves are lean-install lanes. This module is the shared
seam: each index family applies its own policy, and a call site says
"markdown changed" exactly once.

The in-memory wikilink resolver rides the same seam: writers now REUSE the
process-shared resolver (`find.shared_resolver`) instead of rebuilding it per
write, so this dispatch re-syncs the touched entries from disk and restamps
the cache's freshness key. Without the restamp, every write would invalidate
the cache (the vault freshness triple moves) and the next graph-lane query or
write would pay a full O(vault) rebuild -- the watcher also patches, but
asynchronously, leaving a window this closes.

All callees are best-effort by contract (they log and swallow their own
failures at every layer below); call sites keep their existing try/except
wrappers as the outermost belt.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_DEFERRED_LOCK = threading.Lock()
_DEFERRED_SEMANTIC_UPSERTS: dict[str, set[str]] = {}


def _root_key(vault_root: Path) -> str:
    try:
        return str(vault_root.resolve())
    except OSError:
        return str(vault_root)


def _rel_md_paths(vault_root: Path, paths: list[Path]) -> list[str]:
    """Vault-relative POSIX .md paths for `paths` (non-md / outside-vault skipped)."""
    out: list[str] = []
    vr = vault_root.resolve()
    for p in paths:
        try:
            rel = p.resolve().relative_to(vr).as_posix()
        except (OSError, ValueError):
            continue
        if rel.lower().endswith(".md"):
            out.append(rel)
    return out


def _record_deferred_semantic_upserts(vault_root: Path, paths: list[Path]) -> int:
    rels = _rel_md_paths(vault_root, paths)
    if not rels:
        return 0
    with _DEFERRED_LOCK:
        pending = _DEFERRED_SEMANTIC_UPSERTS.setdefault(_root_key(vault_root), set())
        before = len(pending)
        pending.update(rels)
        return len(pending) - before


def deferred_work_status(vault_root: Path | None = None) -> dict:
    """No-allocation summary of expensive index work queued by quiet mode."""
    with _DEFERRED_LOCK:
        if vault_root is not None:
            root = _root_key(vault_root)
            roots = {root: set(_DEFERRED_SEMANTIC_UPSERTS.get(root, set()))}
        else:
            roots = {root: set(rels) for root, rels in _DEFERRED_SEMANTIC_UPSERTS.items()}
    count = sum(len(rels) for rels in roots.values())
    paths = sorted({rel for rels in roots.values() for rel in rels})
    return {
        "semantic_upserts": {
            "count": count,
            "paths": paths[:50],
            "truncated": len(paths) > 50,
            "roots": len([rels for rels in roots.values() if rels]),
        }
    }


def clear_deferred_work(
    vault_root: Path | None = None,
    *,
    paths: list[Path] | list[str] | None = None,
) -> int:
    """Clear deferred semantic work after an explicit index/reconcile heal."""
    with _DEFERRED_LOCK:
        if vault_root is None:
            count = sum(len(rels) for rels in _DEFERRED_SEMANTIC_UPSERTS.values())
            _DEFERRED_SEMANTIC_UPSERTS.clear()
            return count
        root = _root_key(vault_root)
        pending = _DEFERRED_SEMANTIC_UPSERTS.get(root)
        if not pending:
            return 0
        if paths is None:
            count = len(pending)
            _DEFERRED_SEMANTIC_UPSERTS.pop(root, None)
            return count
        rels: set[str] = set()
        for item in paths:
            if isinstance(item, Path):
                rels.update(_rel_md_paths(vault_root, [item]))
            else:
                rel = str(item).replace("\\", "/")
                if rel.lower().endswith(".md"):
                    rels.add(rel)
        before = len(pending)
        pending.difference_update(rels)
        if not pending:
            _DEFERRED_SEMANTIC_UPSERTS.pop(root, None)
        return before - len(pending)


def drain_deferred_work(vault_root: Path, *, limit: int | None = None) -> int:
    """Process queued semantic upserts now and clear them on dispatch.

    The embedding layer is best-effort and logs/soft-fails internally, matching
    the normal writer path. Crash/restart recovery still comes from drift audit
    and explicit reconcile/index.
    """
    root = _root_key(vault_root)
    with _DEFERRED_LOCK:
        pending = sorted(_DEFERRED_SEMANTIC_UPSERTS.get(root, set()))
        if limit is not None:
            pending = pending[:max(0, limit)]
    if not pending:
        return 0
    from . import embeddings

    paths = [vault_root / rel for rel in pending]
    embeddings.upsert_after_write(vault_root, paths)
    return clear_deferred_work(vault_root, paths=paths)


def upsert_after_write(
    vault_root: Path, written_paths: list[Path], *, defer_semantic: bool = False
) -> None:
    """Fan a writer's markdown change out to every index sidecar.

    Paths under excluded scan dirs (`_trash/`, `_archive/`, `_Schema/`, ...) are
    dropped first: every index's FULL rebuild skips them, so the incremental
    path must too (`vault.in_excluded_scan_dir`). The watcher filters its own
    events the same way; this belt covers direct writer calls.
    """
    from . import epistemic_graph, find, lexstore, mode
    from .vault import in_excluded_scan_dir

    vr = vault_root.resolve()

    def _rel(p: Path) -> str | None:
        try:
            return p.resolve().relative_to(vr).as_posix()
        except (OSError, ValueError):
            return None

    eligible: list[Path] = []
    for p in written_paths:
        rel = _rel(p)
        if rel is not None and in_excluded_scan_dir(rel):
            continue
        eligible.append(p)
    if not eligible:
        return
    lexstore.upsert_after_write(vault_root, eligible)
    try:
        rels = _rel_md_paths(vault_root, eligible)
        if rels:
            find.on_resolver_files_changed(vault_root, rels, [])
    except Exception:  # noqa: BLE001 -- resolver sync must never fail a write
        log.debug("resolver re-sync after write failed", exc_info=True)
    epistemic_graph.upsert_after_write(vault_root, eligible)
    if defer_semantic or mode.defer_expensive_indexes():
        added = _record_deferred_semantic_upserts(vault_root, eligible)
        if added:
            log.info("deferred semantic indexing for %d markdown file(s)", added)
        return
    from . import embeddings

    embeddings.upsert_after_write(vault_root, eligible)


def delete_after_remove(vault_root: Path, removed_rel_paths: list[str]) -> None:
    """Fan a removal out to every index sidecar."""
    from . import embeddings, epistemic_graph, find, lexstore

    lexstore.delete_after_remove(vault_root, removed_rel_paths)
    epistemic_graph.delete_after_remove(vault_root, removed_rel_paths)
    embeddings.delete_after_remove(vault_root, removed_rel_paths)
    try:
        md_rels = [r for r in removed_rel_paths if r.lower().endswith(".md")]
        if md_rels:
            find.on_resolver_files_changed(vault_root, [], md_rels)
    except Exception:  # noqa: BLE001 -- resolver sync must never fail a delete
        log.debug("resolver re-sync after delete failed", exc_info=True)
