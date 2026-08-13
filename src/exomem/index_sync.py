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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import deferred_index, semantic_index

log = logging.getLogger(__name__)

_REPORT_PATH_LIMIT = 256
_REPORT_PATH_BYTE_LIMIT = 1024


def _withdraw_unbridgeable_corpus_consumers(vault_root: Path) -> None:
    """Fail closed when an exact filesystem delta could not be published.

    Incremental resolver, inbound, graph, and semantic-corpus projections may
    advance only from one retained event suffix.  Once publication fails, the
    suffix is unknowable: make the registry cold and evict the same-vault RAM
    consumers so their next read derives directly from human-owned files.
    Persisted graph readers also become unavailable because their old marker no
    longer matches the cold disk projection.
    """
    from . import find, freshness, semantic_contract, vault

    freshness.invalidate(vault_root)
    freshness.mark_external_pending(vault_root)
    semantic_contract.evict_corpus_context(vault_root)
    find.evict_resolver_caches(vault_root)
    vault.evict_inbound_index(vault_root)


def publish_corpus_delta(
    vault_root: Path,
    *,
    changed: tuple[Path, ...] | list[Path] = (),
    deleted: tuple[Path | str, ...] | list[Path | str] = (),
    attempts: int = 1,
) -> bool:
    """Publish one complete vault delta, retrying before any consumer fan-out.

    A caller may retry the *same complete batch* once for a transient failure.
    Persistent failure withdraws every checkpoint-consuming projection rather
    than allowing a path-local callback to bless stale global state.
    """
    from . import semantic_contract

    changed_values = tuple(Path(path) for path in changed)
    deleted_values = tuple(deleted)
    if not changed_values and not deleted_values:
        return True
    bounded_attempts = max(1, min(int(attempts), 2))
    for attempt in range(1, bounded_attempts + 1):
        try:
            semantic_contract.publish_corpus_files_changed(
                vault_root,
                changed=changed_values,
                deleted=deleted_values,
            )
        except Exception:  # noqa: BLE001 - canonical bytes already committed
            log.warning(
                "canonical corpus publication failed (attempt %d/%d)",
                attempt,
                bounded_attempts,
                exc_info=True,
            )
        else:
            return True
    _withdraw_unbridgeable_corpus_consumers(vault_root)
    return False


@dataclass(frozen=True, slots=True)
class IndexComponentOutcome:
    """One bounded component result from the existing index fan-out."""

    component: str
    outcome: str
    code: str

    def __post_init__(self) -> None:
        if type(self.component) is not str or self.component not in {
            "lexstore",
            "memory_refs",
            "resolver",
            "epistemic_graph",
            "embeddings",
            "watcher",
            "clip",
            "claims",
            "semantic_purge",
        }:
            raise ValueError("unsupported index component")
        if type(self.outcome) is not str or self.outcome not in {
            "accepted",
            "completed",
            "registered",
            "deferred",
            "failed",
            "not_required",
            "degraded",
        }:
            raise ValueError("unsupported index component outcome")
        if type(self.code) is not str or not self.code or len(self.code) > 64:
            raise ValueError("index component code must be bounded and nonempty")

    def as_dict(self) -> dict[str, str]:
        return {
            "component": self.component,
            "outcome": self.outcome,
            "code": self.code,
        }


@dataclass(frozen=True, slots=True)
class IndexSyncReport:
    """Sanitized observation of one post-write or post-remove fan-out."""

    operation: str
    requested_paths: tuple[str, ...]
    eligible_paths: tuple[str, ...]
    components: tuple[IndexComponentOutcome, ...]
    paths_truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_paths", tuple(self.requested_paths))
        object.__setattr__(self, "eligible_paths", tuple(self.eligible_paths))
        object.__setattr__(self, "components", tuple(self.components))
        if type(self.operation) is not str or self.operation not in {
            "upsert",
            "delete",
        }:
            raise ValueError("unsupported index sync operation")
        if (
            len(self.requested_paths) > _REPORT_PATH_LIMIT
            or len(self.eligible_paths) > _REPORT_PATH_LIMIT
        ):
            raise ValueError("index sync paths exceed report bound")
        if any(
            not _bounded_report_path(path) for path in (*self.requested_paths, *self.eligible_paths)
        ):
            raise ValueError("index sync report contains an unsafe path")
        if len(self.components) > 8:
            raise ValueError("index sync component count exceeds report bound")
        if len({item.component for item in self.components}) != len(self.components):
            raise ValueError("index sync report contains duplicate components")
        if type(self.paths_truncated) is not bool:
            raise ValueError("paths_truncated must be a boolean")

    @property
    def reconcile_required(self) -> bool:
        return any(item.outcome in {"degraded", "failed"} for item in self.components)

    @property
    def reconcile_guidance(self) -> str | None:
        if not self.reconcile_required:
            return None
        return "Run reconcile to repair observed derived-index degradation."

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "requested_paths": list(self.requested_paths),
            "eligible_paths": list(self.eligible_paths),
            "paths_truncated": self.paths_truncated,
            "components": [item.as_dict() for item in self.components],
            "reconcile_required": self.reconcile_required,
            "reconcile_guidance": self.reconcile_guidance,
        }


