"""The `recover_from_trash` Tier 2 op: undo a `delete_file`/`delete_directory`.

Reads the `.meta.json` sidecar to discover the original path, moves the
trashed file/dir back there, and cleans up the sidecar. The ergonomic
counterpart to the trash semantics — without this, callers had to know
the trash path format AND the original-path encoding to recover.

Refuses to overwrite an existing file at the restore destination — pick
a different `restore_path` if the original location is now occupied.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import logging
import time
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from . import (
    graph_sync,
    held_fs,
    originals,
    relation_review,
    reserved_paths,
    semantic_index,
    semantic_writes,
    trash_metadata,
)
from .governance import catalog_publication, lifecycle
from .kbdir import kb_dirname, kb_prefix
from .trash_metadata import METADATA_SUFFIX, TRASH_SUBPATH
from .trash_metadata import original_path as trash_original_path
from .vault import (
    DIRECTORY_TRANSITION_LIMIT,
    DirectoryCensusGuard,
    FrontmatterError,
    PathGuard,
    PathGuardError,
    VaultPathError,
    batch_atomic_write,
    content_hash,
    in_append_only_tree,
    in_curated_tree,
    parse_frontmatter,
    plan_log_entry,
    read_guarded_text,
    resolve_under_vault,
    write_log_entry,
)
from .vault_store import targets as store_targets

log = logging.getLogger(__name__)



@dataclass
class RecoverResult:
    trash_path: str
    restored_path: str
    kind: str  # "file" | "directory"
    warnings: list[str]
    semantic: dict | None = None
    index: dict | None = None

    def as_dict(self) -> dict:
        return {
            "trash_path": self.trash_path,
            "restored_path": self.restored_path,
            "kind": self.kind,
            "warnings": self.warnings,
            "semantic": self.semantic,
            "index": self.index,
        }


@dataclass
class RecoverError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def recover_from_trash(
    vault_root: Path,
    *,
    trash_path: str,
    restore_path: str | None = None,
    allow_curated: bool = False,
    today: dt.date | None = None,
    validate_only: bool = False,
    relation_reviews: Mapping[str, Mapping[str, str]] | None = None,
    _fold: bool = False,
) -> RecoverResult:
    from .media_store import local_originals

    with local_originals(vault_root, (trash_path,)):
        return _recover_from_trash_local(vault_root, trash_path=trash_path, restore_path=restore_path, allow_curated=allow_curated, today=today, validate_only=validate_only, relation_reviews=relation_reviews, _fold=_fold)


def _recover_from_trash_local(
    vault_root: Path,
    *,
    trash_path: str,
    restore_path: str | None = None,
    allow_curated: bool = False,
    today: dt.date | None = None,
    validate_only: bool = False,
    relation_reviews: Mapping[str, Mapping[str, str]] | None = None,
    _fold: bool = False,
) -> RecoverResult:
    if store_targets.owns_any(vault_root, files=(trash_path,)):
        return _recover_in_store(
            vault_root,
            trash_path=trash_path,
            restore_path=restore_path,
            allow_curated=allow_curated,
            today=today,
            validate_only=validate_only,
            relation_reviews=relation_reviews,
            fold=_fold,
        )
    try:
        trash_abs, trash_rel = resolve_under_vault(
            vault_root, trash_path, must_exist=True
        )
    except VaultPathError as e:
        raise RecoverError(code=e.code, reason=e.reason) from e

    _refuse_outside_trash(trash_rel)

    # Bind and classify the complete source before reading its sidecar, parsing
    # Markdown, counting children, or allocating lifecycle state. The later
    # lifecycle manifest performs the immediate pre-rename recheck.
    trash_file_snapshot = None
    trash_tree: tuple[reserved_paths.GenericTreeFile, ...] = ()
    try:
        if trash_abs.is_file():
            trash_kind = "file"
            trash_file_snapshot = reserved_paths.read_generic_bytes(vault_root, trash_rel)
        elif trash_abs.is_dir():
            trash_kind = "directory"
            trash_tree = reserved_paths.read_generic_tree(vault_root, trash_rel)
        else:
            raise reserved_paths.ReservedPathLeafError("UNSAFE_PATH")
    except reserved_paths.ReservedPathLeafError as error:
        code = "NOT_FOUND" if error.code == "MISSING" else "RESERVED_PATH"
        raise RecoverError(
            code=code,
            reason="trash source could not be acquired through the generic boundary",
        ) from None

    if trash_kind == "file" and trash_rel.lower().endswith(".md"):
        _refuse_reference_companion(trash_file_snapshot.data)

    sidecar, meta, sidecar_guard, sidecar_proof_guard, sidecar_source, sidecar_identity = (
        _read_sidecar(vault_root, trash_abs)
    )
    restore_abs, restore_rel, curated = _restore_destination(
        vault_root,
        restore_path=restore_path,
        meta=meta,
        sidecar_name=sidecar.name,
        allow_curated=allow_curated,
        fold=_fold,
    )
    if trash_kind == "file":
        assert trash_file_snapshot is not None
        restored = ((restore_rel, trash_file_snapshot.data),)
    else:
        restored = tuple(
            (f"{restore_rel.rstrip('/')}/{item.relative_path}", item.snapshot.data)
            for item in trash_tree
        )
    from .collection_store import authority as collection_authority

    # Decided before the existence check below, so it cannot tell a withheld view from none.
    collection_authority.refuse_view_paths(
        vault_root, (restore_rel, *(path for path, _data in restored)), RecoverError
    )

    try:
        reserved_paths.inspect_generic_path(vault_root, restore_rel)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            pass
        elif error.code in {
            "CAPABILITY_UNAVAILABLE",
            "IDENTITY_CHANGED",
            "RESERVED_PATH",
            "UNSAFE_PATH",
        }:
            raise RecoverError(
                code="RESERVED_PATH",
                reason="restore destination is reserved for its owning subsystem",
            ) from None
        else:
            raise RecoverError(
                code="RECOVER_FAILED",
                reason="restore destination could not be acquired safely",
            ) from None
    else:
        raise _destination_exists(restore_rel)
    if trash_kind == "file" and trash_rel.lower().endswith(".md"):
        from . import extraction_parts

        parts_destination = extraction_parts.folder_for(Path(restore_rel))
        if extraction_parts.folder_for(trash_abs).is_dir() and (vault_root / parts_destination).exists():
            raise RecoverError(
                code="DEST_EXISTS",
                reason=(
                    f"the part pages of {restore_rel!r} would land on {parts_destination.as_posix()!r}, "
                    "which already exists. Choose a different restore_path, or move it out of the way first."
                ),
            )

    initial_restore_state = tuple(
        sorted((path, hashlib.sha256(data).hexdigest()) for path, data in restored)
    )
    catalog_content_paths = tuple(path for path, _digest in initial_restore_state)

    today = today or dt.date.today()
    date_iso = today.isoformat()
    restore_no_ext = restore_rel.removesuffix(".md")
    log_body = _log_body(trash_rel, restore_rel, trash_kind, curated if allow_curated else None)
    log_plan = plan_log_entry(
        vault_root,
        date_iso=date_iso,
        op="recover_from_trash",
        rel_path_no_ext=restore_no_ext,
        body=log_body,
    )
    catalog_now = int(time.time())

    # Open/schema-v3 restores keep their existing behavior. Exact-v4 restores
    # must refuse every currently unsupported content kind before lifecycle
    # state or canonical placement changes.
    if any(not path.lower().endswith(".md") for path in catalog_content_paths):
        try:
            catalog_publication.prepare_catalog_membership_batch(
                vault_root,
                writes=log_plan.writes,
                content_paths=catalog_content_paths,
                now=catalog_now,
            )
        except catalog_publication.CatalogPublicationError as error:
            raise RecoverError(
                code="GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
                reason=str(error),
            ) from error

    semantic: dict | None = None
    lifecycle_operation: lifecycle.LifecycleOperation | None = None
    retained_companion: tuple[str, reserved_paths.GenericFileSnapshot] | None = None
    retention_unresolved = False
    if trash_kind == "file" and not trash_rel.lower().endswith(".md"):
        try:
            retained_companion = trash_metadata.reference_companion(vault_root, trash_rel)
        except (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
            retention_unresolved = True
    graph_transition: graph_sync.GraphLifecycleTransition | None = None
    semantic_states: dict[str, semantic_index.SemanticParentIndexState] = {}
    recovery_entries: list[semantic_writes.RecoveryEntry] = []
    destination_root_guard: PathGuard | None = None
    trash_census_guards: tuple[DirectoryCensusGuard, ...] = ()
    catalog_published = False
    if trash_kind == "file" and trash_rel.lower().endswith(".md"):
        try:
            source, source_guard = read_guarded_text(vault_root, trash_abs)
            destination_guard = PathGuard.capture(
                vault_root, restore_rel, leaf_policy="absent"
            )
            destination_root_guard = destination_guard
            recovery_entries.append(
                semantic_writes.RecoveryEntry(
                    trash_rel,
                    trash_original_path(meta, trash_root=trash_rel, current_path=trash_rel, fallback_root=restore_rel),
                    restore_rel,
                    source,
                    source_guard,
                    destination_guard,
                    sidecar_proof_guard,
                    sidecar_source,
                )
            )
        except (OSError, UnicodeDecodeError, ValueError, PathGuardError) as error:
            code = getattr(error, "code", "RECOVER_FAILED")
            raise RecoverError(code=code, reason=str(error)) from error
    elif trash_kind == "directory":
        try:
            markdown = sorted(
                trash_abs.rglob("*.md"), key=lambda item: item.as_posix()
            )
            if markdown:
                destination_root_guard = PathGuard.capture(
                    vault_root, restore_rel, leaf_policy="absent"
                )
                directories = [trash_abs, *(path for path in trash_abs.rglob("*") if path.is_dir())]
                if len(directories) > DIRECTORY_TRANSITION_LIMIT:
                    raise RecoverError(
                        code="PATH_GUARD_LIMIT",
                        reason="trash directory exceeds the bounded recovery census",
                    )
                trash_census_guards = tuple(
                    DirectoryCensusGuard.capture(
                        vault_root,
                        path.relative_to(vault_root).as_posix(),
                        max_entries=DIRECTORY_TRANSITION_LIMIT,
                    )
                    for path in sorted(directories, key=lambda item: item.as_posix())
                )
                for markdown_path in markdown:
                    suffix = markdown_path.relative_to(trash_abs).as_posix()
                    source_path = f"{trash_rel}/{suffix}"
                    original_path = trash_original_path(
                        meta, trash_root=trash_rel, current_path=source_path, fallback_root=restore_rel,
                    )
                    destination_path = f"{restore_rel.rstrip('/')}/{suffix}"
                    source, source_guard = read_guarded_text(
                        vault_root, markdown_path
                    )
                    recovery_entries.append(
                        semantic_writes.RecoveryEntry(
                            source_path,
                            original_path,
                            destination_path,
                            source,
                            source_guard,
                            PathGuard.capture(
                                vault_root,
                                destination_path,
                                leaf_policy="absent",
                            ),
                            sidecar_proof_guard,
                            sidecar_source,
                        )
                    )
        except (OSError, UnicodeDecodeError, ValueError, PathGuardError) as error:
            code = getattr(error, "code", "RECOVER_FAILED")
            raise RecoverError(code=code, reason=str(error)) from error

    if recovery_entries:
        assert destination_root_guard is not None
        try:
            preflight = semantic_writes.preflight_recovery(
                vault_root,
                entries=recovery_entries,
                destination_root_guard=destination_root_guard,
                trash_census_guards=trash_census_guards,
                recovery_sidecar_guard=sidecar_guard,
                catalog_auxiliary_writes=log_plan.writes,
                catalog_content_paths=catalog_content_paths,
                catalog_publication_now=catalog_now,
                relation_reviews=relation_reviews,
            )
            semantic_states = {
                item.after.path: semantic_index.from_semantic_page_state(item.after)
                for item in preflight.evaluations
            }
            if validate_only:
                return RecoverResult(
                    trash_path=trash_rel,
                    restored_path=restore_rel,
                    kind=trash_kind,
                    warnings=[],
                    semantic=preflight.as_dict(),
                )

            try:
                graph_transition = graph_sync.begin_recovery_transition(
                    vault_root,
                    trash_rel=trash_rel,
                    source_rel=restore_rel,
                    restored_paths=[
                        (entry.restore_path, entry.source)
                        for entry in recovery_entries
                    ],
                )
            except lifecycle.LifecycleError as error:
                raise RecoverError(code=error.code, reason=error.reason) from error
            except graph_sync.GraphLifecycleEpochSetupError as error:
                raise RecoverError(
                    code="GRAPH_SYNC_EPOCH_FAILED",
                    reason="could not establish the graph recovery epoch",
                ) from error
            except graph_sync.GraphLifecycleRollbackError as error:
                raise RecoverError(
                    code="GRAPH_SYNC_RECOVERY_ROLLBACK_FAILED",
                    reason="the staged recovery transition could not be restored",
                ) from error
            lifecycle_operation = graph_transition.operation
            transition_restore_state = tuple(
                sorted(
                    (item.source_path, item.content_hash)
                    for item in lifecycle_operation.manifest
                )
            )
            if transition_restore_state != initial_restore_state:
                raise RecoverError(
                    code="PATH_GUARD_CHANGED",
                    reason="trash contents changed before the recovery transition",
                )

            def restore() -> None:
                try:
                    assert graph_transition is not None
                    graph_transition.rename()
                except lifecycle.LifecycleError as error:
                    raise RecoverError(
                        code=error.code,
                        reason=error.reason,
                    ) from error
                try:
                    graph_transition.publish_checkpoint()
                except Exception as error:  # noqa: BLE001 - outer abort reverses the move
                    raise RecoverError(
                        code="GRAPH_SYNC_RECOVERY_CHECKPOINT_FAILED",
                        reason="graph checkpoint failed; recovery will be reversed",
                    ) from error
            committed = semantic_writes.commit_recovery(
                vault_root, preflight=preflight, mutate=restore
            )
            catalog_published = committed.catalog_published
            semantic = committed.as_dict()
        except semantic_writes.SemanticWriteError as error:
            if (
                graph_transition is not None
                and error.code != "GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN"
            ):
                try:
                    graph_transition.abort()
                except graph_sync.GraphLifecycleRollbackError as rollback_error:
                    raise RecoverError(
                        code="GRAPH_SYNC_RECOVERY_ROLLBACK_FAILED",
                        reason="recovery could not be reversed; reconcile is required",
                    ) from rollback_error
            raise RecoverError(code=error.code, reason=error.reason) from error
        except PathGuardError as error:
            if graph_transition is not None:
                try:
                    graph_transition.abort()
                except graph_sync.GraphLifecycleRollbackError as rollback_error:
                    raise RecoverError(
                        code="GRAPH_SYNC_RECOVERY_ROLLBACK_FAILED",
                        reason="recovery could not be reversed; reconcile is required",
                    ) from rollback_error
            raise RecoverError(code=error.code, reason=error.reason) from error
        except RecoverError:
            if graph_transition is not None:
                try:
                    graph_transition.abort()
                except graph_sync.GraphLifecycleRollbackError as rollback_error:
                    raise RecoverError(
                        code="GRAPH_SYNC_RECOVERY_ROLLBACK_FAILED",
                        reason="recovery could not be reversed; reconcile is required",
                    ) from rollback_error
            raise
    else:
        if relation_reviews:
            raise RecoverError(
                code="INVALID_RELATION_REVIEW",
                reason="recovery review mapping has no validated Markdown entry",
            )
        if validate_only:
            return RecoverResult(
                trash_path=trash_rel,
                restored_path=restore_rel,
                kind=trash_kind,
                warnings=[],
            )
        direct_catalog_target = None
        if log_plan.writes:
            try:
                direct_catalog_target = (
                    catalog_publication.prepare_catalog_membership_batch(
                        vault_root,
                        writes=log_plan.writes,
                        content_paths=catalog_content_paths,
                        now=catalog_now,
                    )
                )
            except catalog_publication.CatalogPublicationError as error:
                raise RecoverError(
                    code="GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
                    reason=str(error),
                ) from error
        try:
            graph_transition = graph_sync.begin_recovery_transition(
                vault_root,
                trash_rel=trash_rel,
                source_rel=restore_rel,
                restored_paths=[],
            )
        except lifecycle.LifecycleError as error:
            raise RecoverError(code=error.code, reason=error.reason) from error
        except graph_sync.GraphLifecycleEpochSetupError as error:
            raise RecoverError(
                code="GRAPH_SYNC_EPOCH_FAILED",
                reason="could not establish the graph recovery epoch",
            ) from error
        except graph_sync.GraphLifecycleRollbackError as error:
            raise RecoverError(
                code="GRAPH_SYNC_RECOVERY_ROLLBACK_FAILED",
                reason="the staged recovery transition could not be restored",
            ) from error
        lifecycle_operation = graph_transition.operation
        transition_restore_state = tuple(
            sorted(
                (item.source_path, item.content_hash)
                for item in lifecycle_operation.manifest
            )
        )
        if transition_restore_state != initial_restore_state:
            try:
                graph_transition.abort()
            except graph_sync.GraphLifecycleRollbackError as rollback_error:
                raise RecoverError(
                    code="GRAPH_SYNC_RECOVERY_ROLLBACK_FAILED",
                    reason="recovery could not be reversed; reconcile is required",
                ) from rollback_error
            raise RecoverError(
                code="PATH_GUARD_CHANGED",
                reason="trash contents changed before the recovery transition",
            )
        try:
            graph_transition.rename()
            graph_transition.publish_checkpoint()
            if direct_catalog_target is not None and log_plan.writes:
                batch_atomic_write(log_plan.writes, vault_root=vault_root)
            from .writer_lease import mark_active_mutation_committed

            mark_active_mutation_committed()
        except lifecycle.LifecycleError as e:
            try:
                graph_transition.abort()
            except graph_sync.GraphLifecycleRollbackError as rollback_error:
                raise RecoverError(
                    code="GRAPH_SYNC_RECOVERY_ROLLBACK_FAILED",
                    reason="recovery could not be reversed; reconcile is required",
                ) from rollback_error
            raise RecoverError(
                code=e.code,
                reason=e.reason,
            ) from e
        except RecoverError:
            try:
                graph_transition.abort()
            except graph_sync.GraphLifecycleRollbackError as rollback_error:
                raise RecoverError(
                    code="GRAPH_SYNC_RECOVERY_ROLLBACK_FAILED",
                    reason="recovery could not be reversed; reconcile is required",
                ) from rollback_error
            raise
        except Exception as error:  # noqa: BLE001 - reverse a caught epoch failure
            try:
                graph_transition.abort()
            except graph_sync.GraphLifecycleRollbackError as rollback_error:
                raise RecoverError(
                    code="GRAPH_SYNC_RECOVERY_ROLLBACK_FAILED",
                    reason="recovery could not be reversed; reconcile is required",
                ) from rollback_error
            raise RecoverError(
                code="GRAPH_SYNC_RECOVERY_CHECKPOINT_FAILED",
                reason="graph checkpoint failed; recovery was reversed",
            ) from error
        if direct_catalog_target is not None:
            try:
                catalog_publication.publish_markdown_batch(direct_catalog_target)
            except catalog_publication.CatalogPublicationError as error:
                raise RecoverError(
                    code="GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN",
                    reason=str(error),
                ) from error
            catalog_published = True

    warnings: list[str] = []
    keep_metadata = retention_unresolved
    if retained_companion is not None:
        companion_path, companion_snapshot = retained_companion
        try:
            trash_metadata.remove_unchanged(
                vault_root, companion_path, companion_snapshot.identity, companion_snapshot.data,
            )
        except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
            keep_metadata = True
    if keep_metadata:
        warnings.append("recovered bytes; retained the changed or unreadable Trash companion and its placement metadata")
    else:
        warnings.extend(_settle_sidecar(vault_root, sidecar, sidecar_guard, sidecar_identity))

    index_feedback: dict | None = None
    restored_markdown = (
        sorted(restore_abs.rglob("*.md"))
        if restore_abs.is_dir()
        else ([restore_abs] if restore_abs.suffix.lower() == ".md" else [])
    )
    if trash_kind == "file" and restore_abs.suffix.lower() == ".md":
        # The part pages of a long extraction went to Trash beside their page and come back with it.
        from . import extraction_parts

        try:
            carried = extraction_parts.carry(vault_root, trash_rel, restore_rel)
        except OSError:
            log.warning("the part pages of %s stayed in Trash", trash_rel, exc_info=True)
            warnings.append(
                "recovered the page; its part pages stayed in "
                f"{extraction_parts.folder_for(Path(trash_rel)).as_posix()}"
            )
        else:
            restored_markdown += [vault_root / after for _before, after in carried]
    if restored_markdown:
        from . import file_watcher, index_sync

        fanout_unverified = False
        try:
            file_watcher.register_self_write(vault_root, restored_markdown)
        except Exception:  # noqa: BLE001 - suppression is independently observed
            log.exception("restored watcher suppression failed for %s", restore_rel)
            watcher_outcome = index_sync.IndexComponentOutcome(
                "watcher", "degraded", "self_write_registration_failed"
            )
            warnings.append(
                "recovery succeeded but watcher suppression degraded; run reconcile"
            )
        else:
            watcher_outcome = index_sync.IndexComponentOutcome(
                "watcher", "completed", "self_write_registered"
            )
        try:
            restored_states = {
                path.relative_to(vault_root).as_posix(): semantic_states[
                    path.relative_to(vault_root).as_posix()
                ]
                for path in restored_markdown
                if path.relative_to(vault_root).as_posix() in semantic_states
            }
            if restored_states:
                report = index_sync.upsert_after_write(
                    vault_root,
                    restored_markdown,
                    semantic_states=restored_states,
                )
            else:
                report = index_sync.upsert_after_write(vault_root, restored_markdown)
        except Exception:  # noqa: BLE001 - restore remains authoritative
            log.exception("restored index refresh failed for %s", restore_rel)
            try:
                graph_sync.register_outer_fanout_failure(vault_root)
            except Exception:  # noqa: BLE001 - retain the canonical recovery and report reconcile
                log.exception("graph fanout failure handoff could not be registered")
            warnings.append(
                "recovery succeeded but derived-index refresh failed; run reconcile"
            )
            report = index_sync.failed_upsert_report(
                vault_root,
                restored_markdown,
                watcher=watcher_outcome,
            )
        else:
            fanout_unverified = not isinstance(report, index_sync.IndexSyncReport)
            report = index_sync.with_component(
                report
                if isinstance(report, index_sync.IndexSyncReport)
                else index_sync.unverified_upsert_report(
                    vault_root, restored_markdown
                ),
                watcher_outcome,
            )
        index_feedback = report.as_dict()
        if fanout_unverified:
            index_feedback["derived_work"] = "unverified"

        from .writer_lease import active_mutation_request_id

        if active_mutation_request_id() is None:
            required = graph_sync.registered_checkpoint(vault_root)
            if required is not None:
                try:
                    if not graph_sync.join_registered_within_budget(vault_root):
                        warnings.append(
                            "recovery succeeded; derived graph repair is still running "
                            "in the background"
                        )
                except Exception:  # noqa: BLE001 - leave recovery evidence staged
                    warnings.append("recovery succeeded but graph publication failed; run reconcile")
    elif lifecycle_operation is not None:
        index_feedback = lifecycle.exact_no_derived_index_report(lifecycle_operation)

    if lifecycle_operation is not None and not lifecycle.finish_recovery(
        lifecycle_operation, index_report=index_feedback
    ):
        warnings.append(
            "recovery staged but governed derived state is not exact; "
            "content remains tombstoned until reconcile"
        )

    log_warning = log_plan.warning
    if not catalog_published:
        log_warning = write_log_entry(
            vault_root,
            date_iso=date_iso,
            op="recover_from_trash",
            rel_path_no_ext=restore_no_ext,
            body=log_body,
        )
    if log_warning:
        warnings.append(log_warning)

    from .writer_lease import active_mutation_request_id

    if active_mutation_request_id() is None:
        required = graph_sync.registered_checkpoint(vault_root)
        if required is not None:
            try:
                if not graph_sync.join_registered_within_budget(vault_root):
                    warnings.append(
                        "recovery log committed; derived graph repair is still running "
                        "in the background"
                    )
            except Exception:  # noqa: BLE001 - log publication remains recoverable
                warnings.append("recovery log graph publication failed; run reconcile")

    return RecoverResult(
        trash_path=trash_rel,
        restored_path=restore_rel,
        kind=trash_kind,
        warnings=warnings,
        semantic=semantic,
        index=index_feedback,
    )


def _refuse_outside_trash(trash_rel: str) -> None:
    """Refuse a path that is not a trash entry: recovery moves out of the trash only."""
    parts = trash_rel.split("/")
    in_trash = (
        len(parts) >= 2 and parts[0] == kb_dirname() and parts[1] == TRASH_SUBPATH
    )
    if not in_trash:
        raise RecoverError(
            code="NOT_IN_TRASH",
            reason=(
                f"{trash_rel} is not under {kb_prefix()}{TRASH_SUBPATH}/. "
                f"Use `move_file` for general relocations."
            ),
        )


def _refuse_reference_companion(data: bytes) -> None:
    """Refuse a trashed page that only refers to its original: the original is the entry to pick."""
    try:
        fields, body, _marker = parse_frontmatter(data.decode("utf-8"), custody=True)
    except (UnicodeError, ValueError):
        return  # Ordinary page recovery retains its existing validation boundary.
    if originals.is_reference_only_companion(fields, body):
        raise RecoverError("TRASH_REFERENCE_METADATA", "Select the original Trash entry to recover its bytes and retention metadata")


def _read_sidecar(
    vault_root: Path, trash_abs: Path
) -> tuple[Path, dict, PathGuard, PathGuard | None, str | None, object]:
    """The trash entry's placement sidecar, its parsed fields and the guard on its bytes or absence.

    The sidecar is a file whose bytes stay on disk in both modes, so both read it here.
    """
    sidecar = trash_abs.parent / f"{trash_abs.name}{METADATA_SUFFIX}"
    meta: dict = {}
    sidecar_guard: PathGuard
    sidecar_proof_guard: PathGuard | None = None
    sidecar_source: str | None = None
    sidecar_identity = None
    sidecar_rel = sidecar.relative_to(vault_root).as_posix()
    try:
        sidecar_snapshot = reserved_paths.read_generic_bytes(vault_root, sidecar_rel)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            try:
                sidecar_guard = PathGuard.capture(
                    vault_root, sidecar_rel, leaf_policy="absent"
                )
            except PathGuardError as guard_error:
                raise RecoverError(
                    code="TRASH_SIDECAR_INVALID",
                    reason="trash sidecar absence could not be bound safely",
                ) from guard_error
        elif error.code in {
            "CAPABILITY_UNAVAILABLE",
            "IDENTITY_CHANGED",
            "RESERVED_PATH",
            "UNSAFE_PATH",
        }:
            raise RecoverError(
                code="RESERVED_PATH",
                reason="trash sidecar is unavailable through the generic boundary",
            ) from None
        else:
            raise RecoverError(
                code="TRASH_SIDECAR_INVALID",
                reason="trash sidecar could not be read safely",
            ) from None
    else:
        try:
            sidecar_identity = sidecar_snapshot.identity
            sidecar_source = sidecar_snapshot.data.decode("utf-8")
            meta = relation_review.parse_exact_json_object(sidecar_source)
            sidecar_guard = PathGuard.capture(
                vault_root,
                sidecar_rel,
                leaf_policy="content",
                expected_content_hash=content_hash(sidecar_source),
                expected_content_size=len(sidecar_snapshot.data),
            )
            sidecar_proof_guard = sidecar_guard
        except (UnicodeDecodeError, ValueError, PathGuardError) as error:
            raise RecoverError(
                code="TRASH_SIDECAR_INVALID",
                reason="trash sidecar must be one exact strict UTF-8 JSON object",
            ) from error
    return sidecar, meta, sidecar_guard, sidecar_proof_guard, sidecar_source, sidecar_identity


def _restore_destination(
    vault_root: Path,
    *,
    restore_path: str | None,
    meta: dict,
    sidecar_name: str,
    allow_curated: bool,
    fold: bool,
) -> tuple[Path, str, str | None]:
    """Where a recovery puts the entry, with the curated tree it lands in, after every path rule."""
    if restore_path is None or not str(restore_path).strip():
        original = meta.get("original_path")
        if not original:
            raise RecoverError(
                code="NO_RESTORE_PATH",
                reason=(
                    f"no `restore_path` provided and the sidecar at "
                    f"{sidecar_name!r} doesn't carry an original_path. "
                    f"Supply `restore_path` explicitly."
                ),
            )
        restore_path = original

    try:
        restore_abs, restore_rel = resolve_under_vault(vault_root, restore_path)
    except VaultPathError as e:
        raise RecoverError(code=e.code, reason=e.reason) from e

    if reserved_paths.classify_logical(restore_rel).blocked:
        raise RecoverError(
            code="RESERVED_PATH",
            reason="restore destination is reserved for its owning subsystem",
        )

    try:
        lifecycle.assert_not_protected(vault_root, restore_rel)
    except lifecycle.LifecycleError as error:
        raise RecoverError(code=error.code, reason=error.reason) from error

    # The destination must not be inside the trash (recovery, not re-trashing).
    rparts = restore_rel.split("/")
    if len(rparts) >= 2 and rparts[0] == kb_dirname() and rparts[1] == TRASH_SUBPATH:
        raise RecoverError(
            code="RESTORE_INTO_TRASH",
            reason=(
                f"restore_path {restore_rel!r} is in _trash/. Recovery moves "
                f"OUT of trash; use `move_file` for trash-to-trash moves."
            ),
        )

    # Append-only / curated guards on the restore destination.
    append_only = in_append_only_tree(restore_rel)
    # Only the undo of a fold restores into Sources: it puts back the exact bytes the fold trashed.
    if append_only and not fold:
        raise RecoverError(
            code="APPEND_ONLY",
            reason=(
                f"restore_path {restore_rel!r} is in {append_only}/ which is "
                f"append-only. Sources/Evidence can't receive recovered files."
            ),
        )
    curated = in_curated_tree(restore_rel)
    if curated and not allow_curated:
        raise RecoverError(
            code="CURATED_PROTECTED",
            reason=(
                f"restore_path {restore_rel!r} is in curated tree "
                f"{curated!r}. Pass `allow_curated=true` to override."
            ),
        )
    return restore_abs, restore_rel, curated


def _destination_exists(restore_rel: str) -> RecoverError:
    return RecoverError(
        code="DEST_EXISTS",
        reason=(
            f"destination {restore_rel!r} already exists. Choose a "
            f"different restore_path, or move the existing file out of "
            f"the way first."
        ),
    )


def _log_body(trash_rel: str, restore_rel: str, trash_kind: str, curated: str | None) -> str:
    """The activity log's sentence for a recovery; ``curated`` is the tree an override let it into."""
    log_body = (
        f"Recovered {trash_rel!r} → {restore_rel!r} via exomem Tier 2. "
        f"kind={trash_kind}."
    )
    if curated:
        log_body += f" allow_curated=true (target tree: {curated})."
    return log_body


