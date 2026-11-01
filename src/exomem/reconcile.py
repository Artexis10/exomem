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

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audit as audit_module
from . import indexes, maintenance_jobs, originals, relation_review, semantic_index, semantic_writes
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
    semantic_unit_parents_refreshed: int = 0
    semantic_unit_orphans_removed: int = 0
    clip_orphans_removed: int = 0
    frame_orphans_removed: int = 0
    part_orphans_removed: int = 0
    parts_rebuilt: int = 0
    parts_missing: list[str] = field(default_factory=list)
    semantic_suppressed_drift: list[dict[str, str]] = field(default_factory=list)
    semantic_suppressed_purged: list[str] = field(default_factory=list)
    semantic_suppressed_corrupt: list[dict[str, str]] = field(default_factory=list)
    semantic_suppressed_corrupt_purged: dict[str, int] = field(default_factory=dict)
    semantic_suppressed_truncation: dict[str, int] = field(default_factory=dict)
    semantic_missing_drift: list[str] = field(default_factory=list)
    semantic_missing_purged: list[str] = field(default_factory=list)
    semantic_suppressed_incomplete: dict[str, str] = field(default_factory=dict)
    semantic_unit_indexes_status: str = "current"
    semantic_unit_index_drift: list[dict] = field(default_factory=list)
    semantic_unit_index_remaining: list[dict] = field(default_factory=list)
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
    lifecycle_prepared_omitted_counts: dict[str, int] = field(
        default_factory=dict
    )
    remaining_drift: list[dict] = field(default_factory=list)
    receipt_reconcile: dict = field(default_factory=dict)
    derived_batch_reconcile: dict = field(default_factory=dict)
    dry_run: bool = False
    #: The identity of what this run would remove or quarantine; set by a preview and by a job.
    plan_id: str | None = None
    graph_rebuild_requested: bool = False
    graph_rebuild_applicable: bool = False
    graph_rebuild_status: str = "not_requested"
    graph_quarantine_id: str | None = None
    graph_rebuild_warning: str | None = None
    _graph_reconcile_registered: int | None = field(default=None, repr=False)
    _graph_rebuild_handoff: dict | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        result = {
            "indexes_updated": self.indexes_updated,
            "embeddings_refreshed": self.embeddings_refreshed,
            "embeddings_status": self.embeddings_status,
            "graph_refreshed": self.graph_refreshed,
            "graph_status": self.graph_status,
            "references_refreshed": self.references_refreshed,
            "references_status": self.references_status,
            "semantic_unit_parents_refreshed": self.semantic_unit_parents_refreshed,
            "semantic_unit_orphans_removed": self.semantic_unit_orphans_removed,
            "clip_orphans_removed": self.clip_orphans_removed,
            "frame_orphans_removed": self.frame_orphans_removed,
            "part_orphans_removed": self.part_orphans_removed,
            "parts_rebuilt": self.parts_rebuilt,
            "parts_missing": self.parts_missing,
            "semantic_suppressed_drift": self.semantic_suppressed_drift,
            "semantic_suppressed_purged": self.semantic_suppressed_purged,
            "semantic_suppressed_corrupt": self.semantic_suppressed_corrupt,
            "semantic_suppressed_corrupt_purged": self.semantic_suppressed_corrupt_purged,
            "semantic_suppressed_truncation": self.semantic_suppressed_truncation,
            "semantic_missing_drift": self.semantic_missing_drift,
            "semantic_missing_purged": self.semantic_missing_purged,
            "semantic_suppressed_incomplete": self.semantic_suppressed_incomplete,
            "semantic_unit_indexes_status": self.semantic_unit_indexes_status,
            "semantic_unit_index_drift": self.semantic_unit_index_drift,
            "semantic_unit_index_remaining": self.semantic_unit_index_remaining,
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
            "lifecycle_prepared_omitted_counts": (
                self.lifecycle_prepared_omitted_counts
            ),
            "remaining_drift": self.remaining_drift,
            "receipt_reconcile": self.receipt_reconcile,
            "derived_batch_reconcile": self.derived_batch_reconcile,
            "dry_run": self.dry_run,
            "graph_rebuild_requested": self.graph_rebuild_requested,
            "graph_rebuild_applicable": self.graph_rebuild_applicable,
            "graph_rebuild_status": self.graph_rebuild_status,
            "graph_quarantine_id": self.graph_quarantine_id,
            "graph_rebuild_warning": self.graph_rebuild_warning,
        }
        if self.plan_id is not None:
            result["plan_id"] = self.plan_id
        registered = self._graph_reconcile_registered
        if registered is not None:
            result["_graph_reconcile_registered"] = registered
        if self._graph_rebuild_handoff is not None:
            result["_graph_rebuild_handoff"] = self._graph_rebuild_handoff
        return result


