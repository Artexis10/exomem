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
from dataclasses import dataclass
from pathlib import Path

from . import semantic_writes
from .kbdir import kb_dirname
from .vault import (
    PathGuard,
    PathGuardError,
    PlannedWrite,
    VaultPathError,
    batch_atomic_write,
    find_inbound_wikilinks,
    in_append_only_tree,
    in_curated_tree,
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

    def as_dict(self) -> dict:
        return {
            "old_path": self.old_path,
            "new_path": self.new_path,
            "wikilinks_updated": self.wikilinks_updated,
            "files_touched": self.files_touched,
            "warnings": self.warnings,
            "semantic": self.semantic,
        }


@dataclass
class MoveFileError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def move_file(
    vault_root: Path,
    *,
    old_path: str,
    new_path: str,
    update_wikilinks: bool = True,
    allow_curated: bool = False,
    today: dt.date | None = None,
) -> MoveFileResult:
    try:
        old_abs, old_rel = resolve_under_vault(
            vault_root, old_path, must_exist=True, must_be_file=True
        )
    except VaultPathError as e:
        raise MoveFileError(code=e.code, reason=e.reason) from e
    try:
        new_abs, new_rel = resolve_under_vault(vault_root, new_path)
    except VaultPathError as e:
        raise MoveFileError(code=e.code, reason=e.reason) from e

    if new_abs.exists():
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
    if not intra_append:
        if src_append:
            raise MoveFileError(
                code="APPEND_ONLY",
                reason=(
                    f"{old_rel} is in {src_append}/ which is append-only "
                    f"(SKILL.md rule 2). Moves OUT of {src_append}/ are "
                    f"forbidden; relocation WITHIN {src_append}/ is allowed."
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

    # Scan inbound links BEFORE the move, while the old path still exists.
    inbound = find_inbound_wikilinks(vault_root, old_rel) if update_wikilinks else []

    warnings: list[str] = []
    files_touched: list[str] = []
    wikilinks_updated = 0

    # Stage inbound-link rewrites. The file itself moves with one filesystem
    # rename so bytes of any type are preserved without a copy/unlink window.
    writes: list[PlannedWrite] = []
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
                writes.append(
                    PlannedWrite(path=abs_file, content=new_text, guard=guard)
                )
                files_touched.append(rel)
                wikilinks_updated += n_changed

    semantic: dict | None = None
    if old_rel.lower().endswith(".md") and new_rel.lower().endswith(".md"):
        try:
            source, source_guard = read_guarded_text(vault_root, old_abs)
            moved_source = source
            if update_wikilinks:
                moved_source, source_changes = _rewrite_wikilinks(
                    source, old_rel, new_rel
                )
                if source_changes:
                    files_touched.append(old_rel)
                    wikilinks_updated += source_changes
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
            )

            def mutate(
                lifecycle_writes: tuple[PlannedWrite, ...],
                required_guards,
                bound_destination: PathGuard,
            ) -> None:
                bound_destination.recheck(vault_root)
                try:
                    old_abs.rename(new_abs)
                except OSError as error:
                    raise MoveFileError(
                        code="MOVE_FAILED",
                        reason=(
                            f"could not rename {old_rel!r} to {new_rel!r}: {error}"
                        ),
                    ) from error
                try:
                    destination_writes = (
                        [PlannedWrite(path=new_abs, content=moved_source)]
                        if moved_source != source
                        else []
                    )
                    combined = [*lifecycle_writes, *destination_writes, *writes]
                    if combined:
                        batch_atomic_write(
                            combined,
                            vault_root=vault_root,
                            required_guards=required_guards,
                        )
                except Exception as error:
                    log.exception(
                        "move_file: link-update batch failed for %s -> %s",
                        old_rel,
                        new_rel,
                    )
                    try:
                        new_abs.rename(old_abs)
                    except OSError as rollback_error:
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
    else:
        new_abs.parent.mkdir(parents=True, exist_ok=True)
        try:
            old_abs.rename(new_abs)
        except OSError as e:
            raise MoveFileError(
                code="MOVE_FAILED",
                reason=f"could not rename {old_rel!r} to {new_rel!r}: {e}",
            ) from e
        try:
            if writes:
                batch_atomic_write(writes, vault_root=vault_root)
        except Exception as e:
            log.exception(
                "move_file: link-update batch failed for %s -> %s", old_rel, new_rel
            )
            try:
                new_abs.rename(old_abs)
            except OSError as rollback_error:
                raise RuntimeError(
                    f"move link rewrite failed ({e}); rename rollback also failed: "
                    f"{rollback_error}"
                ) from e
            raise

    # The filesystem transaction succeeded. Only now notify watcher/sidecars;
    # no derived index observes a move that was subsequently rolled back.
    try:
        from . import file_watcher
        file_watcher.register_self_delete(vault_root, [old_rel])
        if semantic is None or source == moved_source:
            file_watcher.register_self_write(vault_root, [new_abs])
    except Exception:  # noqa: BLE001 — suppression is best-effort
        log.debug("move watcher suppression registration failed", exc_info=True)

    try:
        from . import index_sync
        if semantic is None or source == moved_source:
            index_sync.upsert_after_write(vault_root, [new_abs])
        index_sync.delete_after_remove(vault_root, [old_rel])
    except Exception:  # noqa: BLE001 — sidecars are best-effort
        log.exception("index refresh failed for moved %s -> %s", old_rel, new_rel)

    # If we just moved a file out of `_trash/`, its `.meta.json` sidecar (if
    # any) is now an orphan. Drop the sidecar — recovery is "removed from
    # trash," not "trash entry that points nowhere." For trash → trash
    # moves we leave it alone (the sidecar is still valid).
    parts = old_rel.split("/")
    moved_out_of_trash = (
        len(parts) >= 2 and parts[0] == kb_dirname() and parts[1] == "_trash"
    )
    new_parts = new_rel.split("/")
    moved_into_trash = (
        len(new_parts) >= 2 and new_parts[0] == kb_dirname()
        and new_parts[1] == "_trash"
    )
    if moved_out_of_trash and not moved_into_trash:
        sidecar = old_abs.parent / f"{old_abs.name}.meta.json"
        if sidecar.exists():
            try:
                sidecar.unlink()
                warnings.append(
                    f"removed orphan trash sidecar: {sidecar.name}"
                )
            except OSError as e:
                warnings.append(
                    f"recovered file but could not remove orphan sidecar "
                    f"{sidecar.name!r}: {e}"
                )

    today = today or dt.date.today()
    date_iso = today.isoformat()
    new_rel_no_ext = new_rel.removesuffix(".md") if new_rel.endswith(".md") else new_rel
    log_body = (
        f"Moved {old_rel!r} → {new_rel!r} via exomem Tier 2. "
        f"wikilinks_updated={wikilinks_updated} across {len(files_touched)} file(s)."
    )
    if src_curated or dst_curated:
        log_body += f" allow_curated=true (tree: {src_curated or dst_curated})."
    log_warning = write_log_entry(
        vault_root,
        date_iso=date_iso,
        op="move_file",
        rel_path_no_ext=new_rel_no_ext,
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
    )


def _rewrite_wikilinks(text: str, old_rel: str, new_rel: str) -> tuple[str, int]:
    return semantic_writes.rewrite_wikilinks_for_move(text, old_rel, new_rel)


_ = walk_vault_md  # imported for parity with other Tier 2 modules
