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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import deferred_index, semantic_index

log = logging.getLogger(__name__)

_REPORT_PATH_LIMIT = 256
_REPORT_PATH_BYTE_LIMIT = 1024


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
        }:
            raise ValueError("unsupported index component")
        if type(self.outcome) is not str or self.outcome not in {
            "accepted",
            "completed",
            "deferred",
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
            not _bounded_report_path(path)
            for path in (*self.requested_paths, *self.eligible_paths)
        ):
            raise ValueError("index sync report contains an unsafe path")
        if len(self.components) > 6:
            raise ValueError("index sync component count exceeds report bound")
        if len({item.component for item in self.components}) != len(self.components):
            raise ValueError("index sync report contains duplicate components")
        if type(self.paths_truncated) is not bool:
            raise ValueError("paths_truncated must be a boolean")

    @property
    def reconcile_required(self) -> bool:
        return any(item.outcome == "degraded" for item in self.components)

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


def with_component(
    report: IndexSyncReport, outcome: IndexComponentOutcome
) -> IndexSyncReport:
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
    return IndexSyncReport(
        "upsert", requested, requested, components, truncated
    )


def unverified_upsert_report(
    vault_root: Path, written_paths: list[Path]
) -> IndexSyncReport:
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
    return IndexSyncReport(
        "upsert", requested, requested, components, truncated
    )


def observed_delete_report(
    removed_paths: list[str], *, degraded: bool
) -> IndexSyncReport:
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
    return IndexSyncReport(
        "delete", requested, requested, components, truncated
    )


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


def _resolver_component(callback) -> IndexComponentOutcome:
    try:
        callback()
    except Exception:  # noqa: BLE001 - resolver sync must not stop the rest
        log.warning("resolver index dispatch failed", exc_info=True)
        return IndexComponentOutcome("resolver", "degraded", "dispatch_failed")
    return IndexComponentOutcome("resolver", "completed", "dispatch_completed")


def _embedding_component(status) -> IndexComponentOutcome:
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
    return IndexComponentOutcome("embeddings", outcome, status.code)


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
    vault_root: Path, paths: list[Path]
) -> tuple[int, int]:
    from . import index_paths

    rels = [
        rel
        for rel in _rel_md_paths(vault_root, paths)
        if index_paths.is_embeddable_path(vault_root / rel)
    ]
    return len(rels), deferred_index.add(vault_root, rels)


def deferred_work_status(vault_root: Path | None = None) -> dict:
    """No-allocation summary of durable expensive index work."""
    return {
        "semantic_upserts": deferred_index.status(vault_root),
        "full_upserts": deferred_index.full_status(vault_root),
    }


def record_failed_refresh(vault_root: Path, paths: list[Path]) -> int:
    """Persist a failed all-index dispatch without importing model modules."""
    return deferred_index.add_full(vault_root, _rel_md_paths(vault_root, paths))


def clear_deferred_work(
    vault_root: Path | None = None,
    *,
    paths: list[Path] | list[str] | None = None,
) -> int:
    """Clear deferred semantic work after an explicit index/reconcile heal."""
    if vault_root is None:
        return 0
    if paths is None:
        return deferred_index.clear(vault_root) + deferred_index.clear_full(vault_root)
    rels: list[str] = []
    for item in paths:
        if isinstance(item, Path):
            rels.extend(_rel_md_paths(vault_root, [item]))
        else:
            rel = str(item).replace("\\", "/")
            if rel.lower().endswith(".md"):
                rels.append(rel)
    return deferred_index.clear(vault_root, rels) + deferred_index.clear_full(
        vault_root, rels
    )


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
    processed = 0
    full_pending = deferred_index.list_full_paths(
        vault_root, limit=limit if paths is None else None
    )
    if paths is not None:
        requested: set[str] = set()
        for item in paths:
            if isinstance(item, Path):
                requested.update(_rel_md_paths(vault_root, [item]))
            else:
                requested.add(str(item).replace("\\", "/"))
        full_pending = [rel for rel in full_pending if rel in requested]
        if limit is not None:
            full_pending = full_pending[: max(0, limit)]
    if full_pending:
        full_paths = [vault_root / rel for rel in full_pending]
        try:
            dispatched = upsert_after_write(vault_root, full_paths)
        except Exception:  # noqa: BLE001 - durable work must survive a failed dispatch
            log.warning("deferred full-index dispatch failed; work remains queued", exc_info=True)
        else:
            if dispatched is False or (
                isinstance(dispatched, IndexSyncReport)
                and dispatched.reconcile_required
            ):
                log.warning(
                    "deferred full-index dispatch incomplete; work remains queued"
                )
                return processed
            processed += deferred_index.clear_full(vault_root, full_pending)

    if paths is not None:
        return processed

    remaining_limit = None if limit is None else max(0, limit - processed)
    pending = deferred_index.list_paths(vault_root, limit=remaining_limit)
    if not pending:
        return processed
    from . import embeddings

    paths = [vault_root / rel for rel in pending]
    try:
        dispatched = embeddings.upsert_after_write(vault_root, paths)
    except Exception:  # noqa: BLE001 - durable work must survive a failed dispatch
        log.warning("deferred semantic dispatch failed; work remains queued", exc_info=True)
        return processed
    if dispatched is False:
        log.warning("deferred semantic dispatch incomplete; work remains queued")
        return processed
    return processed + clear_deferred_work(vault_root, paths=paths)


