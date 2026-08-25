"""The `move_file` Tier 2 op: relocate a file, optionally update wikilinks.

Append-only trees (Sources/, Evidence/): relocation WITHIN the same tree is
allowed (a move carries bytes verbatim — only location changes, content
stays immutable per rule 2), enabling themed sub-folders. Moves that cross
the boundary are refused: OUT of an append-only tree, or INTO one from
elsewhere (those land via `add` / `preserve`).
Curated trees on either end need `allow_curated=true`.

When `update_wikilinks=true` (default), scans the full vault for
`[[<old>]]` and `[[<basename>]]` references and rewrites them to point at
the new location. Returns the count of touched files.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import reserved_paths, semantic_index, semantic_writes
from .governance import catalog_publication
from .kbdir import kb_dirname
from .vault import (
    LogWritePlan,
    PathGuard,
    PathGuardError,
    PlannedWrite,
    VaultPathError,
    batch_atomic_write,
    content_hash,
    find_inbound_wikilinks,
    in_append_only_tree,
    in_curated_tree,
    plan_log_entry,
    read_guarded_text,
    resolve_under_vault,
    walk_vault_md,
    write_log_entry,
)

log = logging.getLogger(__name__)

@dataclass
class MoveFileResult:
    old_path: str
    new_path: str
    wikilinks_updated: int
    files_touched: list[str]
    warnings: list[str]
    semantic: dict | None = None
    index: dict | None = None

    def as_dict(self) -> dict:
        return {
            "old_path": self.old_path,
            "new_path": self.new_path,
            "wikilinks_updated": self.wikilinks_updated,
            "files_touched": self.files_touched,
            "warnings": self.warnings,
            "semantic": self.semantic,
            "index": self.index,
        }


@dataclass
class MoveFileError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def _held_rename(vault_root: Path, old_rel: str, new_rel: str) -> None:
    try:
        reserved_paths.move_generic_path(
            vault_root,
            old_rel,
            new_rel,
            source_kind="file",
        )
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "CROSS_DEVICE":
            raise MoveFileError(
                "CROSS_DEVICE_MOVE",
                "file moves require one filesystem",
            ) from None
        if error.code == "DESTINATION_EXISTS":
            raise MoveFileError("DEST_EXISTS", "destination already exists") from None
        if error.code in {
            "CAPABILITY_UNAVAILABLE",
            "IDENTITY_CHANGED",
            "RESERVED_PATH",
            "UNSAFE_PATH",
        }:
            raise MoveFileError(
                "RESERVED_PATH",
                "path is reserved for its owning subsystem",
            ) from None
        raise MoveFileError("MOVE_FAILED", "held file move was refused") from None


#: Frontmatter key holding a page's pointer at the bytes it describes. The
#: name is a misnomer once a Source uses it, and is kept because roughly fifteen
#: readers depend on it.
_ARTIFACT_POINTER_RE = re.compile(r"(?m)^(evidence_file:[ \t]*)(.+?)[ \t]*$")


def _compose_transforms(first, second):
    """Run a caller's own content transform, then this module's repointing.

    Order matters only in that the caller declared its change first; the
    repointing is mechanical and must see the final text.
    """
    if first is None:
        return second
    return lambda text: second(first(text))


def _paired_artifact(vault_root: Path, rel: str) -> tuple[str, str] | None:
    """Return `(page_rel, binary_rel)` when `rel` is half of a stored artifact.

    A captured artifact is two files — the bytes and the page that addresses
    them, named `<filename>` and `<filename>.md`. Relocating one without the
    other leaves the bytes with no page, which is the unaddressable state the
    page contract exists to prevent, or a page describing an artifact that is
    no longer there. So either half names the whole unit.

    Returns None for an ordinary page, which is the common case: a source page
    whose stem happens to have no sibling file is not a pair.
    """
    if rel.lower().endswith(".md"):
        binary_rel = rel[:-3]
        if not binary_rel or binary_rel.lower().endswith(".md"):
            return None
        return (rel, binary_rel) if (vault_root / binary_rel).is_file() else None
    page_rel = f"{rel}.md"
    return (page_rel, rel) if (vault_root / page_rel).is_file() else None


def _repoint_artifact(old_binary: str, new_binary: str):
    """A content transform that repoints a moved page at its moved bytes."""

    def transform(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            return f"{match.group(1)}{new_binary}" if match.group(2) == old_binary else match.group(0)

        return _ARTIFACT_POINTER_RE.sub(replace, text)

    return transform


def move_file(
    vault_root: Path,
    *,
    old_path: str,
    new_path: str,
    update_wikilinks: bool = True,
    allow_curated: bool = False,
    today: dt.date | None = None,
    promotion_reason: str | None = None,
    content_transform: Callable[[str], str] | None = None,
    extra_writes: tuple[PlannedWrite, ...] = (),
) -> MoveFileResult:
    """Relocate a file, optionally rewriting wikilinks that point at it.

    `content_transform` lets a caller that owns a *declared* content change —
    today only source reclassification — supply the moved file's new bytes. It
    runs after the self-wikilink pass, so the append-only refusal that pass
    raises still fires first: the guard exists to stop an incidental link
    rewrite mutating append-only bytes, not to stop a caller that has already
    justified the change. `extra_writes` joins the same atomic batch so a
    caller's own registry updates commit with the move or not at all.
    """
    try:
        old_abs, old_rel = resolve_under_vault(
            vault_root, old_path, must_exist=True, must_be_file=True
        )
    except VaultPathError as e:
        raise MoveFileError(code=e.code, reason=e.reason) from e
    try:
        reserved_paths.inspect_generic_file(vault_root, old_rel)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code in {
            "CAPABILITY_UNAVAILABLE",
            "IDENTITY_CHANGED",
            "RESERVED_PATH",
            "UNSAFE_PATH",
        }:
            raise MoveFileError(
                "RESERVED_PATH",
                "path is reserved for its owning subsystem",
            ) from None
        raise MoveFileError("MOVE_FAILED", "held file acquisition was refused") from None
    try:
        new_abs, new_rel = resolve_under_vault(vault_root, new_path)
    except VaultPathError as e:
        raise MoveFileError(code=e.code, reason=e.reason) from e

    # An artifact and its page move as one unit. Whichever half the caller
    # named, the operation is normalized onto the page — that is the `.md` path
    # the semantic write machinery below is written for — and the bytes are
    # renamed alongside it inside the same transaction.
    paired_binary: tuple[str, str] | None = None
    pair = _paired_artifact(vault_root, old_rel)
    if pair is not None:
        page_rel, binary_rel = pair
        if old_rel == binary_rel:
            if not new_rel.lower().endswith(".md"):
                paired_binary = (binary_rel, new_rel)
                new_rel = f"{new_rel}.md"
                new_abs = new_abs.with_name(new_abs.name + ".md")
            else:
                paired_binary = (binary_rel, new_rel[:-3])
            old_rel = page_rel
            old_abs = old_abs.with_name(old_abs.name + ".md")
        else:
            new_binary = new_rel[:-3] if new_rel.lower().endswith(".md") else new_rel
            if not new_rel.lower().endswith(".md"):
                new_rel = f"{new_rel}.md"
                new_abs = new_abs.with_name(new_abs.name + ".md")
            paired_binary = (binary_rel, new_binary)
        if (vault_root / paired_binary[1]).exists():
            raise MoveFileError(
                code="DEST_EXISTS",
                reason=(
                    f"destination already exists: {paired_binary[1]}. "
                    f"This op refuses to overwrite — pick a different name."
                ),
            )
        content_transform = _compose_transforms(
            content_transform, _repoint_artifact(*paired_binary)
        )

    try:
        reserved_paths.inspect_generic_path(vault_root, new_rel)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "MISSING":
            pass
        elif error.code in {
            "CAPABILITY_UNAVAILABLE",
            "IDENTITY_CHANGED",
            "RESERVED_PATH",
            "UNSAFE_PATH",
        }:
            raise MoveFileError(
                "RESERVED_PATH",
                "destination is reserved for its owning subsystem",
            ) from None
        else:
            raise MoveFileError(
                "MOVE_FAILED", "held destination acquisition was refused"
            ) from None
    else:
        raise MoveFileError(
            code="DEST_EXISTS",
            reason=(
                f"destination already exists: {new_rel}. "
                f"This op refuses to overwrite — pick a different name."
            ),
        )

    # Append-only guards. Rule 2 protects content *immutability*, not file
    # *location*: a move that stays WITHIN the same append-only tree
    # (Sources/ -> Sources/, Evidence/ -> Evidence/) carries the bytes
    # verbatim and only relocates them, so it is permitted — this is the
    # sanctioned way to organize Sources/ into themed sub-folders. Crossing
    # the boundary is still forbidden in both directions: moving OUT of an
    # append-only tree, or INTO one from elsewhere (those go via `add` /
    # `preserve`).
    src_append = in_append_only_tree(old_rel)
    dst_append = in_append_only_tree(new_rel)
    intra_append = bool(src_append) and src_append == dst_append
    # Sources -> Evidence is a *promotion*: the same raw item, reclassified once
    # it becomes proof-bearing. Whether an item is a Source or Evidence is a
    # judgement about purpose, made at capture time when the answer is often
    # unknowable — a receipt is reference material until the appliance fails.
    # The move carries bytes verbatim, exactly like a sanctioned intra-tree
    # relocation, so content immutability is untouched.
    #
    # The reverse is refused, and the asymmetry is the point. `Evidence/<scope>/`
    # claims to hold everything preserved for that case; handing over a case
    # means handing over that folder. Removing an item alters what the folder
    # claims to contain — bytes unchanged, integrity broken. `Sources/` carries
    # no such completeness property, so promoting out of it subtracts from
    # nothing.
    promotion = src_append == "Sources" and dst_append == "Evidence"
    if promotion and not (promotion_reason or "").strip():
        raise MoveFileError(
            code="PROMOTION_REASON_REQUIRED",
            reason=(
                f"promoting {old_rel} into {dst_append}/ reclassifies it as "
                f"proof-bearing; supply `promotion_reason` naming the claim, "
                f"case, dispute, or record it is being preserved for."
            ),
        )
    if not intra_append and not promotion:
        if src_append == "Evidence":
            raise MoveFileError(
                code="APPEND_ONLY",
                reason=(
                    f"{old_rel} is in Evidence/ and cannot be moved out. A case "
                    f"scope must stay complete: handing over a case means "
                    f"handing over its folder, so removing an item changes what "
                    f"that folder claims to contain. Relocation WITHIN "
                    f"Evidence/ is allowed."
                ),
            )
        if src_append:
            raise MoveFileError(
                code="APPEND_ONLY",
                reason=(
                    f"{old_rel} is in {src_append}/ which is append-only "
                    f"(SKILL.md rule 2). Moves OUT of {src_append}/ are "
                    f"forbidden; relocation WITHIN {src_append}/ is allowed, "
                    f"and Sources/ may be promoted into Evidence/."
                ),
            )
        if dst_append:
            raise MoveFileError(
                code="APPEND_ONLY",
                reason=(
                    f"destination {new_rel} is in {dst_append}/. "
                    f"Use `add` (sources) or `preserve` (evidence) to land "
                    f"content there from outside {dst_append}/."
                ),
            )

    # Curated-tree guards on EITHER end.
    src_curated = in_curated_tree(old_rel)
    dst_curated = in_curated_tree(new_rel)
    if (src_curated or dst_curated) and not allow_curated:
        which = src_curated or dst_curated
        raise MoveFileError(
            code="CURATED_PROTECTED",
            reason=(
                f"move touches curated tree {which!r}. "
                f"Pass `allow_curated=true` to override."
            ),
        )

    old_parts = old_rel.split("/")
    new_parts = new_rel.split("/")
    moved_out_of_trash = (
        len(old_parts) >= 2
        and old_parts[0] == kb_dirname()
        and old_parts[1] == "_trash"
    )
    moved_into_trash = (
        len(new_parts) >= 2
        and new_parts[0] == kb_dirname()
        and new_parts[1] == "_trash"
    )
    if moved_out_of_trash and not moved_into_trash:
        from . import recover_from_trash as recovery

        try:
            recovered = recovery.recover_from_trash(
                vault_root,
                trash_path=old_rel,
                restore_path=new_rel,
                allow_curated=allow_curated,
                today=today,
            )
        except recovery.RecoverError as error:
            raise MoveFileError(code=error.code, reason=error.reason) from error
        return MoveFileResult(
            old_path=old_rel,
            new_path=new_rel,
            wikilinks_updated=0,
            files_touched=[],
            warnings=recovered.warnings,
            semantic=recovered.semantic,
            index=recovered.index,
        )

    # Scan inbound links BEFORE the move, while the old path still exists.
    inbound = find_inbound_wikilinks(vault_root, old_rel) if update_wikilinks else []

    warnings: list[str] = []
    files_touched: list[str] = []
    wikilinks_updated = 0

    # Stage inbound-link rewrites. The file itself moves with one filesystem
    # rename so bytes of any type are preserved without a copy/unlink window.
    writes: list[PlannedWrite] = []
    batch_index_reports: list[object] = []
    batch_fanout_paths: list[Path] = []
    if update_wikilinks and inbound:
        files_to_rewrite = sorted({hit.path for hit in inbound})
        for rel in files_to_rewrite:
            if rel == old_rel:
                continue
            try:
                abs_file = (vault_root / rel).resolve()
                abs_file.relative_to(vault_root.resolve())
            except (ValueError, OSError):
                continue
            try:
                text, guard = read_guarded_text(vault_root, abs_file)
            except (OSError, UnicodeDecodeError, PathGuardError):
                continue
            new_text, n_changed = _rewrite_wikilinks(text, old_rel, new_rel)
            if n_changed > 0:
                append_tree = in_append_only_tree(rel)
                # A generated index inside an append-only tree is not captured
                # content: `add` rewrites `Sources/index.md` on every capture.
                # Rule 2 protects the material, and refusing to repoint an index
                # at a file this op just moved would leave the index dangling —
                # the opposite of what the guard is for.
                if rel.rsplit("/", 1)[-1] == "index.md":
                    append_tree = None
                if append_tree:
                    raise MoveFileError(
                        code="APPEND_ONLY",
                        reason=(
                            f"updating inbound wikilinks would rewrite {rel} in "
                            f"append-only {append_tree}/. Retry with "
                            f"`update_wikilinks=false` to preserve exact bytes "
                            f"and run final-corpus semantic evaluation."
                        ),
                    )
                writes.append(
                    PlannedWrite(path=abs_file, content=new_text, guard=guard)
                )
                files_touched.append(rel)
                wikilinks_updated += n_changed

    today = today or dt.date.today()

    def plan_activity_log() -> tuple[str, str, LogWritePlan]:
        date_iso = today.isoformat()
        new_rel_no_ext = (
            new_rel.removesuffix(".md") if new_rel.endswith(".md") else new_rel
        )
        body = (
            f"Moved {old_rel!r} → {new_rel!r} via exomem Tier 2. "
            f"wikilinks_updated={wikilinks_updated} across "
            f"{len(files_touched)} file(s)."
        )
        if src_curated or dst_curated:
            body += f" allow_curated=true (tree: {src_curated or dst_curated})."
        if promotion:
            body += f" Promoted Sources → Evidence: {promotion_reason.strip()}"
        return (
            new_rel_no_ext,
            body,
            plan_log_entry(
                vault_root,
                date_iso=date_iso,
                op="move_file",
                rel_path_no_ext=new_rel_no_ext,
                body=body,
            ),
        )

    semantic: dict | None = None
    semantic_states: dict[str, semantic_index.SemanticParentIndexState] = {}
    catalog_target: catalog_publication.PreparedMarkdownCatalogPublication | None = None
    log_rel_no_ext: str
    log_body: str
    log_plan: LogWritePlan
    if old_rel.lower().endswith(".md") and new_rel.lower().endswith(".md"):
        try:
            source, source_guard = read_guarded_text(vault_root, old_abs)
            moved_source = source
            if update_wikilinks:
                moved_source, source_changes = _rewrite_wikilinks(
                    source, old_rel, new_rel
                )
                if source_changes:
                    if src_append:
                        raise MoveFileError(
                            code="APPEND_ONLY",
                            reason=(
                                f"updating self-links would rewrite moved source "
                                f"{old_rel} in append-only {src_append}/. Retry "
                                f"with `update_wikilinks=false` to preserve exact "
                                f"bytes and run final-corpus semantic evaluation."
                            ),
                        )
                    files_touched.append(old_rel)
                    wikilinks_updated += source_changes
            if content_transform is not None:
                moved_source = content_transform(moved_source)
            log_rel_no_ext, log_body, log_plan = plan_activity_log()
            destination_guard = PathGuard.capture(
                vault_root, new_rel, leaf_policy="absent"
            )
            preflight = semantic_writes.preflight_move(
                vault_root,
                old_path=old_rel,
                new_path=new_rel,
                source=source,
                moved_source=moved_source,
                source_guard=source_guard,
                destination_guard=destination_guard,
                rewrites=writes,
                content_transform=content_transform,
            )
            semantic_states = {
                item.after.path: semantic_index.from_semantic_page_state(item.after)
                for item in preflight.evaluations
            }

            def mutate(
                lifecycle_writes: tuple[PlannedWrite, ...],
                required_guards,
                bound_destination: PathGuard,
            ) -> None:
                nonlocal catalog_target
                bound_destination.recheck(vault_root)
                catalog_removals = [
                    catalog_publication.CatalogRemoval(
                        old_rel,
                        content_hash(source),
                    )
                ]
                if paired_binary is not None:
                    catalog_removals.append(
                        catalog_publication.CatalogRemoval(
                            paired_binary[0],
                            None,
                        )
                    )
                catalog_writes = [
                    *lifecycle_writes,
                    PlannedWrite(
                        path=new_abs,
                        content=moved_source,
                        create_only=True,
                    ),
                    *writes,
                    *extra_writes,
                    *log_plan.writes,
                ]
                try:
                    catalog_target = (
                        catalog_publication.prepare_catalog_membership_batch(
                            vault_root,
                            writes=tuple(catalog_writes),
                            removals=tuple(catalog_removals),
                            now=int(time.time()),
                        )
                    )
                except catalog_publication.CatalogPublicationError as error:
                    raise MoveFileError(
                        code="GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
                        reason=str(error),
                    ) from error
                _held_rename(vault_root, old_rel, new_rel)
                # The bytes follow the page inside the same transaction. A
                # rollback below undoes both, so a failure can never leave an
                # artifact split across two trees.
                if paired_binary is not None:
                    try:
                        _held_rename(vault_root, *paired_binary)
                    except MoveFileError:
                        _held_rename(vault_root, new_rel, old_rel)
                        raise
                try:
                    destination_writes = (
                        [PlannedWrite(path=new_abs, content=moved_source)]
                        if moved_source != source
                        else []
                    )
                    combined = [
                        *lifecycle_writes,
                        *destination_writes,
                        *writes,
                        *extra_writes,
                    ]
                    if catalog_target is not None:
                        combined.extend(log_plan.writes)
                    if combined:
                        batch_fanout_paths[:] = [write.path for write in combined]
                        batch_atomic_write(
                            combined,
                            vault_root=vault_root,
                            required_guards=required_guards,
                            index_reports=batch_index_reports,
                            semantic_states={
                                write.path.relative_to(vault_root).as_posix(): semantic_states[
                                    write.path.relative_to(vault_root).as_posix()
                                ]
                                for write in combined
                                if write.path.relative_to(vault_root).as_posix()
                                in semantic_states
                            },
                        )
                except Exception as error:
                    log.exception(
                        "move_file: link-update batch failed for %s -> %s",
                        old_rel,
                        new_rel,
                    )
                    try:
                        if paired_binary is not None:
                            _held_rename(vault_root, paired_binary[1], paired_binary[0])
                        _held_rename(vault_root, new_rel, old_rel)
                    except MoveFileError as rollback_error:
                        raise RuntimeError(
                            f"move link rewrite failed ({error}); rename rollback also "
                            f"failed: {rollback_error}"
                        ) from error
                    raise

            committed = semantic_writes.commit_move(
                vault_root, preflight=preflight, mutate=mutate
            )
            semantic = committed.as_dict()
        except semantic_writes.SemanticWriteError as error:
            raise MoveFileError(code=error.code, reason=error.reason) from error
        except PathGuardError as error:
            raise MoveFileError(code=error.code, reason=error.reason) from error
        if catalog_target is not None:
            try:
                catalog_publication.publish_markdown_batch(catalog_target)
            except catalog_publication.CatalogPublicationError as error:
                raise MoveFileError(
                    code="GOVERNANCE_CATALOG_PUBLICATION_UNCERTAIN",
                    reason=(
                        "the file move committed but its active catalog publication "
                        "did not reach a verified terminal"
                    ),
                ) from error
    else:
        log_rel_no_ext, log_body, log_plan = plan_activity_log()
        unsupported_rel = (
            old_rel if not old_rel.lower().endswith(".md") else new_rel
        )
        try:
            catalog_target = catalog_publication.prepare_catalog_membership_batch(
                vault_root,
                writes=log_plan.writes,
                removals=(
                    catalog_publication.CatalogRemoval(
                        unsupported_rel,
                        None,
                    ),
                ),
                now=int(time.time()),
            )
        except catalog_publication.CatalogPublicationError as error:
            raise MoveFileError(
                code="GOVERNANCE_CATALOG_PUBLICATION_BLOCKED",
                reason=str(error),
            ) from error
        _held_rename(vault_root, old_rel, new_rel)
        try:
            if writes:
                batch_atomic_write(writes, vault_root=vault_root)
        except Exception as e:
            log.exception(
                "move_file: link-update batch failed for %s -> %s", old_rel, new_rel
            )
            try:
                _held_rename(vault_root, new_rel, old_rel)
            except MoveFileError as rollback_error:
                raise RuntimeError(
                    f"move link rewrite failed ({e}); rename rollback also failed: "
                    f"{rollback_error}"
                ) from e
            raise

    # The filesystem transaction succeeded. Only now notify watcher/sidecars;
    # no derived index observes a move that was subsequently rolled back.
    from . import file_watcher, index_sync

    watcher_failures: list[str] = []
    try:
        file_watcher.register_self_delete(vault_root, [old_rel])
    except Exception:  # noqa: BLE001 — suppression is independently observed
        log.debug("move delete suppression registration failed", exc_info=True)
        watcher_failures.append("self_delete_registration_failed")
    if semantic is None or source == moved_source:
        try:
            file_watcher.register_self_write(vault_root, [new_abs])
        except Exception:  # noqa: BLE001 — suppression is independently observed
            log.debug("move write suppression registration failed", exc_info=True)
            watcher_failures.append("self_write_registration_failed")

    upsert_reports: list[index_sync.IndexSyncReport] = []
    if batch_fanout_paths:
        if batch_index_reports:
            for report in batch_index_reports:
                upsert_reports.append(
                    report
                    if isinstance(report, index_sync.IndexSyncReport)
                    else index_sync.unverified_upsert_report(
                        vault_root, batch_fanout_paths
                    )
                )
        else:
            upsert_reports.append(
                index_sync.failed_upsert_report(vault_root, batch_fanout_paths)
            )
    if semantic is None or source == moved_source:
        try:
            if new_rel in semantic_states:
                report = index_sync.upsert_after_write(
                    vault_root,
                    [new_abs],
                    semantic_states={new_rel: semantic_states[new_rel]},
                )
            else:
                report = index_sync.upsert_after_write(vault_root, [new_abs])
        except Exception:  # noqa: BLE001 — move remains authoritative
            log.exception("index upsert failed for moved destination %s", new_rel)
            upsert_reports.append(
                index_sync.failed_upsert_report(vault_root, [new_abs])
            )
        else:
            upsert_reports.append(
                report
                if isinstance(report, index_sync.IndexSyncReport)
                else index_sync.unverified_upsert_report(vault_root, [new_abs])
            )
    try:
        raw_delete_report = index_sync.delete_after_remove(vault_root, [old_rel])
    except Exception:  # noqa: BLE001 — move remains authoritative
        log.exception("index delete failed for moved source %s", old_rel)
        delete_report = index_sync.observed_delete_report(
            [old_rel], degraded=True
        )
    else:
        delete_report = (
            raw_delete_report
            if isinstance(raw_delete_report, index_sync.IndexSyncReport)
            else index_sync.observed_delete_report([old_rel], degraded=False)
        )
    reconcile_required = bool(
        watcher_failures
        or delete_report.reconcile_required
        or any(report.reconcile_required for report in upsert_reports)
    )
    index_feedback = {
        "operation": "move",
        "upsert_reports": [report.as_dict() for report in upsert_reports],
        "delete_report": delete_report.as_dict(),
        "watcher": {
            "outcome": "degraded" if watcher_failures else "completed",
            "codes": watcher_failures or ["suppression_registered"],
        },
        "reconcile_required": reconcile_required,
        "reconcile_guidance": (
            "Run reconcile to repair observed move fan-out degradation."
            if reconcile_required
            else None
        ),
    }

    log_warning = log_plan.warning
    if catalog_target is None:
        log_warning = write_log_entry(
            vault_root,
            date_iso=today.isoformat(),
            op="move_file",
            rel_path_no_ext=log_rel_no_ext,
            body=log_body,
        )
    if log_warning:
        warnings.append(log_warning)

    return MoveFileResult(
        old_path=old_rel,
        new_path=new_rel,
        wikilinks_updated=wikilinks_updated,
        files_touched=files_touched,
        warnings=warnings,
        semantic=semantic,
        index=index_feedback,
    )


def _rewrite_wikilinks(text: str, old_rel: str, new_rel: str) -> tuple[str, int]:
    return semantic_writes.rewrite_wikilinks_for_move(text, old_rel, new_rel)


_ = walk_vault_md  # imported for parity with other Tier 2 modules