def _graph_rebuild_is_current(vault_root: Path) -> bool:
    from . import epistemic_graph

    return (
        epistemic_graph.graph_sync.status(vault_root)["state"] == "current"
        and epistemic_graph.EpistemicGraphIndex(vault_root).available()
        and not audit_module._check_graph_drift(vault_root)
    )


def _is_graph_rebuild_handoff(dispatch: object, checkpoint: object) -> bool:
    return (
        getattr(dispatch, "outcome", None) in {"registered", "completed"}
        and getattr(dispatch, "checkpoint", None) == checkpoint
    )


def finalize_graph_rebuild_handoff(
    vault_root: Path, result: Mapping, *, state_root: Path | None = None
) -> dict:
    """Join one registered reset after its outer mutation boundary has exited."""
    from . import graph_sync

    final = dict(result)
    handoff = final.pop("_graph_rebuild_handoff", None)
    if not isinstance(handoff, Mapping):
        return final
    operation_id = handoff.get("operation_id")
    raw_checkpoint = handoff.get("checkpoint")
    if not (
        isinstance(operation_id, str)
        and len(operation_id) == 24
        and all(character in "0123456789abcdef" for character in operation_id)
        and isinstance(raw_checkpoint, Mapping)
    ):
        final["graph_rebuild_status"] = "retained"
        final["graph_rebuild_warning"] = "GRAPH_REBUILD_HANDOFF_INVALID"
        return final
    checkpoint = graph_sync.GraphSyncCheckpoint.parse(
        json.dumps(raw_checkpoint, sort_keys=True, separators=(",", ":"))
    )
    if checkpoint is None:
        final["graph_rebuild_status"] = "retained"
        final["graph_rebuild_warning"] = "GRAPH_REBUILD_HANDOFF_INVALID"
        return final
    try:
        graph_sync.wait_for_registered(vault_root, state_root=state_root)
    except Exception:  # noqa: BLE001 - canonical reconcile already completed
        final["graph_rebuild_status"] = "retained"
        final["graph_rebuild_warning"] = "GRAPH_REBUILD_RETAINED"
        return final
    if not _graph_rebuild_is_current(vault_root):
        final["graph_rebuild_status"] = "retained"
        final["graph_rebuild_warning"] = "GRAPH_REBUILD_RETAINED"
        return final
    final["graph_status"] = "refreshed"
    final["graph_refreshed"] = handoff.get("graph_refreshed", 0)
    if graph_sync.cleanup_published_graph_lineage_reset(
        vault_root, operation_id, checkpoint
    ):
        final["graph_rebuild_status"] = "cleared"
        final["graph_rebuild_warning"] = None
    else:
        final["graph_rebuild_status"] = "retained"
        final["graph_rebuild_warning"] = "GRAPH_REBUILD_RETAINED"
    return final


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


def _bounded_lifecycle_values(values):
    retained = list(values[:_LIFECYCLE_REPORT_LIMIT])
    return retained, max(0, len(values) - len(retained))