def _dispatch_upsert_components(
    vault_root: Path,
    eligible: list[Path],
    *,
    defer_semantic: bool,
) -> list[IndexComponentOutcome]:
    from . import epistemic_graph, find, lexstore, memory_refs, mode

    components = [
        _legacy_component(
            "lexstore", lambda: lexstore.upsert_after_write(vault_root, eligible)
        ),
        _legacy_component(
            "memory_refs",
            lambda: memory_refs.upsert_after_write(vault_root, eligible),
        ),
    ]
    rels = _rel_md_paths(vault_root, eligible)
    components.append(
        _resolver_component(
            lambda: find.on_resolver_files_changed(vault_root, rels, [])
            if rels
            else None
        )
    )
    components.append(
        _legacy_component(
            "epistemic_graph",
            lambda: epistemic_graph.upsert_after_write(vault_root, eligible),
        )
    )
    if defer_semantic or mode.defer_expensive_indexes():
        try:
            semantic_count, added = _record_deferred_semantic_upserts(
                vault_root, eligible
            )
        except Exception:  # noqa: BLE001 - degradation is reported, other lanes landed
            log.warning("durable semantic defer failed", exc_info=True)
            components.append(
                IndexComponentOutcome(
                    "embeddings", "degraded", "durable_defer_failed"
                )
            )
        else:
            if added:
                log.info("deferred semantic indexing for %d markdown file(s)", added)
            if semantic_count:
                components.append(
                    IndexComponentOutcome(
                        "embeddings", "deferred", "deferred_durable"
                    )
                )
            else:
                components.append(
                    IndexComponentOutcome(
                        "embeddings", "accepted", "no_eligible_paths"
                    )
                )
    else:
        from . import embeddings

        try:
            status = embeddings.upsert_after_write_status(vault_root, eligible)
            component = _embedding_component(status)
        except Exception:  # noqa: BLE001 - derived index must not fail a writer
            log.warning("embeddings index dispatch failed", exc_info=True)
            components.append(
                IndexComponentOutcome("embeddings", "degraded", "dispatch_failed")
            )
        else:
            components.append(component)
            if status.status != "completed":
                try:
                    _record_deferred_semantic_upserts(vault_root, eligible)
                except Exception:  # noqa: BLE001 - report remains the primary outcome
                    log.warning(
                        "durable semantic retry recording failed", exc_info=True
                    )
    return components


def upsert_after_write(
    vault_root: Path,
    written_paths: list[Path],
    *,
    defer_semantic: bool = False,
    semantic_states: Mapping[str, semantic_index.SemanticParentIndexState] | None = None,
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
    states = dict(semantic_states or {})
    for path, rel in zip(eligible, eligible_rels, strict=True):
        if path.suffix.lower() != ".md" or rel in states:
            continue
        active = semantic_index.parent_state_for_path(vault_root, path)
        if active is not None:
            states[rel] = active
            continue
        try:
            states[rel] = semantic_index.build_parent_index_state(vault_root, path)
        except (OSError, UnicodeError, ValueError):
            continue
    token = semantic_index.set_parent_states(states)
    try:
        components = _dispatch_upsert_components(
            vault_root, eligible, defer_semantic=defer_semantic
        )
    finally:
        semantic_index.reset_parent_states(token)
    return IndexSyncReport(
        "upsert",
        requested_report,
        eligible_report,
        tuple(components),
        requested_truncated or eligible_truncated,
    )


def delete_after_remove(
    vault_root: Path, removed_rel_paths: list[str]
) -> IndexSyncReport:
    """Fan a removal out to every index sidecar."""
    from . import embeddings, epistemic_graph, find, lexstore, memory_refs

    safe_paths = [
        normalized
        for item in removed_rel_paths
        if (normalized := _safe_relative_path(str(item))) is not None
    ]
    requested_report, paths_truncated = _bounded_paths(safe_paths)
    if not safe_paths:
        return IndexSyncReport("delete", requested_report, requested_report, ())
    components = [
        _legacy_component(
            "lexstore", lambda: lexstore.delete_after_remove(vault_root, safe_paths)
        ),
        _legacy_component(
            "memory_refs",
            lambda: memory_refs.delete_after_remove(vault_root, safe_paths),
        ),
        _legacy_component(
            "epistemic_graph",
            lambda: epistemic_graph.delete_after_remove(vault_root, safe_paths),
        ),
    ]
    try:
        status = embeddings.delete_after_remove_status(vault_root, safe_paths)
        component = _embedding_component(status)
    except Exception:  # noqa: BLE001 - derived index must not stop resolver cleanup
        log.warning("embeddings index delete failed", exc_info=True)
        components.append(
            IndexComponentOutcome("embeddings", "degraded", "dispatch_failed")
        )
    else:
        components.append(component)
    md_rels = [rel for rel in safe_paths if rel.lower().endswith(".md")]
    components.append(
        _resolver_component(
            lambda: find.on_resolver_files_changed(vault_root, [], md_rels)
            if md_rels
            else None
        )
    )
    return IndexSyncReport(
        "delete",
        requested_report,
        requested_report,
        tuple(components),
        paths_truncated,
    )
