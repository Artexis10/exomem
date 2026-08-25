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
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import (
    graph_sync,
    relation_review,
    reserved_paths,
    semantic_index,
    semantic_writes,
)
from .governance import catalog_publication, lifecycle
from .kbdir import kb_dirname, kb_prefix
from .vault import (
    DirectoryCensusGuard,
    PathGuard,
    PathGuardError,
    VaultPathError,
    batch_atomic_write,
    content_hash,
    in_append_only_tree,
    in_curated_tree,
    plan_log_entry,
    read_guarded_text,
    resolve_under_vault,
    write_log_entry,
)

log = logging.getLogger(__name__)

TRASH_SUBPATH = "_trash"


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
) -> RecoverResult:
    try:
        trash_abs, trash_rel = resolve_under_vault(
            vault_root, trash_path, must_exist=True
        )
    except VaultPathError as e:
        raise RecoverError(code=e.code, reason=e.reason) from e

    # Must actually be a trash entry.
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

    # Determine restore_path: explicit > sidecar's original_path.
    sidecar = trash_abs.parent / f"{trash_abs.name}.meta.json"
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

    if restore_path is None or not str(restore_path).strip():
        original = meta.get("original_path")
        if not original:
            raise RecoverError(
                code="NO_RESTORE_PATH",
                reason=(
                    f"no `restore_path` provided and the sidecar at "
                    f"{sidecar.name!r} doesn't carry an original_path. "
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
    if append_only:
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
        raise RecoverError(
            code="DEST_EXISTS",
            reason=(
                f"destination {restore_rel!r} already exists. Choose a "
                f"different restore_path, or move the existing file out of "
                f"the way first."
            ),
        )

    if trash_kind == "file":
        assert trash_file_snapshot is not None
        initial_restore_state = (
            (restore_rel, hashlib.sha256(trash_file_snapshot.data).hexdigest()),
        )
    else:
        initial_restore_state = tuple(
            sorted(
                (
                    f"{restore_rel.rstrip('/')}/{item.relative_path}",
                    hashlib.sha256(item.snapshot.data).hexdigest(),
                )
                for item in trash_tree
            )
        )
    catalog_content_paths = tuple(path for path, _digest in initial_restore_state)

    today = today or dt.date.today()
    date_iso = today.isoformat()
    restore_no_ext = (
        restore_rel.removesuffix(".md") if restore_rel.endswith(".md") else restore_rel
    )
    log_body = (
        f"Recovered {trash_rel!r} → {restore_rel!r} via exomem Tier 2. "
        f"kind={trash_kind}."
    )
    if curated and allow_curated:
        log_body += f" allow_curated=true (target tree: {curated})."
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
                    str(meta.get("original_path") or restore_rel),
                    restore_rel,
                    source,
                    source_guard,
                    destination_guard,
                    sidecar_proof_guard,
                    sidecar_source,
                )
            )
        except (OSError, UnicodeDecodeError, PathGuardError) as error:
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
                if len(directories) > 4096:
                    raise RecoverError(
                        code="PATH_GUARD_LIMIT",
                        reason="trash directory exceeds the bounded recovery census",
                    )
                trash_census_guards = tuple(
                    DirectoryCensusGuard.capture(
                        vault_root,
                        path.relative_to(vault_root).as_posix(),
                        max_entries=4096,
                    )
                    for path in sorted(directories, key=lambda item: item.as_posix())
                )
                original_root = str(meta.get("original_path") or restore_rel).rstrip(
                    "/"
                )
                for markdown_path in markdown:
                    suffix = markdown_path.relative_to(trash_abs).as_posix()
                    source_path = f"{trash_rel}/{suffix}"
                    original_path = f"{original_root}/{suffix}"
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
        except (OSError, UnicodeDecodeError, PathGuardError) as error:
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
                        (entry.restore_path, content_hash(entry.source))
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
    if sidecar_guard.leaf_policy == "content":
        try:
            sidecar_guard.recheck(vault_root)
            reserved_paths.unlink_generic_file(
                vault_root,
                sidecar_rel,
                expected_identity=sidecar_identity,
            )
        except (PathGuardError, reserved_paths.ReservedPathLeafError) as e:
            warnings.append(
                f"recovered file ok but trash sidecar changed or could not be "
                f"removed safely: {sidecar.name!r}: {e}"
            )
    else:
        try:
            sidecar_guard.recheck(vault_root)
        except PathGuardError as e:
            warnings.append(
                f"recovered file ok but absent trash sidecar changed; retained "
                f"the new path occupant {sidecar.name!r}: {e}"
            )

    index_feedback: dict | None = None
    restored_markdown = (
        sorted(restore_abs.rglob("*.md"))
        if restore_abs.is_dir()
        else ([restore_abs] if restore_abs.suffix.lower() == ".md" else [])
    )
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
                    graph_sync.wait_for_registered(vault_root)
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
                graph_sync.wait_for_registered(vault_root)
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