def _settle_sidecar(
    vault_root: Path, sidecar: Path, sidecar_guard: PathGuard, sidecar_identity: object
) -> list[str]:
    """Remove the recovered entry's sidecar when it is still the one read, or keep it and warn."""
    sidecar_rel = sidecar.relative_to(vault_root).as_posix()
    if sidecar_guard.leaf_policy == "content":
        try:
            sidecar_guard.recheck(vault_root)
            reserved_paths.unlink_generic_file(
                vault_root,
                sidecar_rel,
                expected_identity=sidecar_identity,
            )
        except (PathGuardError, reserved_paths.ReservedPathLeafError) as e:
            return [
                f"recovered file ok but trash sidecar changed or could not be "
                f"removed safely: {sidecar.name!r}: {e}"
            ]
    else:
        try:
            sidecar_guard.recheck(vault_root)
        except PathGuardError as e:
            return [
                f"recovered file ok but absent trash sidecar changed; retained "
                f"the new path occupant {sidecar.name!r}: {e}"
            ]
    return []


def _recover_in_store(
    vault_root: Path,
    *,
    trash_path: str,
    restore_path: str | None,
    allow_curated: bool,
    today: dt.date | None,
    validate_only: bool,
    relation_reviews: Mapping[str, Mapping[str, str]] | None,
    fold: bool,
) -> RecoverResult:
    """Recover a trashed page, or a trashed folder of pages, in one store transaction.

    The store moves each page back with its rows and drops its record of the sidecar; the projector
    moves the files afterwards. The semantic preflight and commit are file mode's, judged against
    the stored pages. A trashed file whose bytes stay on disk, a folder that holds one, and a page
    that carries its original or its part pages still refuse by name: they have no store form yet.
    """
    from .vault_store import availability as store_availability
    from .vault_store import batch as store_batch
    from .vault_store import read as store_read
    from .vault_store.write import BinaryDelete, Expect, PageMove, StoreConflict

    root = Path(vault_root)
    writer = store_availability.page_writer(root)
    with ExitStack() as reading:
        # The read phase sees one revision; the commit's expectations recheck all that it read.
        reading.enter_context(store_read.request_snapshot(root))
        try:
            trash_abs, trash_rel = resolve_under_vault(root, trash_path)
        except VaultPathError as e:
            raise RecoverError(code=e.code, reason=e.reason) from e
        _refuse_outside_trash(trash_rel)
        with store_read.snapshot(root) as view:
            below = [
                meta.path
                for meta in itertools.islice(
                    view.pages(f"{trash_rel}/"), DIRECTORY_TRANSITION_LIMIT + 1
                )
            ]
            if len(below) > DIRECTORY_TRANSITION_LIMIT:
                raise RecoverError(
                    code="PATH_GUARD_LIMIT",
                    reason="trash directory exceeds the bounded recovery census",
                )
            stored = view.bodies(below)
        if below:
            trash_kind = "directory"
        elif (root / trash_rel).is_dir():
            raise store_availability.unrouted(
                "recover_from_trash",
                "the store holds no page below this folder, and a folder of files whose bytes "
                "stay on disk has no store form",
            )
        else:
            try:
                trash_abs, trash_rel = store_targets.resolve_existing(root, trash_path)
            except VaultPathError as e:
                raise RecoverError(code=e.code, reason=e.reason) from e
            trash_kind = "file"
            data = store_read.owned_bytes(root, trash_rel) if store_read.serves(root, trash_rel) else None
            stored = {} if data is None else {trash_rel: data}
            if data is not None and trash_rel.lower().endswith(".md"):
                _refuse_reference_companion(data)

        sidecar, meta, sidecar_guard, sidecar_proof_guard, sidecar_source, _identity = (
            _read_sidecar(root, trash_abs)
        )
        sidecar_rel = sidecar.relative_to(root).as_posix()
        restore_abs, restore_rel, curated = _restore_destination(
            root,
            restore_path=restore_path,
            meta=meta,
            sidecar_name=sidecar.name,
            allow_curated=allow_curated,
            fold=fold,
        )
        # Each trashed page and where it goes back to; a lone page is a folder of one.
        moves = {
            source: (
                restore_rel
                if trash_kind == "file"
                else f"{restore_rel.rstrip('/')}/{source[len(trash_rel) + 1:]}"
            )
            for source in stored
        }
        from .collection_store import authority as collection_authority

        # Decided before the existence check below, so it cannot tell a withheld view from none.
        collection_authority.refuse_view_paths(
            root, (restore_rel, *moves.values()), RecoverError
        )
        # Before the preflight: the commit refuses such a path after the slots have committed.
        store_targets.refuse_crossing("recover_from_trash", moves.values())
        if _restore_taken(root, trash_kind, restore_rel):
            raise _destination_exists(restore_rel)

        frontmatter: dict[str, dict] = {}
        for source, body in stored.items():
            try:
                frontmatter[source] = parse_frontmatter(body.decode("utf-8"))[0]
            except (UnicodeDecodeError, FrontmatterError):
                frontmatter[source] = {}
        refusal = (
            store_targets.folder_form_gap(root, trash_rel, frontmatter)
            if trash_kind == "directory"
            else store_targets.page_only_form_gap(
                root, trash_rel, frontmatter.get(trash_rel, {}), destination=restore_rel
            )
        )
        if refusal is not None:
            raise store_availability.unrouted("recover_from_trash", refusal)
        store_targets.active_catalog_gap(
            root,
            "recover_from_trash",
            error_class=RecoverError,
            content_paths=tuple(sorted(moves.values())),
        )

        entries: list[semantic_writes.RecoveryEntry] = []
        for source, body in stored.items():
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as error:
                raise RecoverError(code="RECOVER_FAILED", reason=str(error)) from error
            entries.append(
                semantic_writes.RecoveryEntry(
                    source,
                    trash_original_path(
                        meta, trash_root=trash_rel, current_path=source, fallback_root=restore_rel
                    ),
                    moves[source],
                    text,
                    PathGuard.of_stored(source, content_hash(text)),
                    PathGuard.of_stored(moves[source], None),
                    sidecar_proof_guard,
                    sidecar_source,
                )
            )
        with store_read.snapshot(root) as view:
            # The store drops its record of the sidecar, and the projector removes the file.
            drop_sidecar = (
                sidecar_guard.leaf_policy == "content" and view.binary(sidecar_rel) is not None
            )
        try:
            preflight = semantic_writes.preflight_recovery(
                root,
                entries=entries,
                # A folder's guard binds that no page sits at its own path; `_restore_taken` judged
                # the pages below it.
                destination_root_guard=PathGuard.of_stored(restore_rel, None),
                recovery_sidecar_guard=sidecar_guard,
                catalog_content_paths=tuple(sorted(moves.values())),
                catalog_publication_now=int(time.time()),
                relation_reviews=relation_reviews,
            )
        except semantic_writes.SemanticWriteError as error:
            raise RecoverError(code=error.code, reason=error.reason) from error
        except PathGuardError as error:
            raise RecoverError(code=error.code, reason=error.reason) from error
        if validate_only:
            return RecoverResult(
                trash_path=trash_rel,
                restored_path=restore_rel,
                kind=trash_kind,
                warnings=[],
                semantic=preflight.as_dict(),
            )

    def restore_in_store() -> None:
        restoring = store_batch.Lifecycle(
            moves=tuple(PageMove(source, moves[source]) for source in stored),
            binary_deletes=(BinaryDelete(sidecar_rel),) if drop_sidecar else (),
            expect={
                **{
                    entry.trash_path: Expect(sha256=entry.source_guard.expected_content_hash)
                    for entry in entries
                },
                **{destination: Expect(absent=True) for destination in moves.values()},
            },
        )
        try:
            store_batch.commit_batch(writer, [], vault_root=root, lifecycle=restoring)
        except StoreConflict as conflict:
            if conflict.path in moves:
                raise RecoverError(
                    code="PATH_GUARD_CHANGED",
                    reason="trash contents changed before the recovery transition",
                ) from conflict
            if conflict.path in moves.values():
                raise _destination_exists(restore_rel) from conflict
            raise RecoverError(
                code="RECOVER_FAILED", reason="trash recovery could not be published"
            ) from conflict

    try:
        committed = semantic_writes.commit_recovery(
            root, preflight=preflight, mutate=restore_in_store
        )
    except semantic_writes.SemanticWriteError as error:
        raise RecoverError(code=error.code, reason=error.reason) from error
    except PathGuardError as error:
        raise RecoverError(code=error.code, reason=error.reason) from error

    warnings: list[str] = []
    if not drop_sidecar:
        # A sidecar that the store does not record is a file of its own; file mode's step removes it.
        warnings.extend(_settle_sidecar(root, sidecar, sidecar_guard, _identity))
    log_warning = write_log_entry(
        root,
        date_iso=(today or dt.date.today()).isoformat(),
        op="recover_from_trash",
        rel_path_no_ext=restore_rel.removesuffix(".md"),
        body=_log_body(trash_rel, restore_rel, trash_kind, curated if allow_curated else None),
    )
    if log_warning:
        warnings.append(log_warning)
    return RecoverResult(
        trash_path=trash_rel,
        restored_path=restore_rel,
        kind=trash_kind,
        warnings=warnings,
        semantic=committed.as_dict(),
        index=store_batch.row_report("upsert", list(moves.values())).as_dict(),
    )


def _restore_taken(vault_root: Path, trash_kind: str, restore_rel: str) -> bool:
    """Whether a store-mode recovery's destination is taken, as the store sees it.

    A page's destination is taken by a stored page or a binary there. A folder's is taken by a
    binary at its path or a page or binary below it; a folder on disk that holds neither is the
    projection's leftover, which the projector leaves behind when it moves the last page out.
    """
    from .vault_store import read as store_read

    if trash_kind == "file":
        return store_targets.holds(vault_root, restore_rel)
    with store_read.snapshot(vault_root) as view:
        return (
            view.binary(restore_rel) is not None
            or (Path(vault_root) / restore_rel).is_file()
            or next(view.children(restore_rel), None) is not None
            or view.holds_binaries_under(f"{restore_rel}/")
        )
