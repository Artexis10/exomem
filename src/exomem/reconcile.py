"""reconcile: heal vault drift from out-of-band edits in one pass.

The writers (note/edit/link/...) keep three things current on every write: the
embedding sidecar, the index.md count rows, and log.md. But the vault is also
editable *around* the server — directly in Obsidian, on mobile, or by a manual
filesystem edit. Those bypass the writer hooks, so the sidecar and the index
counts drift silently (surfaced by audit's `embedding_drift` / `index_drift`).

`reconcile` is the first-class "I edited around the system, heal it" command:

1. **Index counts** — recompute the Sources/Notes/Entities count rows from
   on-disk reality (reusing `indexes.compute_subindex_writes`) and rewrite any
   that drifted. Hand-curated descriptions and Recent-activity are preserved —
   only count tokens move.
2. **Embeddings (incremental)** — re-embed the files `embedding_drift` flags:
   *stale* rows (on-disk mtime newer than the sidecar row) AND files with no
   sidecar row at all (never embedded — out-of-band creates in Obsidian /
   mobile / a filesystem write), via the same `upsert_after_write` path the
   writers use. Cheaper than a full `audit_fix(rebuild_embeddings=True)`
   wipe-and-rebuild.
3. **Drift report** — re-run `index_drift` + `embedding_drift` and return what
   remains.

Deliberately narrower than `audit_fix`: it does NOT canonicalize wikilinks or
backfill frontmatter (those are content rewrites you opt into, not reconcile).
Idempotent; `dry_run=True` reports without writing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import audit as audit_module
from . import indexes
from .vault import PlannedWrite, batch_atomic_write, kb_root

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    indexes_updated: list[str] = field(default_factory=list)
    embeddings_refreshed: int = 0
    embeddings_status: str = "current"  # "current" | "refreshed" | "disabled"
    graph_refreshed: int = 0
    graph_status: str = "current"  # "current" | "refreshed" | "disabled"
    remaining_drift: list[dict] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "indexes_updated": self.indexes_updated,
            "embeddings_refreshed": self.embeddings_refreshed,
            "embeddings_status": self.embeddings_status,
            "graph_refreshed": self.graph_refreshed,
            "graph_status": self.graph_status,
            "remaining_drift": self.remaining_drift,
            "dry_run": self.dry_run,
        }


def _rel(path: Path, vault_root: Path) -> str:
    try:
        return path.resolve().relative_to(vault_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _changed_writes(writes: list[PlannedWrite]) -> list[PlannedWrite]:
    """Keep only writes that actually change on-disk content (idempotency)."""
    out: list[PlannedWrite] = []
    for w in writes:
        try:
            current = w.path.read_text(encoding="utf-8") if w.path.exists() else None
        except OSError:
            current = None
        if current != w.content:
            out.append(w)
    return out


def reconcile(vault_root: Path, *, dry_run: bool = False) -> ReconcileReport:
    """Heal index-count + embedding drift from out-of-band edits.

    See the module docstring. Read-only when `dry_run=True`.
    """
    kb = kb_root(vault_root)
    report = ReconcileReport(dry_run=dry_run)

    # ---- 1. Index counts (recompute from disk; preserve curated text) ----
    top_index_path = kb / "index.md"
    top_text = (
        top_index_path.read_text(encoding="utf-8")
        if top_index_path.exists() else None
    )
    sub_writes, new_top = indexes.compute_subindex_writes(
        vault_root, top_index_text=top_text
    )
    writes: list[PlannedWrite] = _changed_writes(list(sub_writes))
    if new_top is not None and top_text is not None and new_top != top_text:
        writes.append(PlannedWrite(path=top_index_path, content=new_top))
    report.indexes_updated = [_rel(w.path, vault_root) for w in writes]
    if writes and not dry_run:
        batch_atomic_write(writes, vault_root=vault_root)

    # ---- 2. Embeddings (incremental refresh of stale + never-embedded files) ----
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        report.embeddings_status = "disabled"
    else:
        drift = audit_module._check_embedding_drift(vault_root)
        drifted_abs = [vault_root / f.path for f in drift]
        refresh_succeeded = True
        if drifted_abs and not dry_run:
            from . import embeddings, index_sync

            refresh_succeeded = embeddings.upsert_after_write(vault_root, drifted_abs) is not False
            if refresh_succeeded:
                index_sync.clear_deferred_work(vault_root, paths=drifted_abs)
        report.embeddings_refreshed = len(drifted_abs) if refresh_succeeded else 0
        if not refresh_succeeded:
            report.embeddings_status = "deferred"
        else:
            report.embeddings_status = "refreshed" if drifted_abs else "current"

    # ---- 2b. Lexical sidecar (count/mtime reconcile against the walk) ----
    # NOT behind the embeddings gate: the lexical index is a lean-install
    # artifact. The store's own sync check is the heal; forcing it here means
    # "reconcile" leaves the sidecar verified-fresh, not lazily healed later.
    if not dry_run:
        try:
            from . import lexstore
            lexstore.ensure_fresh(vault_root)
        except Exception:  # noqa: BLE001 — best-effort, lanes soft-fail anyway
            log.exception("lexical sidecar reconcile failed; next use self-heals")

    # ---- 2c. Derived epistemic graph sidecar ----
    if os.environ.get("EXOMEM_DISABLE_GRAPH_INDEX"):
        report.graph_status = "disabled"
    else:
        from . import epistemic_graph

        drift = epistemic_graph.graph_drift(vault_root)
        if drift and not dry_run:
            epistemic_graph.EpistemicGraphIndex(vault_root).rebuild_all()
        report.graph_refreshed = len(drift)
        report.graph_status = "refreshed" if drift else "current"

    # ---- 3. Remaining drift report ----
    post = audit_module.audit(
        vault_root, categories=["index_drift", "embedding_drift", "graph_drift"]
    )
    report.remaining_drift = [f.as_dict() for f in post.findings]

    # ---- 4. Invalidate the event-maintained registries ----
    # reconcile is the "I edited around the system, heal it" command — after it
    # runs, no in-memory freshness/inbound registry should keep trusting
    # pre-reconcile state. Freshness re-seeds on the watcher's next reconcile
    # tick; inbound rebuilds on next read. (The embedding matrix cache is
    # maintained separately by the shared-index memo and its own mtime check.)
    if not dry_run:
        from . import freshness
        from . import vault as vault_module

        freshness.invalidate(vault_root)
        vault_module.clear_inbound_index()

    return report