def _bounded_report_path(value: object) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        return (
            len(value.encode("utf-8")) <= _REPORT_PATH_BYTE_LIMIT
            and _safe_relative_path(value) is not None
        )
    except UnicodeEncodeError:
        return False


def _bounded_paths(paths: list[str]) -> tuple[tuple[str, ...], bool]:
    bounded = [path for path in paths if _bounded_report_path(path)]
    return (
        tuple(bounded[:_REPORT_PATH_LIMIT]),
        len(bounded) != len(paths) or len(bounded) > _REPORT_PATH_LIMIT,
    )


def with_component(report: IndexSyncReport, outcome: IndexComponentOutcome) -> IndexSyncReport:
    """Return one bounded report with an independently observed outer leaf."""
    components = tuple(
        item for item in report.components if item.component != outcome.component
    ) + (outcome,)
    return IndexSyncReport(
        report.operation,
        report.requested_paths,
        report.eligible_paths,
        components,
        report.paths_truncated,
    )


def failed_upsert_report(
    vault_root: Path,
    written_paths: list[Path],
    *,
    watcher: IndexComponentOutcome | None = None,
) -> IndexSyncReport:
    """Bound an outer upsert failure without claiming any leaf completed."""
    requested, truncated = _bounded_paths(_rel_md_paths(vault_root, written_paths))
    components = tuple(
        IndexComponentOutcome(component, "degraded", "dispatch_failed")
        for component in (
            "lexstore",
            "memory_refs",
            "resolver",
            "epistemic_graph",
            "embeddings",
        )
    )
    if watcher is not None:
        components += (watcher,)
    return IndexSyncReport("upsert", requested, requested, components, truncated)


def _stale_batch_report(
    vault_root: Path,
    identity_rels: list[str],
    requested: tuple[str, ...],
    eligible: tuple[str, ...],
    *,
    truncated: bool,
) -> IndexSyncReport:
    """Requeue a changed admission snapshot without publishing mixed state."""
    deferred_index.add_full(vault_root, identity_rels)
    components = tuple(
        IndexComponentOutcome(component, "degraded", "batch_stale")
        for component in (
            "lexstore",
            "memory_refs",
            "resolver",
            "epistemic_graph",
            "embeddings",
        )
    )
    return IndexSyncReport("upsert", requested, eligible, components, truncated)


def unverified_upsert_report(vault_root: Path, written_paths: list[Path]) -> IndexSyncReport:
    """Represent a legacy outer upsert that returned no observable status."""
    requested, truncated = _bounded_paths(_rel_md_paths(vault_root, written_paths))
    components = tuple(
        IndexComponentOutcome(component, "accepted", "accepted_unverified")
        for component in (
            "lexstore",
            "memory_refs",
            "resolver",
            "epistemic_graph",
            "embeddings",
        )
    )
    return IndexSyncReport("upsert", requested, requested, components, truncated)


def observed_delete_report(removed_paths: list[str], *, degraded: bool) -> IndexSyncReport:
    """Bound a legacy or failed outer delete without inventing completion."""
    requested, truncated = _bounded_paths(
        [path for path in removed_paths if _safe_relative_path(path) is not None]
    )
    outcome = "degraded" if degraded else "accepted"
    code = "dispatch_failed" if degraded else "accepted_unverified"
    components = tuple(
        IndexComponentOutcome(component, outcome, code)
        for component in (
            "lexstore",
            "memory_refs",
            "resolver",
            "epistemic_graph",
            "embeddings",
        )
    )
    return IndexSyncReport("delete", requested, requested, components, truncated)


