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
from . import indexes, relation_review, semantic_writes
from .vault import PlannedWrite, batch_atomic_write, kb_root

log = logging.getLogger(__name__)

_LIFECYCLE_REPORT_LIMIT = 256


@dataclass
class ReconcileReport:
    indexes_updated: list[str] = field(default_factory=list)
    embeddings_refreshed: int = 0
    embeddings_status: str = "current"  # "current" | "refreshed" | "disabled"
    graph_refreshed: int = 0
    graph_status: str = "current"  # "current" | "refreshed" | "disabled"
    references_refreshed: int = 0
    references_status: str = "current"  # "current" | "refreshed"
    semantic_activation: str = "prospective"
    semantic_contract_findings: list[dict] = field(default_factory=list)
    semantic_contract_summary: dict[str, int] = field(default_factory=dict)
    semantic_contract_omitted_counts: dict[str, int] = field(default_factory=dict)
    semantic_contract_truncation: dict[str, int] = field(default_factory=dict)
    lifecycle_prepared: list[dict] = field(default_factory=list)
    lifecycle_prepared_summary: dict[str, int] = field(default_factory=dict)
    lifecycle_prepared_cleaned: list[str] = field(default_factory=list)
    lifecycle_prepared_cleanup_blocked: list[dict[str, str]] = field(
        default_factory=list
    )
    lifecycle_prepared_issues: list[dict[str, str]] = field(default_factory=list)
    lifecycle_prepared_omitted_count: int = 0
    remaining_drift: list[dict] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "indexes_updated": self.indexes_updated,
            "embeddings_refreshed": self.embeddings_refreshed,
            "embeddings_status": self.embeddings_status,
            "graph_refreshed": self.graph_refreshed,
            "graph_status": self.graph_status,
            "references_refreshed": self.references_refreshed,
            "references_status": self.references_status,
            "semantic_activation": self.semantic_activation,
            "semantic_contract_findings": self.semantic_contract_findings,
            "semantic_contract_summary": self.semantic_contract_summary,
            "semantic_contract_omitted_counts": self.semantic_contract_omitted_counts,
            "semantic_contract_truncation": self.semantic_contract_truncation,
            "lifecycle_prepared": self.lifecycle_prepared,
            "lifecycle_prepared_summary": self.lifecycle_prepared_summary,
            "lifecycle_prepared_cleaned": self.lifecycle_prepared_cleaned,
            "lifecycle_prepared_cleanup_blocked": (
                self.lifecycle_prepared_cleanup_blocked
            ),
            "lifecycle_prepared_issues": self.lifecycle_prepared_issues,
            "lifecycle_prepared_omitted_count": (
                self.lifecycle_prepared_omitted_count
            ),
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
    report = ReconcileReport(dry_run=dry_run)
    if not dry_run:
        from .activation_manifest import ensure_manifest

        ensure_manifest(vault_root)
    semantic_batch = semantic_writes.evaluate_posthoc_batch(
        vault_root,
        operation="reconcile",
    )
    semantic = semantic_batch.as_dict()
    report.semantic_activation = semantic["activation"]
    report.semantic_contract_findings = semantic["semantic_contract_findings"]
    report.semantic_contract_summary = semantic["semantic_contract_summary"]
    report.semantic_contract_omitted_counts = semantic["omitted_counts"]
    report.semantic_contract_truncation = semantic["truncation"]
    assert semantic_batch.corpus is not None
    lifecycle_batch = relation_review.inspect_lifecycle_prepared_slots(
        vault_root,
        corpus=semantic_batch.corpus,
    )
    lifecycle = lifecycle_batch.inspections
    report.lifecycle_prepared = [
        item.as_dict() for item in lifecycle[:_LIFECYCLE_REPORT_LIMIT]
    ]
    report.lifecycle_prepared_omitted_count = max(
        0, len(lifecycle) - len(report.lifecycle_prepared)
    )
    report.lifecycle_prepared_summary = {
        state: sum(item.state == state for item in lifecycle)
        for state in ("committed", "pending", "stale", "trashed_committed")
    }
    report.lifecycle_prepared_issues = [
        issue.as_dict()
        for issue in lifecycle_batch.issues[:_LIFECYCLE_REPORT_LIMIT]
    ]
    if not dry_run and lifecycle_batch.cleanup_safe:
        for item in lifecycle:
            if not item.cleanup_eligible:
                continue
            try:
                cleaned = relation_review.cleanup_stale_lifecycle_prepared(
                    vault_root, item
                )
            except relation_review.RelationReviewError as error:
                if len(report.lifecycle_prepared_cleanup_blocked) < _LIFECYCLE_REPORT_LIMIT:
                    report.lifecycle_prepared_cleanup_blocked.append(
                        {
                            "page_identity": item.prepared.page_identity,
                            "code": error.code,
                        }
                    )
                continue
            if len(report.lifecycle_prepared_cleaned) < _LIFECYCLE_REPORT_LIMIT:
                report.lifecycle_prepared_cleaned.append(cleaned)
    kb = kb_root(vault_root)

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

    # ---- 3. Stable-reference sidecar ----
    from . import memory_refs

    reference_drift = memory_refs.drift(vault_root)
    if reference_drift and not dry_run:
        memory_refs.ReferenceIndex(vault_root).rebuild_all()
    report.references_refreshed = len(reference_drift)
    report.references_status = "refreshed" if reference_drift else "current"

    # ---- 4. Remaining drift report ----
    post = audit_module.audit(
        vault_root,
        categories=["index_drift", "embedding_drift", "graph_drift", "reference_identity"],
    )
    report.remaining_drift = [f.as_dict() for f in post.findings]

    # ---- 5. Invalidate the event-maintained registries ----
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
