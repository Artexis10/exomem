"""One post-write dispatch for every index sidecar a markdown change must reach.

Writers, the file watcher, and reconcile used to call
`embeddings.upsert_after_write` / `delete_after_remove` directly. Those entry
points are (correctly) gated by `EXOMEM_DISABLE_EMBEDDINGS` and the torch
import memo — gates the lexical sidecar must NOT sit behind, because the
bm25/keyword lanes it serves are lean-install lanes. This module is the shared
seam: each sidecar family applies its own policy, and a call site says
"markdown changed" exactly once.

Both callees are best-effort by contract (they log and swallow their own
failures at every layer below); call sites keep their existing try/except
wrappers as the outermost belt.
"""

from __future__ import annotations

from pathlib import Path


def upsert_after_write(vault_root: Path, written_paths: list[Path]) -> None:
    """Fan a writer's markdown change out to every index sidecar."""
    from . import embeddings, lexstore

    lexstore.upsert_after_write(vault_root, written_paths)
    embeddings.upsert_after_write(vault_root, written_paths)


def delete_after_remove(vault_root: Path, removed_rel_paths: list[str]) -> None:
    """Fan a removal out to every index sidecar."""
    from . import embeddings, lexstore

    lexstore.delete_after_remove(vault_root, removed_rel_paths)
    embeddings.delete_after_remove(vault_root, removed_rel_paths)