def _iter_frame_dirs(kb: Path):
    """Yield every `<file>.frames/` directory under `kb`, pruning cruft dirs.

    Frame dirs are leaves (scene_frames never nests one inside another), so
    a matched dir is not descended into further.
    """
    from . import scene_frames
    from .vault import VAULT_SCAN_SKIP_DIRS

    stack = [kb]
    while stack:
        d = stack.pop()
        try:
            children = list(d.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            if child.name in VAULT_SCAN_SKIP_DIRS:
                continue
            if child.name.endswith(scene_frames.FRAMES_DIR_SUFFIX):
                yield child
            else:
                stack.append(child)


def _dangling_frame_dirs(vault_root: Path) -> list[tuple[Path, Path]]:
    """`[(frame_dir, video_path)]` for every frame dir whose parent video is gone."""
    from . import scene_frames

    kb = kb_root(vault_root)
    out: list[tuple[Path, Path]] = []
    for frame_dir in _iter_frame_dirs(kb):
        video_path = frame_dir.with_name(
            frame_dir.name[: -len(scene_frames.FRAMES_DIR_SUFFIX)]
        )
        if not (video_path.exists() or originals.evicted(vault_root, video_path)):
            out.append((frame_dir, video_path))
    return out


def _isolation_census(vault_root: Path) -> audit_module.SemanticRecallIsolationCensus:
    from . import deferred_index

    return audit_module.semantic_recall_isolation_census(
        vault_root, after=deferred_index.semantic_isolation_cursors(vault_root)
    )


def _isolation_plan(
    vault_root: Path, census: audit_module.SemanticRecallIsolationCensus
) -> tuple[list[str], list[str], set[str]]:
    """`(suppressed_paths, missing_paths, missing_clip_orphans)` of one semantic-isolation census."""
    suppressed_paths = sorted({item["path"] for item in census.safe_dicts()})
    missing_paths = sorted({row.path for row in census.missing_rows if row.path})
    # The semantic-isolation census validates CLIP keys through their Markdown
    # sidecar identity. Retain only keys whose binary is also absent before the
    # generic missing-path cleanup removes their CLIP rows. A live binary
    # with a missing sidecar still needs semantic cleanup, but is not an orphan.
    missing_clip_orphans = {
        row.path.removesuffix(".md")
        for row in census.missing_rows
        if row.component == "clip"
        and row.path is not None
        and not ((vault_root / row.path.removesuffix(".md")).exists()
                 or originals.evicted(vault_root, row.path.removesuffix(".md")))
    }
    return suppressed_paths, missing_paths, missing_clip_orphans


def _clip_orphans(vault_root: Path, missing_clip_orphans: set[str]) -> list[str]:
    """CLIP keys with no binary on disk: the census's own and every stored row that lost its file."""
    from . import embeddings as embeddings_module

    if not embeddings_module.clip_enabled():
        return []
    clip_paths, _frame_ts, _matrix = embeddings_module.get_clip_index(vault_root).all_vectors()
    return sorted(
        missing_clip_orphans
        | {p for p in clip_paths if not ((vault_root / p).exists() or originals.evicted(vault_root, p))}
    )


def _cleanup_eligible(batch: relation_review.LifecyclePreparedBatch) -> tuple[relation_review.LifecyclePreparedInspection, ...]:
    return tuple(item for item in batch.inspections if item.cleanup_eligible)


def _graph_quarantine_members(vault_root: Path) -> tuple[str, ...] | None:
    """The members a graph-lineage quarantine would move aside; `None` when the lineage is not unavailable."""
    from . import epistemic_graph

    if epistemic_graph.graph_sync.classify_epoch(vault_root).kind != "unavailable":
        return None
    return tuple(
        str(member) for member in epistemic_graph.graph_sync.census_unavailable_graph_lineage(vault_root)
    )


@dataclass(frozen=True)
class _Census:
    """What `reconcile` would remove or quarantine, read before it writes anything.

    The inputs the run reads again are handed on, so one pass reads the vault once.
    """

    plan_id: str
    isolation: audit_module.SemanticRecallIsolationCensus
    posthoc_inputs: dict[str, Any]


def _plan_census(vault_root: Path, rebuild_graph: bool) -> _Census:
    """Read every removal and quarantine a run would make, and name them as one `plan_id`.

    Index counts, embeddings, lexical rows, unit sidecars and reference rows are not in the plan.
    They converge toward what is on disk, so a repeat only does less of them. Only what deletes
    state, or moves it aside, is named, so a page edit between preview and start does not stale it.
    The graph recovery of an earlier crashed reset is a repair that cannot be read first, so it stays out.
    """
    from . import lifecycle_statuses

    status_basis = lifecycle_statuses.Basis(vault_root)
    corpus, language = semantic_writes.build_posthoc_corpus(vault_root, status_basis=status_basis)
    isolation = _isolation_census(vault_root)
    suppressed_paths, missing_paths, missing_clip_orphans = _isolation_plan(vault_root, isolation)
    lifecycle = relation_review.inspect_lifecycle_prepared_slots(vault_root, corpus=corpus)
    quarantine = (
        _graph_quarantine_members(vault_root)
        if rebuild_graph and not os.environ.get("EXOMEM_DISABLE_GRAPH_INDEX")
        else None
    )
    entries: list[list[str]] = [["suppressed", path] for path in suppressed_paths]
    entries += [["missing", path] for path in missing_paths]
    entries += [
        [
            "corrupt",
            row.component,
            row.edge_column or "",
            hashlib.sha256(row.raw.encode("utf-8", "replace")).hexdigest(),
            repr((row.dependency_source, row.dependency_lookup_key, row.dependency_raw_target)),
        ]
        for row in isolation.corrupt_rows
    ]
    entries += [["clip", key] for key in _clip_orphans(vault_root, missing_clip_orphans)]
    entries += [["frames", _rel(frame_dir, vault_root)] for frame_dir, _video in _dangling_frame_dirs(vault_root)]
    if lifecycle.cleanup_safe:
        entries += [["lifecycle", item.prepared.page_identity, item.prepared.reference] for item in _cleanup_eligible(lifecycle)]
    entries += [["graph-quarantine", member] for member in quarantine or ()]
    return _Census(
        plan_id=maintenance_jobs.plan_identity("reconcile", {"rebuild_graph": bool(rebuild_graph)}, entries),
        isolation=isolation,
        posthoc_inputs={"corpus": corpus, "language_registry": language, "status_basis": status_basis},
    )


def reconcile(
    vault_root: Path, *, dry_run: bool = False, rebuild_graph: bool = False, plan_id: str | None = None
) -> ReconcileReport:
    """Heal index-count + embedding drift from out-of-band edits.

    See the module docstring. Read-only when `dry_run=True`. A preview reports the `plan_id` of what
    the run would remove or quarantine. A write that names a `plan_id` is a reviewed one, run by a
    maintenance job: it reads that plan again before its first write and writes nothing if it changed.
    A job holds the mutation boundary for the whole run, as the command does, and a stop lands
    between phases: the phases before it keep their effect, and a repeat converges the rest.
    """
    report = ReconcileReport(dry_run=dry_run, graph_rebuild_requested=rebuild_graph)
    census: _Census | None = None
    if dry_run or plan_id is not None:
        # Read before any write, `ensure_manifest` included: a preview names the plan, and a job
        # refuses a changed one with nothing written. A plain write reads nothing extra.
        maintenance_jobs.checkpoint("planning")
        census = _plan_census(vault_root, rebuild_graph)
        report.plan_id = census.plan_id
        if plan_id is not None:
            maintenance_jobs.require_current_plan(
                "reconcile", plan_id, census.plan_id, "what reconcile would remove or quarantine"
            )
    if not dry_run:
        from .activation_manifest import ensure_manifest

        ensure_manifest(vault_root)
    semantic_batch = semantic_writes.evaluate_posthoc_batch(
        vault_root,
        operation="reconcile",
        **({} if census is None else census.posthoc_inputs),
    )
    semantic = semantic_batch.as_dict()
    report.semantic_activation = semantic["activation"]
    report.semantic_contract_findings = semantic["semantic_contract_findings"]
    report.semantic_contract_summary = semantic["semantic_contract_summary"]
    report.semantic_contract_omitted_counts = semantic["omitted_counts"]
    report.semantic_contract_truncation = semantic["truncation"]
    assert semantic_batch.corpus is not None
    semantic_states = {
        path: semantic_index.from_semantic_page_state(state)
        for path, state in semantic_batch.corpus.pages.items()
    }
    from . import deferred_index, epistemic_graph, index_paths, lexstore

    maintenance_jobs.checkpoint("purging")
    # A live raw Record is present, not missing.  Purge only the semantic
    # derivatives that should never have survived a policy transition; generic
    # delete routing would incorrectly remove its stable identity/reference rows.
    suppression_census = census.isolation if census is not None else _isolation_census(vault_root)
    report.semantic_suppressed_drift = suppression_census.safe_dicts()
    report.semantic_suppressed_corrupt = suppression_census.corrupt_dicts()
    report.semantic_suppressed_truncation = suppression_census.truncation
    report.semantic_suppressed_incomplete = suppression_census.incomplete
    suppressed_paths, missing_paths, missing_clip_orphans = _isolation_plan(vault_root, suppression_census)
    report.semantic_missing_drift = missing_paths
    repair_succeeded = not suppression_census.incomplete
    if suppressed_paths and not dry_run:
        from . import index_sync

        if index_sync.purge_semantic_only(vault_root, suppressed_paths):
            report.semantic_suppressed_purged = suppressed_paths
        else:
            repair_succeeded = False
    if suppression_census.corrupt_rows and not dry_run:
        report.semantic_suppressed_corrupt_purged = (
            audit_module.purge_corrupt_semantic_recall_isolation_rows(
                vault_root, suppression_census.corrupt_rows
            )
        )
        expected_components = {
            "graph"
            if row.component
            in {"graph_edges", "graph_dependencies", "graph_dependency_coverage"}
            else row.component
            for row in suppression_census.corrupt_rows
        }
        if not expected_components <= set(report.semantic_suppressed_corrupt_purged):
            repair_succeeded = False
    if missing_paths and not dry_run:
        from . import embeddings, index_sync

        # These are lexically validated, absent Markdown identities: ordinary
        # removal routing is correct and intentionally clears stable identity
        # sidecars too. CLIP stores the corresponding media key without `.md`.
        missing_report = index_sync.delete_after_remove(vault_root, missing_paths)
        embeddings.get_clip_index(vault_root).purge_markdown_paths_if_present(missing_paths)
        report.semantic_missing_purged = missing_paths
        repair_succeeded &= not missing_report.reconcile_required
    if not dry_run and repair_succeeded:
        deferred_index.set_semantic_isolation_cursors(
            vault_root,
            suppression_census.continuation if suppression_census.truncation else {},
        )

    maintenance_jobs.checkpoint("units")
    include_unit_lexical = lexstore.lexical_path(vault_root).exists()
    include_unit_vectors = bool(
        not os.environ.get("EXOMEM_DISABLE_EMBEDDINGS")
        and index_paths.sidecar_path(vault_root).exists()
    )
    graph_sidecar_exists = epistemic_graph.sidecar_path(vault_root).exists()
    initial_graph_drift = (
        []
        if suppressed_paths and not graph_sidecar_exists
        else audit_module._check_graph_drift(vault_root)
    )
    include_unit_graph = bool(
        epistemic_graph.graph_enabled()
        and epistemic_graph.sidecar_path(vault_root).exists()
        and not initial_graph_drift
    )
    unit_drift = semantic_index.audit_semantic_unit_sidecars(
        vault_root,
        semantic_states,
        include_lexical=include_unit_lexical,
        include_vectors=include_unit_vectors,
        include_graph=include_unit_graph,
    )
    report.semantic_unit_index_drift = [item.as_dict() for item in unit_drift]
    if unit_drift:
        report.semantic_unit_indexes_status = "drifted" if dry_run else "repairing"
    if unit_drift and not dry_run:
        from . import index_sync

        affected = sorted(
            {
                item.parent_path
                for item in unit_drift
                if item.parent_path in semantic_states
            }
        )
        orphans = sorted(
            {
                item.parent_path
                for item in unit_drift
                if item.parent_path not in semantic_states
                and item.sidecar != "cross_sidecar"
            }
        )
        if orphans:
            index_sync.delete_after_remove(vault_root, orphans)
        if affected:
            index_sync.upsert_after_write(
                vault_root,
                [vault_root / path for path in affected],
                semantic_states={path: semantic_states[path] for path in affected},
            )
        report.semantic_unit_parents_refreshed = len(affected)
        report.semantic_unit_orphans_removed = len(orphans)
    maintenance_jobs.checkpoint("lifecycle")
    lifecycle_batch = relation_review.inspect_lifecycle_prepared_slots(
        vault_root,
        corpus=semantic_batch.corpus,
    )
    lifecycle = lifecycle_batch.inspections
    report.lifecycle_prepared, prepared_omitted = _bounded_lifecycle_values(
        tuple(item.as_dict() for item in lifecycle)
    )
    report.lifecycle_prepared_omitted_count = prepared_omitted
    report.lifecycle_prepared_omitted_counts = {
        "lifecycle_prepared": prepared_omitted,
        "lifecycle_prepared_issues": 0,
        "lifecycle_prepared_cleaned": 0,
        "lifecycle_prepared_cleanup_blocked": 0,
    }
    report.lifecycle_prepared_summary = {
        state: sum(item.state == state for item in lifecycle)
        for state in ("committed", "pending", "stale", "trashed_committed")
    }
    (
        report.lifecycle_prepared_issues,
        report.lifecycle_prepared_omitted_counts["lifecycle_prepared_issues"],
    ) = _bounded_lifecycle_values(
        tuple(issue.as_dict() for issue in lifecycle_batch.issues)
    )
    if not dry_run and lifecycle_batch.cleanup_safe:
        cleanup = relation_review.cleanup_stale_lifecycle_prepared_batch(
            vault_root,
            _cleanup_eligible(lifecycle_batch),
        )
        (
            report.lifecycle_prepared_cleaned,
            report.lifecycle_prepared_omitted_counts[
                "lifecycle_prepared_cleaned"
            ],
        ) = _bounded_lifecycle_values(cleanup.cleaned)
        (
            report.lifecycle_prepared_cleanup_blocked,
            report.lifecycle_prepared_omitted_counts[
                "lifecycle_prepared_cleanup_blocked"
            ],
        ) = _bounded_lifecycle_values(
            tuple(issue.as_dict() for issue in cleanup.blocked)
        )
    kb = kb_root(vault_root)

    # ---- 1. Index counts (recompute from disk; preserve curated text) ----
    maintenance_jobs.checkpoint("indexes")
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
    maintenance_jobs.checkpoint("embeddings")
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        report.embeddings_status = "disabled"
    else:
        drift = audit_module._check_embedding_drift(vault_root)
        drifted_abs: list[Path] = []
        missing_rels: list[str] = []
        for finding in drift:
            rel = audit_module._safe_persisted_markdown_rel(str(finding.path))
            if rel is None:
                continue
            candidate = audit_module._no_follow_regular_markdown_path(vault_root, rel)
            if candidate is not None:
                drifted_abs.append(candidate)
            else:
                missing_rels.append(rel)
        refresh_succeeded = True
        if not dry_run:
            from . import embeddings, index_sync, mode, semantic_drain

            if missing_rels:
                index_sync.delete_after_remove(vault_root, missing_rels)
            if drifted_abs:
                if mode.service_profile_enabled():
                    semantic_drain.request(vault_root, drifted_abs, edited=False)
                    refresh_succeeded = False
                else:
                    refresh_succeeded = (
                        embeddings.upsert_after_write(vault_root, drifted_abs) is not False
                    )
                if refresh_succeeded:
                    index_sync.clear_deferred_work(vault_root, paths=drifted_abs)
        report.embeddings_refreshed = len(drifted_abs) if refresh_succeeded else 0
        if not refresh_succeeded:
            report.embeddings_status = "deferred"
        else:
            report.embeddings_status = "refreshed" if drifted_abs else "current"

    # ---- 2b. Lexical sidecar (count/mtime reconcile against the walk) ----
    maintenance_jobs.checkpoint("lexical")
    # NOT behind the embeddings gate: the lexical index is a lean-install
    # artifact. The store's own sync check is the heal; forcing it here means
    # "reconcile" leaves the sidecar verified-fresh, not lazily healed later.
    if not dry_run and (not suppressed_paths or lexstore.lexical_path(vault_root).exists()):
        try:
            lexstore.ensure_fresh(vault_root)
        except Exception:  # noqa: BLE001 — best-effort, lanes soft-fail anyway
            log.exception("lexical sidecar reconcile failed; next use self-heals")

    # ---- 2c. Derived epistemic graph sidecar ----
    maintenance_jobs.checkpoint("graph")
    if os.environ.get("EXOMEM_DISABLE_GRAPH_INDEX"):
        report.graph_status = "disabled"
        if rebuild_graph:
            report.graph_rebuild_status = "not_applicable"
    else:
        index = epistemic_graph.EpistemicGraphIndex(vault_root)
        reset_registered = False
        if rebuild_graph:
            # A process can crash after durable isolation but before the new
            # checkpoint is written.  That leaves the live root looking
            # legacy/current, so explicit recovery must adopt this first.
            reset = (
                epistemic_graph.graph_sync.recover_isolated_graph_lineage_reset(vault_root)
                if not dry_run
                else None
            )
            if reset is not None:
                report.graph_rebuild_applicable = True
                report.graph_rebuild_status = "quarantined"
                report.graph_quarantine_id = reset.operation_id
            else:
                members = _graph_quarantine_members(vault_root)
                if members is not None:
                    report.graph_rebuild_applicable = bool(members)
                    if dry_run:
                        report.graph_rebuild_status = "would_quarantine"
                    else:
                        reset = epistemic_graph.graph_sync.isolate_unavailable_graph_lineage(
                            vault_root
                        )
                        if reset is not None:
                            report.graph_rebuild_status = "quarantined"
                            report.graph_quarantine_id = reset.operation_id
                else:
                    report.graph_rebuild_status = "not_applicable"
            if reset is not None and not dry_run:
                try:
                    checkpoint = epistemic_graph.graph_sync.reconcile_checkpoint(
                        vault_root
                    )
                    dispatch = epistemic_graph._registered_or_failure(
                        vault_root,
                        checkpoint,
                        index,
                        index._canonical_mutation_coordinator(),
                    )
                except Exception:  # noqa: BLE001 - canonical reset isolation remains durable
                    report.graph_rebuild_status = "retained"
                    report.graph_rebuild_warning = "GRAPH_REBUILD_REGISTRATION_FAILED"
                else:
                    if _is_graph_rebuild_handoff(dispatch, checkpoint):
                        report._graph_reconcile_registered = 0
                        report._graph_rebuild_handoff = {
                            "operation_id": reset.operation_id,
                            "checkpoint": checkpoint.as_dict(),
                            "graph_refreshed": len(initial_graph_drift),
                        }
                        reset_registered = True
                    else:
                        report.graph_rebuild_status = "retained"
                        report.graph_rebuild_warning = dispatch.code
        initial_epoch = epistemic_graph.graph_sync.status(vault_root)
        needs_repair = (
            bool(initial_graph_drift)
            or initial_epoch["state"] != "current"
            or not index.available()
        )
        if not dry_run:
            epistemic_graph.graph_sync.sweep_abandoned_temporaries(
                vault_root,
                epistemic_graph.sidecar_path(vault_root),
                live_paths=epistemic_graph.graph_sync.live_temporary_paths(),
                state_root=index._canonical_mutation_coordinator().state_root,
            )
            if (
                not reset_registered
                and epistemic_graph.graph_sync.classify_epoch(vault_root).kind
                == "recoverable"
            ):
                epistemic_graph.graph_sync.recover_checkpoint(vault_root)
            if (
                needs_repair
                and not reset_registered
                and epistemic_graph.graph_sync.status(vault_root)["state"] != "unavailable"
            ):
                if audit_module._check_graph_drift(vault_root) or not index.available():
                    checkpoint = epistemic_graph.graph_sync.reconcile_checkpoint(
                        vault_root
                    )
                    dispatch = epistemic_graph._registered_or_failure(
                        vault_root,
                        checkpoint,
                        index,
                        index._canonical_mutation_coordinator(),
                    )
                    from .writer_lease import active_mutation_request_id

                    if active_mutation_request_id() is None:
                        epistemic_graph.graph_sync.wait_for_registered(
                            vault_root,
                            state_root=index._canonical_mutation_coordinator().state_root,
                        )
                    elif dispatch.outcome == "registered":
                        report._graph_reconcile_registered = len(initial_graph_drift)
        current = (
            epistemic_graph.graph_sync.status(vault_root)["state"] == "current"
            and index.available()
            and not audit_module._check_graph_drift(vault_root)
        )
        report.graph_refreshed = len(initial_graph_drift) if current and needs_repair else 0
        if current:
            report.graph_status = "refreshed" if needs_repair else "current"
        else:
            report.graph_status = "unavailable"
    if report._graph_reconcile_registered is not None or report._graph_rebuild_handoff is not None:
        # A registered graph flight keeps running after the job ends or stops, so the receipt says
        # the graph is not done yet, whatever `graph_status` read at this instant.
        maintenance_jobs.note(graph="pending")

    # ---- 2d. CLIP + scene-frame orphan healing ----
    # Heals vaults that already lost content through the pre-fix gap (a media
    # deletion that predated CLIP/scene-frame fan-out): stale CLIP rows for
    # paths no longer on disk, and `.frames/` directories whose parent video
    # is gone. Idempotent — re-running finds nothing left once healed.
    # Detection always runs (dry_run reports the true counts, same pattern as
    # the graph/reference drift above); only the repair is gated by dry_run.
    from . import embeddings as embeddings_module
    from . import scene_frames

    maintenance_jobs.checkpoint("orphans")
    clip_orphans = _clip_orphans(vault_root, missing_clip_orphans)
    if clip_orphans and not dry_run:
        embeddings_module.delete_clip_after_remove(vault_root, clip_orphans)
    report.clip_orphans_removed = len(clip_orphans)

    frame_orphans = _dangling_frame_dirs(vault_root)
    if not dry_run:
        for frame_dir, video_path in frame_orphans:
            scene_frames.clear_scene_frames(vault_root, video_path)
            try:
                frame_dir.rmdir()
            except OSError:
                pass
    report.frame_orphans_removed = len(frame_orphans)

    # Hidden part pages of long extractions: remove those no Source owns, and write missing
    # ones again where the original reads cheaply. A recording's or image's are only reported.
    from . import extraction_parts

    swept = extraction_parts.sweep(vault_root, dry_run=dry_run)
    report.part_orphans_removed = len(swept.removed)
    report.parts_rebuilt = len(swept.rebuilt)
    report.parts_missing = swept.missing

    # ---- 3. Stable-reference sidecar ----
    from . import memory_refs

    maintenance_jobs.checkpoint("references")

    reference_drift = memory_refs.drift(vault_root)
    if reference_drift and not dry_run:
        memory_refs.ReferenceIndex(vault_root).rebuild_all()
    report.references_refreshed = len(reference_drift)
    report.references_status = "refreshed" if reference_drift else "current"

    remaining_unit_drift = semantic_index.audit_semantic_unit_sidecars(
        vault_root,
        semantic_states,
        include_lexical=include_unit_lexical,
        include_vectors=include_unit_vectors,
        include_graph=include_unit_graph,
    )
    report.semantic_unit_index_remaining = [
        item.as_dict() for item in remaining_unit_drift
    ]
    if dry_run:
        report.semantic_unit_indexes_status = "drifted" if unit_drift else "current"
    elif remaining_unit_drift:
        report.semantic_unit_indexes_status = "degraded"
    elif unit_drift:
        report.semantic_unit_indexes_status = "repaired"
    else:
        report.semantic_unit_indexes_status = "current"

    # ---- 3b. Stranded derived receipts ----
    # A fast-acknowledged batch held in `reconcile_required` owns pages that no
    # drain pass will finish. Converge them from their current bytes and retire
    # the batch once both recall lanes hold those bytes (owner ruling R3).
    from . import derived_receipts, index_sync

    maintenance_jobs.checkpoint("derived-receipts")
    report.derived_batch_reconcile = derived_receipts.reconcile_stranded_batches(
        vault_root,
        converge=index_sync.converge_paths_from_current_bytes,
        dry_run=dry_run,
    )

    # ---- 4. Remaining drift report ----
    maintenance_jobs.checkpoint("drift")
    post = audit_module.audit(
        vault_root,
        categories=[
            "index_drift",
            "embedding_drift",
            "graph_drift",
            "reference_identity",
            "semantic_recall_isolation",
        ],
    )
    report.remaining_drift = [f.as_dict() for f in post.findings]

    # ---- 5. Rebaseline the event-maintained registries ----
    # reconcile is the "I edited around the system, heal it" command — after it
    # runs, no in-memory freshness/inbound registry should keep trusting
    # pre-reconcile state. Freshness is immediately re-derived from final disk
    # state; inbound rebuilds on next read. (The embedding matrix cache is
    # maintained separately by the shared-index memo and its own mtime check.)
    maintenance_jobs.checkpoint("rebaseline")
    if not dry_run:
        from . import freshness
        from . import vault as vault_module

        freshness.rebaseline(vault_root)
        lexstore.rebase_current_catalog_lineage(vault_root)
        vault_module.clear_inbound_index()

    # Receipt evidence is intentionally repaired only here, after ordinary
    # derived-state reconciliation has established any critical-event target.
    from .governance import receipts

    maintenance_jobs.checkpoint("receipts")
    report.receipt_reconcile = receipts.reconcile(vault_root, dry_run=dry_run)

    # A job's receipt reads the counts of what this run did; the full report stays the dispatcher's result.
    maintenance_jobs.note(summary=_job_summary(report))
    return report


def _job_summary(report: ReconcileReport) -> dict[str, Any]:
    """The counts of one run, for a job's receipt."""
    return {
        "plan_id": report.plan_id,
        "indexes_updated": len(report.indexes_updated),
        "embeddings_refreshed": report.embeddings_refreshed,
        "embeddings_status": report.embeddings_status,
        "graph_status": "pending" if report._graph_reconcile_registered is not None or report._graph_rebuild_handoff is not None else report.graph_status,
        "graph_rebuild_status": report.graph_rebuild_status,
        "references_refreshed": report.references_refreshed,
        "clip_orphans_removed": report.clip_orphans_removed,
        "frame_orphans_removed": report.frame_orphans_removed,
        "part_orphans_removed": report.part_orphans_removed,
        "parts_missing": len(report.parts_missing),
        "semantic_suppressed_purged": len(report.semantic_suppressed_purged),
        "semantic_missing_purged": len(report.semantic_missing_purged),
        "semantic_suppressed_corrupt_purged": dict(report.semantic_suppressed_corrupt_purged),
        "lifecycle_prepared_cleaned": len(report.lifecycle_prepared_cleaned),
        "remaining_drift": len(report.remaining_drift),
    }
