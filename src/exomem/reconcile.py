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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

from . import audit as audit_module
from . import indexes, maintenance_jobs, originals, relation_review, semantic_index, semantic_writes
from .vault import PlannedWrite, batch_atomic_write, content_hash, kb_root

log = logging.getLogger(__name__)

_Item = TypeVar("_Item")
_Key = TypeVar("_Key")

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
    parts_unowned: list[str] = field(default_factory=list)
    semantic_suppressed_drift: list[dict[str, str]] = field(default_factory=list)
    semantic_suppressed_purged: list[str] = field(default_factory=list)
    semantic_suppressed_corrupt: list[dict[str, str]] = field(default_factory=list)
    semantic_suppressed_corrupt_purged: dict[str, int] = field(default_factory=dict)
    semantic_suppressed_truncation: dict[str, int] = field(default_factory=dict)
    semantic_missing_drift: list[str] = field(default_factory=list)
    semantic_missing_purged: list[str] = field(default_factory=list)
    semantic_suppressed_incomplete: dict[str, str] = field(default_factory=dict)
    #: The isolation rows that a planned write's plan and its read inside the purge hold do not share,
    #: each with `change`: "unplanned" when only the fresh read holds it, "gone" when only the plan does.
    semantic_isolation_changed: list[dict[str, str]] = field(default_factory=list)
    #: The unit-index parents a write left alone, because their bytes moved after the corpus read.
    semantic_unit_parents_moved: list[str] = field(default_factory=list)
    #: The CLIP and frame orphans that a planned write's plan and its read inside the orphans hold do
    #: not share, each as {kind, path, change}, with `change` as in `semantic_isolation_changed`.
    orphans_changed: list[dict[str, str]] = field(default_factory=list)
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
            "parts_unowned": self.parts_unowned,
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
        if not self.dry_run:
            # Only a write reads its inputs again inside its holds, so only a write can find that one moved.
            result["semantic_isolation_changed"] = self.semantic_isolation_changed
            result["semantic_unit_parents_moved"] = self.semantic_unit_parents_moved
            result["orphans_changed"] = self.orphans_changed
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


@contextmanager
def _run_lock(vault_root: Path, *, dry_run: bool) -> Iterator[None]:
    """Hold this vault's reconcile-run lock for one write run, or refuse the run before it reads anything.

    The whole-run boundary hold used to serialize reconciles; this lock keeps that guarantee without
    blocking writers, because no ordinary writer takes it. A second write run refuses at once with
    `MUTATION_BUSY` and nothing written; its caller's own retry decides whether to ask again. It does
    not wait, because the running reconcile may be a job parked at a `_phase_hold` yield, and a
    foreground request that waited here, outside `foreground_priority.awaiting_bulk()`, would wait on
    a parked bulk pass. It is a plain file lock, as one archive expansion per vault holds, and not a
    mutation coordinator: a coordinator hold would make the run's foreground yields return at once.
    A preview, and a call from outside the writer lease, take none, as `_phase_hold` does.
    """
    from filelock import FileLock, Timeout

    from . import mutation_lock, private_state
    from .cli_ops import OpError
    from .writer_lease import active_mutation_request_id

    if dry_run or active_mutation_request_id() is None:
        yield
        return
    lock = FileLock(private_state.store(vault_root, "reconcile") / "run.lock")
    try:
        lock.acquire(blocking=False)
    except Timeout:
        raise OpError(
            "MUTATION_BUSY",
            "another reconcile is running for this vault",
            "Retry after the running reconcile completes.",
            details={
                "status": "retryable",
                "committed": False,
                "retry_after_ms": mutation_lock.dynamic_retry_after_ms(None),
            },
        ) from None
    try:
        yield
    finally:
        lock.release()


def _phase_hold(vault_root: Path, operation: str, *, dry_run: bool) -> AbstractContextManager[Any]:
    """Hold the mutation boundary for one phase's writes: one short hold, re-read inside, per phase.

    The writer lease runs a reconcile without a whole-run hold, so a phase that writes vault-coupled
    state takes its own hold, which waits out a busy boundary for a bounded time and checks for a stop
    on each side (`maintenance_jobs.chunk_hold`). Under `EXOMEM_WIDE_MUTATION_BOUNDARY` the lease
    holds the boundary for the whole run, and the hold re-enters it. A preview writes nothing, and a
    call from outside the writer lease coordinates its own writes, so neither holds anything here.
    """
    from . import foreground_priority
    from .writer_lease import active_mutation_request_id

    if dry_run or active_mutation_request_id() is None:
        return nullcontext()
    # A writer that queued behind the last hold takes its turn before this run takes the next.
    foreground_priority.yield_to_foreground()
    return maintenance_jobs.chunk_hold(vault_root, operation)


