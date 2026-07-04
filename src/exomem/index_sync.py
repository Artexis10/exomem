"""One post-write dispatch for every index a markdown change must reach.

Writers, the file watcher, and reconcile used to call
`embeddings.upsert_after_write` / `delete_after_remove` directly. Those entry
points are (correctly) gated by `EXOMEM_DISABLE_EMBEDDINGS` and the torch
import memo — gates the lexical sidecar must NOT sit behind, because the
bm25/keyword lanes it serves are lean-install lanes. This module is the shared
seam: each index family applies its own policy, and a call site says
"markdown changed" exactly once.

The in-memory wikilink resolver rides the same seam: writers now REUSE the
process-shared resolver (`find.shared_resolver`) instead of rebuilding it per
write, so this dispatch re-syncs the touched entries from disk and restamps
the cache's freshness key. Without the restamp, every write would invalidate
the cache (the vault freshness triple moves) and the next graph-lane query or
write would pay a full O(vault) rebuild — the watcher also patches, but
asynchronously, leaving a window this closes.

All callees are best-effort by contract (they log and swallow their own
failures at every layer below); call sites keep their existing try/except
wrappers as the outermost belt.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


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


def upsert_after_write(vault_root: Path, written_paths: list[Path]) -> None:
    """Fan a writer's markdown change out to every index sidecar.

    Paths under excluded scan dirs (`_trash/`, `_archive/`, `_Schema/`, …) are
    dropped first: every index's FULL rebuild skips them, so the incremental
    path must too (`vault.in_excluded_scan_dir`). The watcher filters its own
    events the same way; this belt covers direct writer calls.
    """
    from . import embeddings, find, lexstore
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
    embeddings.upsert_after_write(vault_root, eligible)
    try:
        rels = _rel_md_paths(vault_root, eligible)
        if rels:
            find.on_resolver_files_changed(vault_root, rels, [])
    except Exception:  # noqa: BLE001 — resolver sync must never fail a write
        log.debug("resolver re-sync after write failed", exc_info=True)


def delete_after_remove(vault_root: Path, removed_rel_paths: list[str]) -> None:
    """Fan a removal out to every index sidecar."""
    from . import embeddings, find, lexstore

    lexstore.delete_after_remove(vault_root, removed_rel_paths)
    embeddings.delete_after_remove(vault_root, removed_rel_paths)
    try:
        md_rels = [r for r in removed_rel_paths if r.lower().endswith(".md")]
        if md_rels:
            find.on_resolver_files_changed(vault_root, [], md_rels)
    except Exception:  # noqa: BLE001 — resolver sync must never fail a delete
        log.debug("resolver re-sync after delete failed", exc_info=True)