def _safe_relative_path(value: str) -> str | None:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "\0" in normalized
        or (len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return None
    return path.as_posix()


def _legacy_component(component: str, callback) -> IndexComponentOutcome:
    """Observe only what a legacy leaf actually exposes."""
    try:
        result = callback()
    except Exception:  # noqa: BLE001 - one derived index must not stop the rest
        log.warning("%s index dispatch failed", component, exc_info=True)
        return IndexComponentOutcome(component, "degraded", "dispatch_failed")
    if result is None:
        return IndexComponentOutcome(component, "accepted", "accepted_unverified")
    if result is False:
        return IndexComponentOutcome(component, "degraded", "reported_incomplete")
    return IndexComponentOutcome(component, "completed", "dispatch_completed")


def _graph_component(callback) -> IndexComponentOutcome:
    """Preserve the graph's exact handoff outcome outside legacy best effort."""
    from .epistemic_graph import GraphDispatchResult

    try:
        result = callback()
    except Exception:  # noqa: BLE001 - defensive boundary for external callers
        log.warning("epistemic graph dispatch escaped", exc_info=True)
        return IndexComponentOutcome("epistemic_graph", "failed", "graph_dispatch_failed")
    if not isinstance(result, GraphDispatchResult):
        log.warning("epistemic graph dispatch returned no exact outcome")
        return IndexComponentOutcome("epistemic_graph", "failed", "graph_outcome_missing")
    return IndexComponentOutcome("epistemic_graph", result.outcome, result.code)


def _resolver_component(callback) -> IndexComponentOutcome:
    try:
        callback()
    except Exception:  # noqa: BLE001 - resolver sync must not stop the rest
        log.warning("resolver index dispatch failed", exc_info=True)
        return IndexComponentOutcome("resolver", "degraded", "dispatch_failed")
    return IndexComponentOutcome("resolver", "completed", "dispatch_completed")


def _embedding_component(status) -> IndexComponentOutcome:
    return _embedding_status_component("embeddings", status)


def _embedding_status_component(component: str, status) -> IndexComponentOutcome:
    if status.code == "no_eligible_paths":
        outcome = "accepted"
    elif status.status == "completed":
        outcome = "completed"
    elif status.status == "deferred":
        outcome = "deferred"
    elif status.status == "degraded":
        outcome = "degraded"
    else:
        outcome = "accepted"
    return IndexComponentOutcome(component, outcome, status.code)


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


def _record_deferred_semantic_upserts(
    vault_root: Path, paths: list[Path], *, omit_proven_current: bool = False
) -> tuple[int, int]:
    from . import index_paths

    rels = [
        rel
        for rel in _rel_md_paths(vault_root, paths)
        if index_paths.is_embeddable_path(vault_root / rel)
    ]
    if omit_proven_current and rels:
        freshness = deferred_index.inspect_embedding_freshness(vault_root, rels)
        rels = [
            rel
            for rel in rels
            if freshness.get(rel) is not deferred_index.EmbeddingFreshness.CURRENT
        ]
    return len(rels), deferred_index.add(vault_root, rels)


def purge_semantic_only(vault_root: Path, rel_paths: list[str]) -> bool:
    """Drop structured-only paths from semantic sidecars without touching identity.

    This is intentionally model-free: cleanup must work while model features are
    disabled, and a failure in one derivative must not keep the other semantic
    indexes serving a raw Record.  Memory references and the wikilink resolver
    are deliberately absent; they index stable page identity, not recall text.
    """
    rels = [
        rel
        for raw in rel_paths
        if (rel := _safe_relative_path(raw)) is not None and rel.lower().endswith(".md")
    ]
    if not rels:
        return True
    # Keep the first event spelling/order but avoid redundant sqlite writes.
    rels = list(dict.fromkeys(rels))
    from . import (
        claims,
        embedding_index,
        embeddings,
        epistemic_graph,
        index_paths,
        lexstore,
        recall_policy,
    )

    def _purge(component: str, callback) -> bool:
        try:
            return callback() is not False
        except Exception:  # noqa: BLE001 - every semantic sidecar heals independently
            log.warning("%s semantic purge failed", component, exc_info=True)
            return False

    succeeded = True
    if lexstore.lexical_path(vault_root).exists():
        succeeded &= _purge(
            "lexstore", lambda: lexstore.get_store(vault_root).delete_rel_paths(rels)
        )
    if index_paths.sidecar_path(vault_root).exists():
        succeeded &= _purge(
            "embeddings",
            lambda: embedding_index.EmbeddingIndex(vault_root).purge_paths_if_present(rels),
        )
    graph_rels = [
        rel for rel in rels if recall_policy.is_recall_candidate(vault_root, vault_root / rel)
    ]
    if graph_rels and epistemic_graph.sidecar_path(vault_root).exists():
        succeeded &= _purge(
            "epistemic_graph",
            lambda: epistemic_graph.EpistemicGraphIndex(vault_root).delete_paths(graph_rels),
        )
    if claims.sidecar_path(vault_root).exists():
        succeeded &= _purge(
            "claims",
            lambda: claims.delete_after_remove(vault_root, rels),
        )
    if index_paths.clip_sidecar_path(vault_root).exists():
        succeeded &= _purge(
            "clip",
            lambda: embeddings.get_clip_index(vault_root).purge_markdown_paths_if_present(rels),
        )
    succeeded &= _purge(
        "deferred_semantic", lambda: deferred_index.clear_semantic_receipts(vault_root, rels)
    )
    return bool(succeeded)


def replay_deferred_embedding(
    vault_root: Path,
    paths: list[Path],
    receipts: list[deferred_index.DeferredReceipt]
    | tuple[deferred_index.DeferredReceipt, ...]
    | None = None,
):
    """Replay one durable embedding batch and clear only its completed revisions."""
    from . import embeddings

    if receipts is None:
        rels = set(_rel_md_paths(vault_root, paths))
        receipts = [
            receipt for receipt in deferred_index.snapshot(vault_root) if receipt.rel_path in rels
        ]
    status = embeddings.upsert_after_write_status(vault_root, paths, defer_during_warm=False)
    if status.status == "completed":
        deferred_index.clear_receipts(vault_root, list(receipts))
    return status


def deferred_work_status(vault_root: Path | None = None) -> dict:
    """No-allocation summary of durable expensive index work."""
    return {
        "semantic_upserts": deferred_index.status(vault_root),
        "full_upserts": deferred_index.full_status(vault_root),
    }


def record_failed_refresh(vault_root: Path, paths: list[Path]) -> int:
    """Persist a failed all-index dispatch without importing model modules."""
    return deferred_index.add_full(vault_root, _rel_md_paths(vault_root, paths))


def _publication_failed_report(
    operation: str,
    requested_paths: tuple[str, ...],
    eligible_paths: tuple[str, ...],
    *,
    paths_truncated: bool,
) -> IndexSyncReport:
    """Report a failed canonical publication without advancing any consumer."""
    components = tuple(
        IndexComponentOutcome(component, "failed", "publication_failed")
        for component in (
            ("lexstore", "memory_refs", "resolver", "epistemic_graph", "embeddings")
            if operation == "upsert"
            else (
                "lexstore",
                "memory_refs",
                "epistemic_graph",
                "clip",
                "embeddings",
                "claims",
                "semantic_purge",
                "resolver",
            )
        )
    )
    return IndexSyncReport(
        operation,
        requested_paths,
        eligible_paths,
        components,
        paths_truncated,
    )


def _register_publication_failure_graph_handle(vault_root: Path) -> None:
    """Keep a committed graph epoch joinable when corpus publication failed."""
    from . import graph_sync

    required = graph_sync.read_checkpoint(vault_root)
    if required is None:
        return
    try:
        graph_sync.register_failure(vault_root, required, code="CORPUS_PUBLICATION_FAILED")
    except Exception:  # noqa: BLE001 - the failed publication report remains authoritative
        log.warning("graph publication-failure handoff registration failed", exc_info=True)


def clear_deferred_work(
    vault_root: Path | None = None,
    *,
    paths: list[Path] | list[str] | None = None,
    include_full: bool = False,
) -> int:
    """Clear embedding work, preserving full-index retries unless explicitly requested."""
    if vault_root is None:
        return 0
    if paths is None:
        cleared = deferred_index.clear(vault_root)
        if include_full:
            cleared += deferred_index.clear_full(vault_root)
        return cleared
    rels: list[str] = []
    for item in paths:
        if isinstance(item, Path):
            rels.extend(_rel_md_paths(vault_root, [item]))
        else:
            rel = str(item).replace("\\", "/")
            if rel.lower().endswith(".md"):
                rels.append(rel)
    cleared = deferred_index.clear(vault_root, rels)
    if include_full:
        cleared += deferred_index.clear_full(vault_root, rels)
    return cleared


def drain_deferred_work(
    vault_root: Path,
    *,
    limit: int | None = None,
    paths: list[Path] | list[str] | None = None,
) -> int:
    """Process queued semantic upserts now and clear them on dispatch.

    The embedding layer is best-effort and logs/soft-fails internally, matching
    the normal writer path. Crash/restart recovery still comes from drift audit
    and explicit reconcile/index.
    """
    requested: set[str] | None = None
    if paths is not None:
        requested = set()
        for item in paths:
            if isinstance(item, Path):
                requested.update(_rel_md_paths(vault_root, [item]))
            else:
                requested.add(str(item).replace("\\", "/"))
    full_receipts = deferred_index.snapshot_full(vault_root)
    semantic_receipts = deferred_index.snapshot(vault_root)
    if requested is not None:
        full_receipts = [receipt for receipt in full_receipts if receipt.rel_path in requested]
        # `paths=` is the media worker's targeted full-refresh seam. Semantic
        # replay is owned by the periodic unscoped drain and must not add
        # unrelated work to a media completion callback.
        semantic_receipts = []
    if limit is not None:
        budget = max(0, limit)
        if full_receipts and semantic_receipts and budget > 1:
            full_budget = budget // 2
            semantic_budget = budget - full_budget
        elif full_receipts:
            full_budget, semantic_budget = budget, 0
        else:
            full_budget, semantic_budget = 0, budget
        full_receipts = full_receipts[:full_budget]
        semantic_receipts = semantic_receipts[:semantic_budget]

    processed = 0
    if full_receipts:
        full_paths = [vault_root / receipt.rel_path for receipt in full_receipts]
        full_batch_completed = False
        try:
            dispatched = upsert_after_write(vault_root, full_paths)
        except Exception:  # noqa: BLE001 - isolate failures below
            log.warning("deferred full-index batch failed; isolating receipts", exc_info=True)
        else:
            full_batch_completed = dispatched is not False and not (
                isinstance(dispatched, IndexSyncReport) and dispatched.reconcile_required
            )
        if full_batch_completed:
            processed += deferred_index.clear_full_receipts(vault_root, full_receipts)
        else:
            log.warning("deferred full-index batch incomplete; isolating receipts")
        for receipt in () if full_batch_completed else full_receipts:
            try:
                dispatched = upsert_after_write(
                    vault_root, [vault_root / receipt.rel_path]
                )
            except Exception:  # noqa: BLE001 - durable work must survive a failed dispatch
                log.warning(
                    "deferred full-index dispatch failed; work remains queued",
                    exc_info=True,
                )
                deferred_index.rotate_receipts(vault_root, [receipt], full=True)
                continue
            if dispatched is False or (
                isinstance(dispatched, IndexSyncReport) and dispatched.reconcile_required
            ):
                log.warning("deferred full-index dispatch incomplete; work remains queued")
                deferred_index.rotate_receipts(vault_root, [receipt], full=True)
                continue
            processed += deferred_index.clear_full_receipts(vault_root, [receipt])

    if limit is None and requested is None and full_receipts:
        # A full refresh performed under a deferring mode can create or revise
        # semantic receipts. The unbounded operator drain must reconcile that
        # post-full snapshot too; bounded reconcile passes leave it for the
        # next pass so one file cannot consume the shared cap twice.
        semantic_receipts = deferred_index.snapshot(vault_root)
    if not semantic_receipts:
        return processed
    freshness = deferred_index.inspect_embedding_freshness(
        vault_root,
        [receipt.rel_path for receipt in semantic_receipts],
        mtime_slack_seconds=1.0,
    )
    current = [
        receipt
        for receipt in semantic_receipts
        if freshness.get(receipt.rel_path) is deferred_index.EmbeddingFreshness.CURRENT
    ]
    processed += deferred_index.clear_receipts(vault_root, current)
    semantic_receipts = [
        receipt for receipt in semantic_receipts if receipt not in current
    ]
    if not semantic_receipts:
        return processed
    semantic_paths = [vault_root / receipt.rel_path for receipt in semantic_receipts]
    try:
        status = replay_deferred_embedding(vault_root, semantic_paths, semantic_receipts)
    except Exception:  # noqa: BLE001 - durable work must survive a failed dispatch
        log.warning("deferred semantic dispatch failed; work remains queued", exc_info=True)
    else:
        if status.status == "completed":
            return processed + len(semantic_receipts)
        log.warning("deferred semantic dispatch incomplete; work remains queued")

    # A single malformed source must not pin every later receipt in the sorted
    # batch. The optimistic batch above is the fast path; only a failed batch
    # falls back to per-receipt replay so successful work can retire by CAS.
    for receipt in semantic_receipts:
        try:
            status = replay_deferred_embedding(
                vault_root, [vault_root / receipt.rel_path], [receipt]
            )
        except Exception:  # noqa: BLE001 - isolate poison receipts
            log.warning(
                "deferred semantic receipt dispatch failed; work remains queued",
                exc_info=True,
            )
            deferred_index.rotate_receipts(vault_root, [receipt])
            continue
        if status.status == "completed":
            processed += 1
        else:
            log.warning("deferred semantic receipt incomplete; work remains queued")
            deferred_index.rotate_receipts(vault_root, [receipt])
    return processed


def _dispatch_upsert_components(
    vault_root: Path,
    identity_paths: list[Path],
    semantic_paths: list[Path],
    suppressed_rels: list[str],
    *,
    defer_semantic: bool,
    created_semantic_paths: list[Path],
) -> list[IndexComponentOutcome]:
    from . import epistemic_graph, find, lexstore, memory_refs, mode

    components = [
        _legacy_component(
            "memory_refs",
            lambda: memory_refs.upsert_after_write(vault_root, identity_paths),
        ),
    ]
    rels = _rel_md_paths(vault_root, identity_paths)
    components.append(
        _resolver_component(
            lambda: find.on_resolver_files_changed(vault_root, rels, []) if rels else None
        )
    )
    # Publish all raw-Record removals before any semantic insertion/defer. The
    # identity fan-out above remains intentionally broad and has no recall body
    # egress; a purge failure is isolated and cannot stop other sidecars.
    purge_succeeded = purge_semantic_only(vault_root, suppressed_rels)
    components.append(
        IndexComponentOutcome(
            "semantic_purge",
            "completed" if purge_succeeded else "degraded",
            "purge_completed" if purge_succeeded else "purge_failed",
        )
    )
    components.append(
        _legacy_component(
            "lexstore", lambda: lexstore.upsert_after_write(vault_root, semantic_paths)
        )
    )
    def graph_upsert():
        if created_semantic_paths:
            return epistemic_graph.upsert_after_write(
                vault_root,
                semantic_paths,
                created_paths=created_semantic_paths,
            )
        return epistemic_graph.upsert_after_write(vault_root, semantic_paths)

    components.append(
        _graph_component(graph_upsert)
        if semantic_paths
        else IndexComponentOutcome("epistemic_graph", "not_required", "no_graph_input")
    )
    if defer_semantic or mode.defer_expensive_indexes():
        try:
            semantic_count, added = _record_deferred_semantic_upserts(
                vault_root,
                semantic_paths,
                omit_proven_current=defer_semantic,
            )
        except Exception:  # noqa: BLE001 - degradation is reported, other lanes landed
            log.warning("durable semantic defer failed", exc_info=True)
            components.append(
                IndexComponentOutcome("embeddings", "degraded", "durable_defer_failed")
            )
        else:
            if added:
                log.info("deferred semantic indexing for %d markdown file(s)", added)
            if semantic_count:
                components.append(
                    IndexComponentOutcome("embeddings", "deferred", "deferred_durable")
                )
            else:
                components.append(
                    IndexComponentOutcome("embeddings", "accepted", "no_eligible_paths")
                )
    else:
        from . import embeddings

        try:
            status = embeddings.upsert_after_write_status(vault_root, semantic_paths)
            component = _embedding_component(status)
        except Exception:  # noqa: BLE001 - derived index must not fail a writer
            log.warning("embeddings index dispatch failed", exc_info=True)
            components.append(IndexComponentOutcome("embeddings", "degraded", "dispatch_failed"))
        else:
            components.append(component)
            if status.status != "completed" and status.code != "deferred_warmup":
                try:
                    _record_deferred_semantic_upserts(vault_root, semantic_paths)
                except Exception:  # noqa: BLE001 - report remains the primary outcome
                    log.warning("durable semantic retry recording failed", exc_info=True)
    return components


def upsert_after_write(
    vault_root: Path,
    written_paths: list[Path],
    *,
    defer_semantic: bool = False,
    semantic_states: Mapping[str, semantic_index.SemanticParentIndexState] | None = None,
    publish_corpus_change: bool = True,
    created_paths: Iterable[Path] = (),
) -> IndexSyncReport:
    """Fan a writer's markdown change out to every index sidecar.

    Paths under excluded scan dirs (`_trash/`, `_archive/`, `_Schema/`, ...) are
    dropped first: every index's FULL rebuild skips them, so the incremental
    path must too (`vault.in_excluded_scan_dir`). The watcher filters its own
    events the same way; this belt covers direct writer calls.
    """
    from .vault import in_excluded_scan_dir

    vr = vault_root.resolve()

    def _rel(p: Path) -> str | None:
        try:
            return p.resolve().relative_to(vr).as_posix()
        except (OSError, ValueError):
            return None

    requested_rels: list[str] = []
    eligible: list[Path] = []
    eligible_rels: list[str] = []
    for p in written_paths:
        rel = _rel(p)
        if rel is None:
            continue
        requested_rels.append(rel)
        if in_excluded_scan_dir(rel):
            continue
        eligible.append(p)
        eligible_rels.append(rel)
    requested_report, requested_truncated = _bounded_paths(requested_rels)
    eligible_report, eligible_truncated = _bounded_paths(eligible_rels)
    if not eligible:
        return IndexSyncReport(
            "upsert",
            requested_report,
            eligible_report,
            (),
            requested_truncated or eligible_truncated,
        )
    from . import recall_policy

    batch = recall_policy.partition_markdown_paths(vault_root, eligible)
    identity_paths = [item.path for item in batch.identity_paths]
    semantic_paths = [item.path for item in batch.admitted_paths]
    semantic_rels = [item.rel_path for item in batch.admitted_paths]
    suppressed_rels = [item.rel_path for item in batch.suppressed_paths]
    created_rels = {
        rel
        for path in created_paths
        if (rel := _rel(Path(path))) is not None
    }
    created_semantic_paths = [
        item.path for item in batch.admitted_paths if item.rel_path in created_rels
    ]
    if not identity_paths:
        # Preserve the observable no-semantic-path fan-out contract for legacy
        # non-Markdown notifications while keeping them out of every identity
        # and semantic collection.
        components = _dispatch_upsert_components(
            vault_root,
            [],
            [],
            [],
            defer_semantic=defer_semantic,
            created_semantic_paths=[],
        )
        return IndexSyncReport(
            "upsert",
            requested_report,
            eligible_report,
            tuple(components),
            requested_truncated or eligible_truncated,
        )
    if not batch.revalidate(vault_root):
        # A changed policy or source cannot be safely split across the identity
        # and semantic fan-outs. Preserve a generic retry; the next replay takes
        # a fresh snapshot rather than treating the path as suppression.
        return _stale_batch_report(
            vault_root,
            [item.rel_path for item in batch.identity_paths],
            requested_report,
            eligible_report,
            truncated=requested_truncated or eligible_truncated,
        )
    # Canonical writers publish their exact committed targets before any
    # incremental cache fan-out. This gives consumers a bridgeable freshness
    # delta instead of asking a path-local resolver patch to bless a global
    # disk identity it cannot prove. The wrapper keeps the warm semantic
    # corpus's freshness token in the same publication boundary; a watcher may
    # later publish the identical event harmlessly.
    publication_current = not publish_corpus_change or publish_corpus_delta(
        vault_root,
        changed=identity_paths,
    )
    if not publication_current:
        _register_publication_failure_graph_handle(vault_root)
        try:
            record_failed_refresh(vault_root, identity_paths)
        except Exception:  # noqa: BLE001 - the failed publication report remains authoritative
            log.warning("durable full-index retry recording failed", exc_info=True)
        return _publication_failed_report(
            "upsert",
            requested_report,
            eligible_report,
            paths_truncated=requested_truncated or eligible_truncated,
        )
    admitted_rels = {item.rel_path for item in batch.admitted_paths}
    states = {rel: state for rel, state in (semantic_states or {}).items() if rel in admitted_rels}
    for path, rel in zip(semantic_paths, semantic_rels, strict=True):
        if rel in states:
            continue
        active = semantic_index.parent_state_for_path(vault_root, path)
        if active is not None:
            states[rel] = active
            continue
        try:
            states[rel] = semantic_index.build_parent_index_state(vault_root, path)
        except (OSError, UnicodeError, ValueError):
            continue
    if not batch.revalidate(vault_root):
        return _stale_batch_report(
            vault_root,
            [item.rel_path for item in batch.identity_paths],
            requested_report,
            eligible_report,
            truncated=requested_truncated or eligible_truncated,
        )
    token = semantic_index.set_parent_states(states)
    try:
        components = _dispatch_upsert_components(
            vault_root,
            identity_paths,
            semantic_paths,
            suppressed_rels,
            defer_semantic=defer_semantic,
            created_semantic_paths=created_semantic_paths,
        )
    finally:
        semantic_index.reset_parent_states(token)
    if not batch.revalidate(vault_root):
        return _stale_batch_report(
            vault_root,
            [item.rel_path for item in batch.identity_paths],
            requested_report,
            eligible_report,
            truncated=requested_truncated or eligible_truncated,
        )
    report = IndexSyncReport(
        "upsert",
        requested_report,
        eligible_report,
        tuple(components),
        requested_truncated or eligible_truncated,
    )
    return report


def delete_after_remove(
    vault_root: Path,
    removed_rel_paths: list[str],
    *,
    publish_corpus_change: bool = True,
) -> IndexSyncReport:
    """Fan a removal out to every index sidecar."""
    from . import (
        claims,
        embeddings,
        epistemic_graph,
        find,
        lexstore,
        media_types,
        memory_refs,
        scene_frames,
    )

    safe_paths = [
        normalized
        for item in removed_rel_paths
        if (normalized := _safe_relative_path(str(item))) is not None
    ]
    requested_report, paths_truncated = _bounded_paths(safe_paths)
    if not safe_paths:
        return IndexSyncReport("delete", requested_report, requested_report, ())
    md_rels = [rel for rel in safe_paths if rel.lower().endswith(".md")]
    publication_current = not (md_rels and publish_corpus_change) or publish_corpus_delta(
        vault_root,
        deleted=[vault_root / rel for rel in md_rels],
    )
    if not publication_current:
        _register_publication_failure_graph_handle(vault_root)
        return _publication_failed_report(
            "delete",
            requested_report,
            requested_report,
            paths_truncated=paths_truncated,
        )
    components = [
        _legacy_component("lexstore", lambda: lexstore.delete_after_remove(vault_root, safe_paths)),
        _legacy_component(
            "memory_refs",
            lambda: memory_refs.delete_after_remove(vault_root, safe_paths),
        ),
        _graph_component(
            # The exact deletion delta is already published above. Refreshing
            # through that checkpoint removes the vanished file and repairs
            # every affected source edge before marking the graph current.
            lambda: (
                epistemic_graph.upsert_after_write(
                    vault_root, [vault_root / rel for rel in md_rels]
                )
                if md_rels
                else epistemic_graph.delete_after_remove(vault_root, safe_paths)
            ),
        ),
        _embedding_status_component(
            "clip", embeddings.delete_clip_after_remove(vault_root, safe_paths)
        ),
    ]
    try:
        status = embeddings.delete_after_remove_status(vault_root, safe_paths)
        component = _embedding_component(status)
    except Exception:  # noqa: BLE001 - derived index must not stop resolver cleanup
        log.warning("embeddings index delete failed", exc_info=True)
        components.append(IndexComponentOutcome("embeddings", "degraded", "dispatch_failed"))
    else:
        components.append(component)
    components.append(
        _legacy_component("claims", lambda: claims.delete_after_remove(vault_root, md_rels))
    )
    components.append(
        _legacy_component(
            "semantic_purge",
            lambda: deferred_index.clear_semantic_receipts(vault_root, md_rels),
        )
    )
    components.append(
        _resolver_component(
            lambda: find.on_resolver_files_changed(vault_root, [], md_rels) if md_rels else None
        )
    )
    # A removed video also drops its scene-frame derivatives: clear_scene_frames
    # deletes the `<video>.frames/` jpg+sidecar pairs from disk and purges their
    # own lexical/embedding rows via its own delete_after_remove call (recursive,
    # idempotent). No-op (guarded inside clear_scene_frames) when the video never
    # had persisted frames.
    for rel in safe_paths:
        if media_types.media_type_for(rel) != "video":
            continue
        try:
            scene_frames.clear_scene_frames(vault_root, vault_root / rel)
        except Exception:  # noqa: BLE001 - frame cleanup is best-effort
            log.warning("scene-frame cleanup failed for %s", rel, exc_info=True)
    report = IndexSyncReport(
        "delete",
        requested_report,
        requested_report,
        tuple(components),
        paths_truncated,
    )
    return report