def _phase_holds(
    vault_root: Path,
    operation: str,
    items: Sequence[Any],
    apply: Callable[[Sequence[Any]], bool | None],
    *,
    dry_run: bool,
) -> None:
    """`_phase_hold` for a phase whose work grows with the vault: `apply` takes `items` in short holds.

    Each hold ends after `maintenance_jobs.HOLD_SECONDS` or as soon as a writer waits, and the run lets
    that writer in before it takes the boundary again (`maintenance_jobs.apply_in_holds`). A preview, and
    a call from outside the writer lease, take no hold and apply every item at once, as `_phase_hold` does.
    """
    from .writer_lease import active_mutation_request_id

    if not items:
        return
    if dry_run or active_mutation_request_id() is None:
        apply(items)
        return
    maintenance_jobs.apply_in_holds(vault_root, operation, items, apply)


def _current_hash(vault_root: Path, rel: str) -> str | None:
    """The content hash of a path's bytes now, as the corpus hashes a page, or None with no file there."""
    try:
        return content_hash((vault_root / rel).read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return None


def _read_hashes(vault_root: Path, paths: Iterable[str]) -> dict[str, str]:
    """The content hash of each path that is a readable file now, for a later `_moved_since_read`."""
    hashes: dict[str, str] = {}
    for rel in paths:
        try:
            current = _current_hash(vault_root, rel)
        except (OSError, UnicodeDecodeError):
            continue
        if current is not None:
            hashes[rel] = current
    return hashes


def _corpus_hashes(pages: Mapping[str, Any], paths: Iterable[str]) -> dict[str, str]:
    """The content hash that the run's corpus read for each path that was a corpus page then."""
    return {rel: pages[rel].source_hash for rel in paths if rel in pages}


def _moved_since_read(vault_root: Path, read: Mapping[str, str], paths: Iterable[str]) -> list[str]:
    """The paths whose bytes are not the bytes the run read (`read` maps a path to their content hash).

    A path that `read` lacks was absent, or could not be read, so it moved once a file is there. A
    path that cannot be read now counts as moved. The write that moved a path also indexed it, so a
    phase leaves it to that write.
    """
    moved: list[str] = []
    for rel in paths:
        try:
            current = _current_hash(vault_root, rel)
        except (OSError, UnicodeDecodeError):
            moved.append(rel)
            continue
        if current != read.get(rel):
            moved.append(rel)
    return moved


def _against_plan(
    planned: Iterable[_Key], fresh: Iterable[_Item], key: Callable[[_Item], _Key]
) -> tuple[list[_Item], list[_Item], list[_Key]]:
    """Split what a planned write read inside its hold by its plan.

    Returns the items that both name, the items that only the fresh read holds, and the keys that
    only the plan holds. A planned write applies the first and reports the other two.
    """
    planned_keys = list(dict.fromkeys(planned))
    planned_set = set(planned_keys)
    items = list(fresh)
    fresh_keys = {key(item) for item in items}
    shared = [item for item in items if key(item) in planned_set]
    unplanned = [item for item in items if key(item) not in planned_set]
    return shared, unplanned, [name for name in planned_keys if name not in fresh_keys]


def _media_present(vault_root: Path, path: Path | str) -> bool:
    """Whether a media file is on disk or evicted to the originals store, so its derived rows keep an owner."""
    return (vault_root / path).exists() or originals.evicted(vault_root, path)


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
        if not _media_present(vault_root, video_path):
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
        and not _media_present(vault_root, row.path.removesuffix(".md"))
    }
    return suppressed_paths, missing_paths, missing_clip_orphans


def _isolation_key(row: Any) -> tuple[str, ...]:
    """What a plan names for one isolation row: its kind and path, or a corrupt row by its content."""
    if row.path is None:
        return (
            "corrupt",
            row.component,
            row.edge_column or "",
            hashlib.sha256(row.raw.encode("utf-8", "replace")).hexdigest(),
            repr((row.dependency_source, row.dependency_lookup_key, row.dependency_raw_target)),
        )
    return ("missing" if row.missing else "suppressed", row.path)


