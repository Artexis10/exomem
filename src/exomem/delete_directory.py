"""The `delete_directory` Tier 2 op: trash a folder (whole tree).

Symmetric with `create_directory`. Like `delete_file`, this NEVER does
a permanent delete: the folder is moved to
`Knowledge Base/_trash/YYYY-MM-DD/HHMMSS-<sanitized-original-path>/`
with a `.meta.json` sidecar capturing what was trashed and why.

Refuses Sources/ and Evidence/ outright (append-only). Refuses non-
empty directories unless `recursive=true` is passed (acknowledges
"yes I know it has stuff in it"). Curated trees need `allow_curated=true`.

Scans for inbound wikilinks pointing to any .md file inside the
directory tree; refuses if any exist unless `force_orphan=true`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import logging
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from . import held_fs, media_types, originals, reserved_paths, semantic_contract, trash_metadata
from .delete_file import planned_trash_path
from .governance import catalog_publication, graph_producer, lifecycle
from .kbdir import kb_dirname, kb_prefix
from .trash_metadata import METADATA_SUFFIX, TRASH_SUBPATH
from .vault import (
    DIRECTORY_TRANSITION_LIMIT,
    VaultPathError,
    batch_atomic_write,
    find_inbound_wikilinks,
    in_append_only_tree,
    in_curated_tree,
    missing_path_reason,
    plan_log_entry,
    resolve_under_vault,
    write_log_entry,
)
from .vault_store import targets as store_targets

log = logging.getLogger(__name__)



@dataclass
class DeleteDirectoryResult:
    path: str
    trash_path: str
    trash_meta_path: str
    file_count: int
    inbound_link_count: int
    warnings: list[str]
    index: dict | None = None

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "trash_path": self.trash_path,
            "trash_meta_path": self.trash_meta_path,
            "file_count": self.file_count,
            "inbound_link_count": self.inbound_link_count,
            "warnings": self.warnings,
            "index": self.index,
        }


@dataclass
class DeleteDirectoryError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def _read_tree(vault_root: Path, rel_path: str) -> tuple[reserved_paths.GenericTreeFile, ...]:
    try:
        return reserved_paths.read_generic_tree(vault_root, rel_path)
    except reserved_paths.ReservedPathLeafError as error:
        code = "NOT_FOUND" if error.code == "MISSING" else "RESERVED_PATH"
        raise DeleteDirectoryError(code=code, reason="directory could not be acquired through the generic boundary") from None


def _refuse_protected(vault_root: Path, rel_path: str, *, allow_curated: bool) -> str | None:
    """Refuse a folder that no delete may trash; return its curated tree, if any."""
    try:
        lifecycle.assert_not_protected(vault_root, rel_path)
    except lifecycle.LifecycleError as error:
        raise DeleteDirectoryError(code=error.code, reason=error.reason) from error

    # Don't allow deleting the trash itself, or trash subdirs.
    parts = rel_path.split("/")
    if len(parts) >= 2 and parts[0] == kb_dirname() and parts[1] == TRASH_SUBPATH:
        raise DeleteDirectoryError(
            code="ALREADY_TRASHED",
            reason=(
                f"{rel_path} is already in {TRASH_SUBPATH}/. To clean it, "
                f"do it filesystem-side."
            ),
        )

    # Refuse anywhere in Sources/Evidence — those trees are append-only at
    # any granularity (whole-folder delete would violate rule 2 wholesale).
    append_only = in_append_only_tree(rel_path)
    if append_only:
        raise DeleteDirectoryError(
            code="APPEND_ONLY",
            reason=(
                f"{rel_path} is in {append_only}/ which is append-only "
                f"(SKILL.md rule 2)."
            ),
        )

    curated = in_curated_tree(rel_path)
    if curated and not allow_curated:
        raise DeleteDirectoryError(
            code="CURATED_PROTECTED",
            reason=(
                f"{rel_path} is in curated tree {curated!r} (desk-managed). "
                f"Pass `allow_curated=true` only if you really mean it."
            ),
        )
    return curated


def _not_empty(rel_path: str, count: int) -> DeleteDirectoryError:
    return DeleteDirectoryError(
        code="NOT_EMPTY",
        reason=(
            f"{rel_path} contains {count} file(s). Pass "
            f"`recursive=true` to trash the whole tree."
        ),
    )


def _refuse_orphaning(
    rel_path: str, inbound_total: int, inbound_samples: list[str], force_orphan: bool
) -> list[str]:
    """Refuse a trash that would orphan links from outside the folder; return its warnings."""
    if inbound_total > 0 and not force_orphan:
        sample = "; ".join(inbound_samples[:3])
        more = (
            f" (+{inbound_total - 3} more)" if inbound_total > 3 else ""
        )
        raise DeleteDirectoryError(
            code="INBOUND_LINKS",
            reason=(
                f"{rel_path} contains files with {inbound_total} external "
                f"inbound wikilink(s): {sample}{more}. Trashing would orphan "
                f"those. Pass `force_orphan=true` to override."
            ),
        )

    warnings: list[str] = []
    if force_orphan and inbound_total > 0:
        warnings.append(
            f"force_orphan=true: trashed with {inbound_total} external "
            f"inbound link(s) into the tree. Run `audit` to surface the "
            f"new broken links."
        )
    return warnings


def _disk_slot_taken(vault_root: Path, candidate: Path) -> bool:
    relative = candidate.relative_to(vault_root).as_posix()
    try:
        reserved_paths.inspect_generic_path(vault_root, relative)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            return False
        if error.code in {
            "CAPABILITY_UNAVAILABLE",
            "IDENTITY_CHANGED",
            "RESERVED_PATH",
            "UNSAFE_PATH",
        }:
            raise DeleteDirectoryError(
                "RESERVED_PATH",
                "trash destination is reserved for its owning subsystem",
            ) from None
        raise DeleteDirectoryError(
            "DELETE_FAILED", "trash destination could not be acquired safely"
        ) from None
    return True


def _free_trash_folder(planned: Path, taken: Callable[[Path], bool]) -> Path:
    """The first of `planned`, `name-2`, `name-3`... whose payload, metadata and companion slots
    `taken` all finds free."""
    candidate, counter = planned, 2
    while any(taken(slot) for slot in trash_metadata.destination_paths(candidate)):
        candidate = planned.parent / f"{planned.name}-{counter}"
        counter += 1
    return candidate


def _log_body(
    rel_path: str,
    trash_rel: str,
    *,
    files: int,
    md_files: int,
    inbound: int,
    recursive: bool,
    force_orphan: bool,
    curated: str | None,
) -> str:
    """The log entry of a folder trash; ``curated`` is the curated tree it was allowed in."""
    log_body = (
        f"Trashed directory {rel_path!r} → {trash_rel!r} via exomem Tier 2. "
        f"files={files}, md_files={md_files}, "
        f"inbound_links_at_trash={inbound}."
    )
    if recursive:
        log_body += " recursive=true."
    if force_orphan:
        log_body += " force_orphan=true."
    if curated:
        log_body += f" allow_curated=true (target tree: {curated})."
    return log_body


def _trash_metadata(
    rel_path: str,
    now: dt.datetime,
    *,
    files: int,
    md_files: int,
    inbound: int,
    recursive: bool,
    force_orphan: bool,
    curated: bool,
) -> bytes:
    """The sidecar of a folder trash; ``curated`` says a curated tree was allowed."""
    meta = {
        "original_path": rel_path,
        "trashed_at": now.isoformat(),
        "deleted_by": "exomem",
        "file_count_at_trash": files,
        "md_file_count_at_trash": md_files,
        "inbound_link_count_at_trash": inbound,
        "force_orphan_used": bool(force_orphan and inbound > 0),
        "allow_curated_used": curated,
        "recursive_used": bool(recursive),
    }
    return json.dumps(meta, indent=2, default=str).encode("utf-8")


def delete_directory(
    vault_root: Path,
    *,
    path: str,
    confirm: bool,
    recursive: bool = False,
    force_orphan: bool = False,
    allow_curated: bool = False,
    today: dt.date | None = None,
    now: dt.datetime | None = None,
) -> DeleteDirectoryResult:
    with ExitStack() as originals_context:
        return _delete_directory_local(vault_root, path=path, confirm=confirm, recursive=recursive, force_orphan=force_orphan, allow_curated=allow_curated, today=today, now=now, originals_context=originals_context)


def _delete_directory_local(
    vault_root: Path,
    *,
    path: str,
    confirm: bool,
    recursive: bool = False,
    force_orphan: bool = False,
    allow_curated: bool = False,
    today: dt.date | None = None,
    now: dt.datetime | None = None,
    originals_context: ExitStack,
) -> DeleteDirectoryResult:
    if not confirm:
        raise DeleteDirectoryError(
            code="UNCONFIRMED",
            reason=(
                "delete_directory requires `confirm=true` explicitly. "
                f"Deletes go to {kb_prefix()}_trash/ (recoverable) but the "
                "action is still deliberate."
            ),
        )

    # A folder that the store owns holds stored pages, which its store form trashes in one commit.
    if store_targets.owns_any(vault_root, folders=(path,)):
        return _delete_in_store(
            vault_root,
            path=path,
            recursive=recursive,
            force_orphan=force_orphan,
            allow_curated=allow_curated,
            today=today,
            now=now,
        )
    try:
        abs_path, rel_path = resolve_under_vault(
            vault_root, path, must_exist=True, must_be_dir=True
        )
    except VaultPathError as e:
        raise DeleteDirectoryError(code=e.code, reason=e.reason) from e

    curated = _refuse_protected(vault_root, rel_path, allow_curated=allow_curated)

    # Acquire and classify every child before counts, link scans, markers, or
    # lifecycle planning.  The later lifecycle snapshot rechecks the same
    # content immediately before its retained directory rename.
    tree = _read_tree(vault_root, rel_path)
    from .media_store import bound_originals, local_originals

    evicted = (tuple(item for item in bound_originals(vault_root, root=abs_path) if item.state == "evicted")
               if originals.binding_for(vault_root) is not None else ())
    files = sorted({*(abs_path / item.relative_path for item in tree), *(item.path for item in evicted)})
    from .collection_store import authority as collection_authority

    collection_authority.refuse_view_paths(
        vault_root,
        (rel_path, *(item.relative_to(vault_root).as_posix() for item in files)),
        DeleteDirectoryError,
    )
    if files and not recursive:
        raise _not_empty(rel_path, len(files))

    # Inbound-wikilink scan: any .md file inside the tree could have
    # external referents. Aggregate the count across all .md children.
    md_files = sorted(
        (p for p in files if p.suffix.lower() == ".md"),
        key=lambda item: item.as_posix(),
    )
    inbound_total = 0
    inbound_samples: list[str] = []
    for md in md_files:
        try:
            md_rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
        except ValueError:
            continue

        # Skip inbound links from inside the doomed tree itself.
        hits = [
            h for h in find_inbound_wikilinks(vault_root, md_rel)
            if not h.path.startswith(rel_path + "/")
        ]
        inbound_total += len(hits)
        for h in hits[:2]:
            inbound_samples.append(f"{h.path}:{h.line_number} → {md_rel}")

    warnings = _refuse_orphaning(rel_path, inbound_total, inbound_samples, force_orphan)

    # Move the whole tree into trash.
    now = now or dt.datetime.now()
    today = today or now.date()
    trash_abs = _free_trash_folder(
        vault_root / planned_trash_path(vault_root, rel_path, now),
        lambda slot: _disk_slot_taken(vault_root, slot),
    )
    trash_root = trash_abs.parent

    trash_rel = trash_abs.relative_to(vault_root).as_posix()
    meta_abs = trash_root / f"{trash_abs.name}{METADATA_SUFFIX}"
    meta_rel = meta_abs.relative_to(vault_root).as_posix()

    initial_tree_state = tuple(sorted(
        [(f"{rel_path.rstrip('/')}/{item.relative_path}", hashlib.sha256(item.snapshot.data).hexdigest()) for item in tree]
        + [(item.path.relative_to(vault_root).as_posix(), item.binary_sha256) for item in evicted]
    ))
    # Capture the affected Markdown paths before the tree moves.
    md_rels_to_unindex: list[str] = []
    for md in md_files:
        try:
            md_rels_to_unindex.append(
                md.resolve().relative_to(vault_root.resolve()).as_posix()
            )
        except ValueError:
            continue

    today_iso = today.isoformat()
    log_body = _log_body(
        rel_path,
        trash_rel,
        files=len(files),
        md_files=len(md_files),
        inbound=inbound_total,
        recursive=recursive,
        force_orphan=force_orphan,
        curated=curated if allow_curated else None,
    )
    log_plan = plan_log_entry(
        vault_root,
        date_iso=today_iso,
        op="delete_directory (trash)",
        rel_path_no_ext=rel_path,
        body=log_body,
    )
    catalog_removals = tuple(
        catalog_publication.CatalogRemoval(
            path,
            digest if path.lower().endswith(".md") else None,
        )
        for path, digest in initial_tree_state
    )
    catalog_target: catalog_publication.PreparedMarkdownCatalogPublication | None = None
    if log_plan.writes or catalog_removals:
        def graph_replacement_provider():
            return graph_producer.replacements_for_removed_markdown(
                vault_root,
                before_corpus=semantic_contract.build_corpus_context(vault_root),
                removed_paths=tuple(md_rels_to_unindex),
                writes=log_plan.writes,
            )

        try:
            catalog_target = catalog_publication.prepare_catalog_membership_batch(
                vault_root,
                writes=log_plan.writes,
                removals=catalog_removals,
                graph_replacement_provider=(
                    graph_replacement_provider if md_rels_to_unindex else None
                ),
                now=int(time.time()),
            )
        except catalog_publication.CatalogPublicationError as error:
            raise DeleteDirectoryError(
                code="GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
                reason=str(error),
            ) from error

    originals_context.enter_context(local_originals(vault_root, (rel_path,)))
    if evicted:
        tree = _read_tree(vault_root, rel_path)
        if tuple(sorted((f"{rel_path.rstrip('/')}/{item.relative_path}", hashlib.sha256(item.snapshot.data).hexdigest())
                        for item in tree)) != initial_tree_state:
            raise DeleteDirectoryError("PATH_GUARD_CHANGED", "directory contents changed before the trash transition")
    from . import graph_sync

    try:
        transition = graph_sync.begin_deletion_transition(
            vault_root,
            source_rel=rel_path,
            trash_rel=trash_rel,
            removed_rel_paths=md_rels_to_unindex,
        )
    except lifecycle.LifecycleError as error:
        raise DeleteDirectoryError(code=error.code, reason=error.reason) from error
    except graph_sync.GraphLifecycleEpochSetupError as error:
        raise DeleteDirectoryError(
            code="GRAPH_SYNC_EPOCH_FAILED",
            reason="could not establish the graph deletion epoch",
        ) from error
    except graph_sync.GraphLifecycleRollbackError as error:
        raise DeleteDirectoryError(
            code="GRAPH_SYNC_DELETION_ROLLBACK_FAILED",
            reason="the staged trash transition could not be restored",
        ) from error
    lifecycle_operation = transition.operation
    # The catalog successor was prepared from the first held tree. The
    # lifecycle boundary independently recaptures that tree before staging its
    # tombstone/graph epoch; require the two exact path/hash sets to agree so a
    # concurrent child cannot be moved to trash while retaining an active row.
    transition_tree_state = tuple(
        sorted(
            (item.source_path, item.content_hash)
            for item in lifecycle_operation.manifest
        )
    )
    if transition_tree_state != initial_tree_state:
        try:
            transition.abort()
        except graph_sync.GraphLifecycleRollbackError as rollback_error:
            raise DeleteDirectoryError(
                code="GRAPH_SYNC_DELETION_ROLLBACK_FAILED",
                reason="the changed directory transition could not be restored",
            ) from rollback_error
        raise DeleteDirectoryError(
            code="PATH_GUARD_CHANGED",
            reason="directory contents changed before the trash transition",
        )

    # A doomed subtree may also contain CLIP-relevant media binaries (image/
    # video — the only kinds with derived-index residue: CLIP rows, and for a
    # video, scene-frame children). Other extractable-but-not-visual kinds
    # (audio/pdf/docx/text/email/calendar) keep the prior .md-only behavior:
    # their OCR/ASR text lives in a separate `.md` sidecar, already covered
    # above. Unlike delete_file (single target, needs list_scene_frame_children
    # to reach siblings), the recursive walk here already visits `.frames/`
    # children itself: the frame `.jpg` lands in media_rels and its `.jpg.md`
    # sidecar in the markdown walk.
    media_rels: list[str] = []
    for p in files:
        if p.suffix.lower() == ".md":
            continue
        media_type = media_types.media_type_for(p)
        if media_type not in {"image", "video"}:
            continue
        try:
            rel = p.resolve().relative_to(vault_root.resolve()).as_posix()
        except ValueError:
            continue
        media_rels.append(rel)

    fanout_rels = list(dict.fromkeys(md_rels_to_unindex + media_rels))

    # Register the self-authored removals BEFORE the move so the watcher's
    # per-file delete events are dropped (sidecar purge happens below).
    # register_self_delete itself filters to `.md` paths, so the media
    # binaries' own (non-md) rels here are a harmless no-op; the frame `.md`
    # sidecars are what need suppressing.
    if fanout_rels:
        try:
            from . import file_watcher
            file_watcher.register_self_delete(vault_root, fanout_rels)
        except Exception:  # noqa: BLE001 — suppression is best-effort
            log.debug("self-delete suppression registration failed", exc_info=True)

    metadata_bytes = _trash_metadata(
        rel_path,
        now,
        files=len(files),
        md_files=len(md_files),
        inbound=inbound_total,
        recursive=recursive,
        force_orphan=force_orphan,
        curated=bool(curated and allow_curated),
    )
    # Retained directory companions need their historical placement even after a crash.
    placement_required = originals.binding_for(vault_root) is not None
    placement_files = [(meta_rel, metadata_bytes)] if placement_required else []
    try:
        with trash_metadata.preserve_placement(vault_root, transition, placement_files):
            try:
                transition.rename()
            except lifecycle.LifecycleError as e:
                try:
                    transition.abort()
                except graph_sync.GraphLifecycleRollbackError as rollback_error:
                    raise DeleteDirectoryError(
                        code="GRAPH_SYNC_DELETION_ROLLBACK_FAILED",
                        reason="the staged trash transition could not be restored",
                    ) from rollback_error
                raise DeleteDirectoryError(
                    code=e.code,
                    reason=e.reason,
                ) from e

            try:
                transition.publish_checkpoint()
            except Exception as error:  # noqa: BLE001 - reverse caught transition
                try:
                    transition.abort()
                except graph_sync.GraphLifecycleRollbackError as rollback_error:
                    raise DeleteDirectoryError(
                        code="GRAPH_SYNC_DELETION_ROLLBACK_FAILED",
                        reason="graph checkpoint failed and the trash transition could not be restored",
                    ) from rollback_error
                raise DeleteDirectoryError(
                    code="GRAPH_SYNC_CHECKPOINT_FAILED",
                    reason="graph checkpoint failed; the trash transition was restored",
                ) from error
    except graph_sync.GraphLifecycleRollbackError as error:
        raise DeleteDirectoryError("GRAPH_SYNC_DELETION_ROLLBACK_FAILED", "Trash placement could not be restored") from error
    except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError) as error:
        raise DeleteDirectoryError("DELETE_FAILED", "Trash placement could not be published") from error
    from .writer_lease import mark_active_mutation_committed

    mark_active_mutation_committed()

    from . import due_state

    # The pages as they stood, so the Sources they cited are re-judged once they are gone.
    trashed_pages: list[due_state.PageWrite] = []
    for item in tree:
        if not item.relative_path.lower().endswith(".md"):
            continue
        try:
            text = item.snapshot.data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        trashed_pages.append(
            due_state.PageWrite(f"{rel_path.rstrip('/')}/{item.relative_path}", prior_markdown=text)
        )
    due_state.refresh_after_writes(vault_root, trashed_pages)

    index_feedback: dict | None = None
    if fanout_rels:
        fanout_unverified = False
        try:
            from . import index_sync
            raw_report = index_sync.delete_after_remove(
                vault_root, fanout_rels
            )
            report = (
                raw_report
                if isinstance(raw_report, index_sync.IndexSyncReport)
                else index_sync.observed_delete_report(
                    fanout_rels, degraded=False
                )
            )
            fanout_unverified = not isinstance(raw_report, index_sync.IndexSyncReport)
        except Exception:  # noqa: BLE001 — sidecars are best-effort
            log.exception(
                "index delete failed for trashed tree %s; sidecar may be stale",
                rel_path,
            )
            try:
                graph_sync.register_outer_fanout_failure(vault_root)
            except Exception:  # noqa: BLE001 - retain the canonical deletion and report reconcile
                log.exception("graph fanout failure handoff could not be registered")
            warnings.append(
                "trash succeeded but derived-index cleanup failed; run reconcile"
            )
            report = index_sync.observed_delete_report(
                fanout_rels, degraded=True
            )
        index_feedback = report.as_dict()
        if fanout_unverified:
            index_feedback["derived_work"] = "unverified"
    else:
        index_feedback = lifecycle.exact_no_derived_index_report(lifecycle_operation)

    from .writer_lease import active_mutation_request_id

    if active_mutation_request_id() is None:
        from . import graph_sync

        if graph_sync.registered_checkpoint(vault_root) is not None:
            try:
                if not graph_sync.join_registered_within_budget(vault_root):
                    warnings.append(
                        "trash succeeded; derived graph repair is still running in the background"
                    )
            except Exception:  # noqa: BLE001 - preserve committed deletion for reconcile
                warnings.append("trash succeeded but graph publication failed; run reconcile")

    if not placement_required:
        try:
            reserved_paths.publish_generic_bytes(vault_root, meta_rel, metadata_bytes, expected_identity=None)
        except (OSError, reserved_paths.ReservedPathLeafError) as error:
            warnings.append(f"trashed dir ok but meta sidecar write failed: {error}")

    if not lifecycle.finish_deletion(
        lifecycle_operation, index_report=index_feedback
    ):
        warnings.append(
            "trash succeeded but governed derived state is not exact; "
            "content remains tombstoned until reconcile"
        )

    if catalog_target is not None:
        try:
            if log_plan.writes:
                batch_atomic_write(log_plan.writes, vault_root=vault_root)
            catalog_publication.publish_markdown_batch(catalog_target)
        except Exception as error:  # noqa: BLE001 - canonical removal already committed
            raise DeleteDirectoryError(
                code="GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN",
                reason=(
                    "the directory was trashed but its active catalog publication "
                    "did not reach a verified terminal"
                ),
            ) from error

    log_warning = log_plan.warning
    if catalog_target is None:
        log_warning = write_log_entry(
            vault_root,
            date_iso=today_iso,
            op="delete_directory (trash)",
            rel_path_no_ext=rel_path,
            body=log_body,
        )
    if log_warning:
        warnings.append(log_warning)

    try:
        trash_meta_rel = meta_abs.resolve().relative_to(vault_root.resolve()).as_posix()
    except (ValueError, OSError):
        trash_meta_rel = ""

    return DeleteDirectoryResult(
        path=rel_path,
        trash_path=trash_rel,
        trash_meta_path=trash_meta_rel,
        file_count=len(files),
        inbound_link_count=inbound_total,
        warnings=warnings,
        index=index_feedback,
    )


def _delete_in_store(
    vault_root: Path,
    *,
    path: str,
    recursive: bool,
    force_orphan: bool,
    allow_curated: bool,
    today: dt.date | None,
    now: dt.datetime | None,
) -> DeleteDirectoryResult:
    """Trash a folder in one store transaction.

    The store moves each page under one trash folder with its rows; the projector moves the page
    files afterwards, and no Markdown is written. Each binary, and each file or folder the store
    does not own, is renamed into the trash folder first, and the commit records the binaries there
    (``vault_store.binaries`` names the window). A folder the store holds nothing below is renamed
    whole. The commit binds every page and record it read and the folder's counts, so a page that
    changed or that a commit added, or a link from a page that the inbound check did not see,
    refuses the whole commit and every rename is undone. The sidecar lands on disk inside the same
    rollback window. A folder that holds a governed item keeps its tombstone and receipt around the
    commit (``vault_store.governed``), with file mode's limit on a receipt's items.
    """
    from .vault import CreateOnlyConflict, PathGuardError, PlannedWrite
    from .vault_store import availability as store_availability
    from .vault_store import batch as store_batch
    from .vault_store import binaries, governed
    from .vault_store import media as store_media
    from .vault_store import read as store_read
    from .vault_store.write import Expect, PageMove, StoreConflict

    root = Path(vault_root)
    writer = store_availability.page_writer(root)
    with ExitStack() as reading:
        # The read phase sees one revision; the commit's expectations recheck all that it read.
        reading.enter_context(store_read.request_snapshot(root))
        try:
            abs_path, rel_path = resolve_under_vault(root, path)
        except VaultPathError as e:
            raise DeleteDirectoryError(code=e.code, reason=e.reason) from e
        with store_read.snapshot(root) as view:
            if (
                view.page_meta(rel_path) is not None
                or view.binary(rel_path) is not None
                or (abs_path.exists() and not abs_path.is_dir())
            ):
                raise DeleteDirectoryError(
                    code="NOT_A_DIR", reason=f"path is not a directory: {rel_path}"
                )
            pages = [
                meta.path
                for meta in itertools.islice(
                    view.pages(f"{rel_path}/"), DIRECTORY_TRANSITION_LIMIT + 1
                )
            ]
        if not pages and not abs_path.is_dir():
            raise DeleteDirectoryError(code="NOT_FOUND", reason=missing_path_reason(rel_path))
        curated = _refuse_protected(root, rel_path, allow_curated=allow_curated)
        if len(pages) > DIRECTORY_TRANSITION_LIMIT:
            raise DeleteDirectoryError(
                code="PATH_GUARD_LIMIT",
                reason="directory exceeds the bounded trash census",
            )
        with store_read.snapshot(root) as view:
            stored = view.bodies(pages)
            listing = store_targets.folder_files(
                root,
                view,
                rel_path,
                stored,
                operation="delete_directory",
                error_class=DeleteDirectoryError,
            )
        file_count = len(pages) + listing.file_count
        if file_count > DIRECTORY_TRANSITION_LIMIT:
            raise DeleteDirectoryError(
                code="PATH_GUARD_LIMIT",
                reason="directory exceeds the bounded trash census",
            )
        from .collection_store import authority as collection_authority

        collection_authority.refuse_view_paths(
            root, (rel_path, *pages, *listing.files), DeleteDirectoryError
        )
        if file_count and not recursive:
            raise _not_empty(rel_path, file_count)
        hashes = {page: hashlib.sha256(body).hexdigest() for page, body in stored.items()}
        store_targets.active_catalog_gap(
            root,
            "delete_directory",
            error_class=DeleteDirectoryError,
            removals=(
                *(catalog_publication.CatalogRemoval(page, hashes[page]) for page in pages),
                *(catalog_publication.CatalogRemoval(rel, None) for rel in listing.files),
            ),
        )

        # The links into the folder from outside it; a link between two of its pages moves with them.
        checks = store_targets.inbound_links(root, pages)
        counted = store_targets.visible_links(root, checks)
        inbound_total = 0
        inbound_samples: list[str] = []
        for page in pages:
            hits = [link for link in counted[page] if not link.path.startswith(f"{rel_path}/")]
            inbound_total += len(hits)
            inbound_samples.extend(f"{h.path}:{h.line_number} → {page}" for h in hits[:2])
        warnings = _refuse_orphaning(rel_path, inbound_total, inbound_samples, force_orphan)

        now = now or dt.datetime.now()
        today = today or now.date()
        trash_abs = _free_trash_folder(
            root / planned_trash_path(root, rel_path, now),
            lambda slot: store_targets.holds(root, slot.relative_to(root).as_posix())
            or store_read.holds_folder(root, slot.relative_to(root).as_posix()),
        )
        trash_rel = trash_abs.relative_to(root).as_posix()

        def trashed(source: str) -> str:
            return f"{trash_rel}/{source[len(rel_path) + 1:]}"

        if pages or listing.recorded:
            entries = [
                *(binaries.Entry(rel, trashed(rel)) for rel in listing.files),
                *(binaries.Entry(rel, trashed(rel), folder=True) for rel in listing.folders),
            ]
        else:
            # The store refers to nothing below the folder, so it moves as file mode moves it.
            entries = [binaries.Entry(rel_path, trash_rel, folder=True)]
        with store_read.snapshot(root) as view:
            relocation = binaries.plan(view, entries)
    meta_rel = f"{trash_rel}{METADATA_SUFFIX}"
    counts = {"files": file_count, "md_files": len(pages), "inbound": inbound_total}
    sidecar = PlannedWrite(
        root / meta_rel,
        _trash_metadata(
            rel_path,
            now,
            **counts,
            recursive=recursive,
            force_orphan=force_orphan,
            curated=bool(curated and allow_curated),
        ).decode("utf-8"),
        create_only=True,
    )
    moves = {page: trashed(page) for page in pages}
    expect = {
        page: Expect(sha256=hashes[page], linked_from=checks[page].bound) for page in pages
    }
    expect.update(relocation.expect())
    # A page or a record that a commit adds below the folder after this read refuses the trash.
    expect[rel_path] = Expect(pages_below=len(pages), binaries_below=listing.recorded)
    failed = "Trash placement could not be published"
    try:
        with ExitStack() as hydrated:
            if listing.recorded and originals.binding_for(root) is not None:
                from .media_store import local_originals

                # An evicted original's bytes move with its record, so they are fetched first.
                hydrated.enter_context(local_originals(root, (rel_path,)))
            # A governed folder's receipt intent and tombstone come last before the commit.
            deletion = governed.begin_deletion(
                root, source_rel=rel_path, trash_rel=trash_rel, error_class=DeleteDirectoryError
            )
            with governed.committing(deletion), binaries.relocated(root, relocation) as moved:
                store_batch.commit_batch(
                    writer,
                    [sidecar],
                    vault_root=root,
                    lifecycle=store_batch.Lifecycle(
                        moves=tuple(PageMove(source, moves[source]) for source in pages),
                        binary_deletes=relocation.binary_deletes,
                        expect=expect,
                        records=moved.records,
                        refresh=moved.refresh,
                    ),
                )
    except StoreConflict as conflict:
        if conflict.path in moves or conflict.path in relocation.recorded or (
            conflict.path == rel_path
        ):
            raise DeleteDirectoryError(
                "PATH_GUARD_CHANGED",
                f"directory contents changed before the trash transition ({conflict.reason})",
            ) from conflict
        raise DeleteDirectoryError("DELETE_FAILED", failed) from conflict
    except binaries.Stranded as error:
        raise DeleteDirectoryError("DELETE_FAILED", f"{failed}; {binaries.STRANDED}") from error
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "CROSS_DEVICE":
            raise DeleteDirectoryError(
                "CROSS_DEVICE_MOVE", "the trash is on another filesystem than the folder"
            ) from error
        raise DeleteDirectoryError("DELETE_FAILED", failed) from error
    except (CreateOnlyConflict, PathGuardError, OSError) as error:
        raise DeleteDirectoryError("DELETE_FAILED", failed) from error
    binaries.prune_empty(abs_path)
    store_media.after_removal(root, listing.files)

    from . import due_state

    # The pages as they stood, so the Sources they cited are re-judged once they are gone.
    trashed_pages: list[due_state.PageWrite] = []
    for page in pages:
        try:
            trashed_pages.append(
                due_state.PageWrite(page, prior_markdown=stored[page].decode("utf-8"))
            )
        except UnicodeDecodeError:
            continue
    due_state.refresh_after_writes(root, trashed_pages)
    index = governed.settle(
        root, deletion, store_batch.row_report("delete", [*pages, *listing.files]), warnings
    )
    log_warning = write_log_entry(
        root,
        date_iso=today.isoformat(),
        op="delete_directory (trash)",
        rel_path_no_ext=rel_path,
        body=_log_body(
            rel_path,
            trash_rel,
            **counts,
            recursive=recursive,
            force_orphan=force_orphan,
            curated=curated if allow_curated else None,
        ),
    )
    if log_warning:
        warnings.append(log_warning)
    return DeleteDirectoryResult(
        path=rel_path,
        trash_path=trash_rel,
        trash_meta_path=meta_rel,
        file_count=file_count,
        inbound_link_count=inbound_total,
        warnings=warnings,
        index=index,
    )