def _shared_isolation(
    planned: audit_module.SemanticRecallIsolationCensus, fresh: audit_module.SemanticRecallIsolationCensus
) -> tuple[audit_module.SemanticRecallIsolationCensus, list[dict[str, str]]]:
    """The fresh census cut to the rows its plan names too, and each row that only one of the two holds."""
    planned_rows = {_isolation_key(row): row for row in planned.rows}
    shared, unplanned, gone = _against_plan(planned_rows, fresh.rows, _isolation_key)
    changed = [{**row.as_dict(), "change": "unplanned"} for row in unplanned]
    changed += [{**planned_rows[name].as_dict(), "change": "gone"} for name in gone]
    return replace(fresh, rows=tuple(shared)), changed


def _planned_orphans(
    vault_root: Path, census: _Census, clip_orphans: list[str], frame_orphans: list[tuple[Path, Path]]
) -> tuple[list[str], list[tuple[Path, Path]], list[dict[str, str]]]:
    """The CLIP and frame orphans that a planned write's plan names too, and each that only one holds."""
    clips, unplanned_clips, gone_clips = _against_plan(census.clip_orphans, clip_orphans, lambda clip: clip)
    frames, unplanned_frames, gone_frames = _against_plan(
        census.frame_orphans, frame_orphans, lambda frame: _rel(frame[0], vault_root)
    )
    changed = [{"kind": "clip", "path": clip, "change": "unplanned"} for clip in unplanned_clips]
    changed += [{"kind": "clip", "path": clip, "change": "gone"} for clip in gone_clips]
    changed += [
        {"kind": "frames", "path": _rel(frame_dir, vault_root), "change": "unplanned"}
        for frame_dir, _video in unplanned_frames
    ]
    changed += [{"kind": "frames", "path": rel, "change": "gone"} for rel in gone_frames]
    return clips, frames, changed


def _clip_orphans(vault_root: Path, missing_clip_orphans: set[str]) -> list[str]:
    """CLIP keys with no binary on disk: the census's own and every stored row that lost its file."""
    from . import embeddings as embeddings_module

    if not embeddings_module.clip_enabled():
        return []
    clip_paths, _frame_ts, _matrix = embeddings_module.get_clip_index(vault_root).all_vectors()
    return sorted(
        missing_clip_orphans
        | {p for p in clip_paths if not _media_present(vault_root, p)}
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
    #: Each removal or quarantine as (what it names, its kind), for a preview job's receipt.
    planned: tuple[tuple[str, str], ...]
    #: The CLIP keys and the vault-relative frame directories that the plan removes.
    clip_orphans: tuple[str, ...]
    frame_orphans: tuple[str, ...]


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
    entries += [list(_isolation_key(row)) for row in isolation.corrupt_rows]
    clip_orphans = tuple(_clip_orphans(vault_root, missing_clip_orphans))
    frame_orphans = tuple(_rel(frame_dir, vault_root) for frame_dir, _video in _dangling_frame_dirs(vault_root))
    entries += [["clip", key] for key in clip_orphans]
    entries += [["frames", rel] for rel in frame_orphans]
    from . import extraction_parts

    entries += [["part", rel] for rel in extraction_parts.sweep(vault_root, dry_run=True).removed]
    if lifecycle.cleanup_safe:
        entries += [["lifecycle", item.prepared.page_identity, item.prepared.reference] for item in _cleanup_eligible(lifecycle)]
    entries += [["graph-quarantine", member] for member in quarantine or ()]
    return _Census(
        plan_id=maintenance_jobs.plan_identity("reconcile", {"rebuild_graph": bool(rebuild_graph)}, entries),
        isolation=isolation,
        posthoc_inputs={"corpus": corpus, "language_registry": language, "status_basis": status_basis},
        # Every entry reads [kind, what it names, ...].
        planned=tuple((entry[1], entry[0]) for entry in entries),
        clip_orphans=clip_orphans,
        frame_orphans=frame_orphans,
    )


def reconcile(
    vault_root: Path, *, dry_run: bool = False, rebuild_graph: bool = False, plan_id: str | None = None
) -> ReconcileReport:
    """Heal index-count + embedding drift from out-of-band edits.

    See the module docstring. Read-only when `dry_run=True`. A preview reports the `plan_id` of what
    the run would remove or quarantine. A write that names a `plan_id` is a reviewed one, run by a
    maintenance job: it reads that plan again before its first write and writes nothing if it changed.

    Under the writer lease, the CLI's run and a job's alike hold the mutation boundary only in one
    short hold per phase that writes vault-coupled state (`_phase_hold`), so other writers commit
    between phases. The two phases whose work grows with the vault, the unit sidecars and the
    embeddings, hold in batches that each end when a writer waits (`_phase_holds`). Each phase reads
    again inside its hold what it applies, or checks that what it read earlier has not moved: a page
    whose bytes moved belongs to the write that moved it. The read-only phases (the census, the
    posthoc evaluation, the unit audit and the drift report) and the lexical refresh, which
    serializes its publication itself, hold nothing. A stop lands between holds: the phases before
    it keep their effect, and a repeat converges the rest. A write run holds the vault's
    reconcile-run lock throughout (`_run_lock`), so two write runs never interleave.
    """
    with _run_lock(vault_root, dry_run=dry_run):
        return _reconcile(vault_root, dry_run=dry_run, rebuild_graph=rebuild_graph, plan_id=plan_id)


def _reconcile(vault_root: Path, *, dry_run: bool, rebuild_graph: bool, plan_id: str | None) -> ReconcileReport:
    """One reconcile run, as `reconcile` describes it, under that run's reconcile-run lock."""
    report = ReconcileReport(dry_run=dry_run, graph_rebuild_requested=rebuild_graph)
    census: _Census | None = None
    if dry_run or plan_id is not None:
        # Read before any write, `ensure_manifest` included: a preview names the plan, and a job
        # refuses a changed one with nothing written. A plain write reads nothing extra.
        maintenance_jobs.checkpoint("planning")
        census = _plan_census(vault_root, rebuild_graph)
        report.plan_id = census.plan_id
        if dry_run:
            maintenance_jobs.record_planned(census.planned)
        if plan_id is not None:
            maintenance_jobs.require_current_plan(
                "reconcile", plan_id, census.plan_id, "what reconcile would remove or quarantine"
            )
    if not dry_run:
        from .activation_manifest import ensure_manifest

        with _phase_hold(vault_root, "reconcile_manifest", dry_run=dry_run):
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
    with _phase_hold(vault_root, "reconcile_purging", dry_run=dry_run):
        # A preview reports the census it planned from. A write reads the census again inside this
        # hold, and a planned write applies only the rows that its plan names too: a row that moved
        # since the plan is reported, and the repair stays incomplete, so the cursors stay put.
        suppression_census = census.isolation if dry_run else _isolation_census(vault_root)
        purge_census, isolation_changed = (
            _shared_isolation(census.isolation, suppression_census)
            if census is not None and not dry_run
            else (suppression_census, [])
        )
        report.semantic_suppressed_drift = suppression_census.safe_dicts()
        report.semantic_suppressed_corrupt = suppression_census.corrupt_dicts()
        report.semantic_suppressed_truncation = suppression_census.truncation
        report.semantic_suppressed_incomplete = {
            **suppression_census.incomplete,
            **{item["component"]: "changed_since_plan" for item in isolation_changed},
        }
        report.semantic_isolation_changed = isolation_changed
        suppressed_paths, missing_paths, _ = _isolation_plan(vault_root, suppression_census)
        purge_suppressed, purge_missing, missing_clip_orphans = _isolation_plan(vault_root, purge_census)
        report.semantic_missing_drift = missing_paths
        repair_succeeded = not report.semantic_suppressed_incomplete
        # A live raw Record is present, not missing.  Purge only the semantic
        # derivatives that should never have survived a policy transition; generic
        # delete routing would incorrectly remove its stable identity/reference rows.
        if purge_suppressed and not dry_run:
            from . import index_sync

            if index_sync.purge_semantic_only(vault_root, purge_suppressed):
                report.semantic_suppressed_purged = purge_suppressed
            else:
                repair_succeeded = False
        if purge_census.corrupt_rows and not dry_run:
            report.semantic_suppressed_corrupt_purged = (
                audit_module.purge_corrupt_semantic_recall_isolation_rows(
                    vault_root, purge_census.corrupt_rows
                )
            )
            expected_components = {
                "graph"
                if row.component
                in {"graph_edges", "graph_dependencies", "graph_dependency_coverage"}
                else row.component
                for row in purge_census.corrupt_rows
            }
            if not expected_components <= set(report.semantic_suppressed_corrupt_purged):
                repair_succeeded = False
        if purge_missing and not dry_run:
            from . import embeddings, index_sync

            # These are lexically validated, absent Markdown identities: ordinary
            # removal routing is correct and intentionally clears stable identity
            # sidecars too. CLIP stores the corresponding media key without `.md`.
            missing_report = index_sync.delete_after_remove(vault_root, purge_missing)
            embeddings.get_clip_index(vault_root).purge_markdown_paths_if_present(purge_missing)
            report.semantic_missing_purged = purge_missing
            repair_succeeded &= not missing_report.reconcile_required
        if not dry_run and repair_succeeded:
            deferred_index.set_semantic_isolation_cursors(
                vault_root,
                suppression_census.continuation if suppression_census.truncation else {},
            )

    maintenance_jobs.checkpoint("units")
    # The audit and the values it is scoped by are read with no hold. A stale scope only changes
    # which sidecars the audit compares, and `initial_graph_drift` only decides whether the graph
    # phase looks again: that phase reads the drift again inside its own hold before it repairs.
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
    outside_corpus_hashes: dict[str, str] = {}
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
        # A parent outside the corpus was no page that the corpus read. Its bytes are recorded now, at
        # the audit, so the hold removes its rows only while it stays as the audit found it. Rows
        # stamped with the parent's bytes now were indexed by a write that the corpus read missed:
        # that parent is not recorded, so it counts as moved and its rows stay with that write.
        indexed_for: dict[str, set[str]] = {}
        for item in unit_drift:
            if item.parent_path not in semantic_states:
                indexed_for.setdefault(item.parent_path, set()).update(item.actual_source_hashes)
        outside_corpus_hashes = {
            rel: digest
            for rel, digest in _read_hashes(vault_root, indexed_for).items()
            if digest not in indexed_for[rel]
        }
        read_hashes = {**_corpus_hashes(semantic_batch.corpus.pages, affected), **outside_corpus_hashes}
        moved: set[str] = set()
        removed: list[str] = []
        refreshed: list[str] = []

        def unmoved(batch: Sequence[str]) -> list[str]:
            # A parent whose bytes moved since they were read was indexed by the write that moved it, so
            # the run leaves it and reports it. Removing an orphan's rows would undo that write. Refreshing
            # a corpus page would repeat it, since each sidecar re-parses a planned state whose bytes moved.
            gone = set(_moved_since_read(vault_root, read_hashes, batch))
            moved.update(gone)
            return [path for path in batch if path not in gone]

        def remove_orphans(batch: Sequence[str]) -> None:
            if kept := unmoved(batch):
                index_sync.delete_after_remove(vault_root, kept)
                removed.extend(kept)

        def refresh_parents(batch: Sequence[str]) -> None:
            if kept := unmoved(batch):
                index_sync.upsert_after_write(
                    vault_root,
                    [vault_root / path for path in kept],
                    semantic_states={path: semantic_states[path] for path in kept},
                )
                refreshed.extend(kept)

        # The fan-out costs each parent its own sidecar writes, so a vault with many drifted parents runs
        # it for minutes: in short holds, with a waiting writer let in between them.
        _phase_holds(vault_root, "reconcile_units", orphans, remove_orphans, dry_run=dry_run)
        _phase_holds(vault_root, "reconcile_units", affected, refresh_parents, dry_run=dry_run)
        report.semantic_unit_parents_moved = sorted(moved)
        report.semantic_unit_parents_refreshed = len(refreshed)
        report.semantic_unit_orphans_removed = len(removed)
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
    eligible = _cleanup_eligible(lifecycle_batch)
    if not dry_run and lifecycle_batch.cleanup_safe and eligible:
        with _phase_hold(vault_root, "reconcile_lifecycle", dry_run=dry_run):
            # The inspection classified each slot against its owner's bytes in the corpus read. A
            # slot whose owner moved since then is left for the next run to classify again. The
            # cleanup checks the rest itself: the owners' identities, the slots and the trash.
            owners = sorted({path for item in eligible for path in item.live_owner_paths})
            moved = set(
                _moved_since_read(vault_root, _corpus_hashes(semantic_batch.corpus.pages, owners), owners)
            )
            raced = [item for item in eligible if moved.intersection(item.live_owner_paths)]
            cleanup = relation_review.cleanup_stale_lifecycle_prepared_batch(
                vault_root,
                tuple(item for item in eligible if not moved.intersection(item.live_owner_paths)),
            )
        blocked = (
            *cleanup.blocked,
            *(
                relation_review.LifecyclePreparedIssue("LIFECYCLE_PRIMARY_RACE", item.prepared.page_identity)
                for item in raced
            ),
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
            tuple(issue.as_dict() for issue in blocked)
        )
    kb = kb_root(vault_root)

    # ---- 1. Index counts (recompute from disk; preserve curated text) ----
    maintenance_jobs.checkpoint("indexes")
    with _phase_hold(vault_root, "reconcile_indexes", dry_run=dry_run):
        # Counted inside the hold, so the counts it writes are the vault's as it writes them.
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
        drifted: dict[str, Path] = {}
        missing_rels: list[str] = []
        for finding in drift:
            rel = audit_module._safe_persisted_markdown_rel(str(finding.path))
            if rel is None:
                continue
            candidate = audit_module._no_follow_regular_markdown_path(vault_root, rel)
            if candidate is not None:
                drifted[rel] = candidate
            else:
                missing_rels.append(rel)
        drifted_abs = list(drifted.values())
        refresh_succeeded = True
        if not dry_run and (missing_rels or drifted_abs):
            from . import embeddings, index_sync, mode, semantic_drain

            # Found with no hold, then checked again inside each: a file that came back, or went,
            # since the drift check belongs to the write that moved it. A refresh reads the
            # current bytes, so a page edited since then is embedded as it is now.
            def remove_missing(batch: Sequence[str]) -> None:
                gone = [rel for rel in batch if audit_module._no_follow_regular_markdown_path(vault_root, rel) is None]
                if gone:
                    index_sync.delete_after_remove(vault_root, gone)

            refreshed_paths: list[Path] = []

            def refresh_drifted(batch: Sequence[str]) -> bool:
                nonlocal refresh_succeeded
                present = [
                    drifted[rel] for rel in batch
                    if audit_module._no_follow_regular_markdown_path(vault_root, rel) is not None
                ]
                if not present:
                    return True
                if mode.service_profile_enabled():
                    semantic_drain.request(vault_root, present, edited=False)
                    refresh_succeeded = False
                    return True
                refresh_succeeded = embeddings.upsert_after_write(vault_root, present) is not False
                if refresh_succeeded:
                    index_sync.clear_deferred_work(vault_root, paths=present)
                    refreshed_paths.extend(present)
                return refresh_succeeded

            # Encoding every drifted page took one hold for as long as the model ran, minutes on a vault
            # that an import had just filled. Short holds let a waiting writer in between batches.
            _phase_holds(vault_root, "reconcile_embeddings", missing_rels, remove_missing, dry_run=dry_run)
            _phase_holds(vault_root, "reconcile_embeddings", list(drifted), refresh_drifted, dry_run=dry_run)
            drifted_abs = refreshed_paths
        report.embeddings_refreshed = len(drifted_abs) if refresh_succeeded else 0
        if not refresh_succeeded:
            report.embeddings_status = "deferred"
        else:
            report.embeddings_status = "refreshed" if drifted_abs else "current"

    # ---- 2b. Lexical sidecar (count/mtime reconcile against the walk) ----
    maintenance_jobs.checkpoint("lexical")
    # No hold: the store serializes its own publication against every writer with its own lock.
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
        # One hold for the whole phase: lineage isolation, checkpoint recovery and the sweeps move
        # durable graph state, and the drift that decides a repair is read again inside it. The
        # graph build itself runs in its registered flight, which this hold only starts.
        with _phase_hold(vault_root, "reconcile_graph", dry_run=dry_run):
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
    # Hidden part pages of long extractions: remove those left over from a longer extraction,
    # write missing ones again where the original reads cheaply, and report the rest.
    from . import extraction_parts

    # Found with no hold, then checked again inside one: an orphan that has its media back now
    # belongs to the write that restored it.
    clip_orphans = _clip_orphans(vault_root, missing_clip_orphans)
    frame_orphans = _dangling_frame_dirs(vault_root)
    swept = extraction_parts.sweep(vault_root, dry_run=True)
    # A planned write also takes the hold when every orphan that its plan names has gone, to report them.
    planned_orphans = census is not None and bool(census.clip_orphans or census.frame_orphans)
    if not dry_run and (clip_orphans or frame_orphans or swept.removed or swept.missing or planned_orphans):
        with _phase_hold(vault_root, "reconcile_orphans", dry_run=dry_run):
            clip_orphans = [key for key in clip_orphans if not _media_present(vault_root, key)]
            frame_orphans = [
                (frame_dir, video_path)
                for frame_dir, video_path in frame_orphans
                if not _media_present(vault_root, video_path)
            ]
            if census is not None:
                # A planned write removes only the orphans that its plan names too, and reports the rest.
                clip_orphans, frame_orphans, report.orphans_changed = _planned_orphans(
                    vault_root, census, clip_orphans, frame_orphans
                )
            if clip_orphans:
                embeddings_module.delete_clip_after_remove(vault_root, clip_orphans)
            for frame_dir, video_path in frame_orphans:
                scene_frames.clear_scene_frames(vault_root, video_path)
                try:
                    frame_dir.rmdir()
                except OSError:
                    pass
            if swept.removed or swept.missing:
                # The sweep reads every part again under this hold before it removes or writes one.
                swept = extraction_parts.sweep(vault_root, dry_run=False)
    report.clip_orphans_removed = len(clip_orphans)
    report.frame_orphans_removed = len(frame_orphans)
    report.part_orphans_removed = len(swept.removed)
    report.parts_rebuilt = len(swept.rebuilt)
    report.parts_missing = swept.missing
    report.parts_unowned = swept.unowned

    # ---- 3. Stable-reference sidecar ----
    from . import memory_refs

    maintenance_jobs.checkpoint("references")

    reference_drift = memory_refs.drift(vault_root)
    if reference_drift and not dry_run:
        # The rebuild reads every page again under this hold, then replaces the table.
        with _phase_hold(vault_root, "reconcile_references", dry_run=dry_run):
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
    if not dry_run:
        # A parent that moved since the run read it is its own write's, not drift this run left.
        parents = sorted({item.parent_path for item in remaining_unit_drift})
        moved_since = set(
            _moved_since_read(
                vault_root, {**_corpus_hashes(semantic_batch.corpus.pages, parents), **outside_corpus_hashes}, parents
            )
        )
        remaining_unit_drift = tuple(
            item for item in remaining_unit_drift if item.parent_path not in moved_since
        )
        report.semantic_unit_parents_moved = sorted(set(report.semantic_unit_parents_moved) | moved_since)
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
    with _phase_hold(vault_root, "reconcile_derived-receipts", dry_run=dry_run):
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

        with _phase_hold(vault_root, "reconcile_rebaseline", dry_run=dry_run):
            freshness.rebaseline(vault_root)
            vault_module.clear_inbound_index()
        # Outside the hold, as warm-up calls it: it serializes on the lexical publication barrier, where
        # every catalog write commits, and stamps only a stored state equal to the live one, so it needs no
        # boundary, and its wait for that barrier (up to 30 s) no longer blocks writers.
        lexstore.rebase_current_catalog_lineage(vault_root)

    # Receipt evidence is intentionally repaired only here, after ordinary
    # derived-state reconciliation has established any critical-event target.
    # It holds the boundary: it closes intents and tombstones from the vault as it reads it, and a
    # governed write holds the boundary from its intent to its terminal.
    from .governance import receipts

    maintenance_jobs.checkpoint("receipts")
    with _phase_hold(vault_root, "reconcile_receipts", dry_run=dry_run):
        report.receipt_reconcile = receipts.reconcile(vault_root, dry_run=dry_run)

    # A job's receipt reads the counts of what this run did; the full report stays the dispatcher's result.
    maintenance_jobs.note(summary=_job_summary(report))
    return report


def _job_summary(report: ReconcileReport) -> dict[str, Any]:
    """The counts of one run, for a job's receipt."""
    summary = {
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
        "parts_unowned": len(report.parts_unowned),
        "semantic_suppressed_purged": len(report.semantic_suppressed_purged),
        "semantic_missing_purged": len(report.semantic_missing_purged),
        "semantic_suppressed_corrupt_purged": dict(report.semantic_suppressed_corrupt_purged),
        "lifecycle_prepared_cleaned": len(report.lifecycle_prepared_cleaned),
        "remaining_drift": len(report.remaining_drift),
    }
    if not report.dry_run:
        # As in `as_dict`: only a write reads its inputs again inside its holds.
        summary["semantic_isolation_changed"] = len(report.semantic_isolation_changed)
        summary["semantic_unit_parents_moved"] = len(report.semantic_unit_parents_moved)
        summary["orphans_changed"] = len(report.orphans_changed)
    return summary
