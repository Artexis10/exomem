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
import json
import logging
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import semantic_writes
from .kbdir import kb_dirname, kb_prefix
from .vault import (
    DirectoryCensusGuard,
    PathGuard,
    PathGuardError,
    VaultPathError,
    in_append_only_tree,
    in_curated_tree,
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

    # Determine restore_path: explicit > sidecar's original_path.
    sidecar = trash_abs.parent / f"{trash_abs.name}.meta.json"
    meta: dict = {}
    sidecar_guard: PathGuard | None = None
    sidecar_source: str | None = None
    if sidecar.exists():
        try:
            sidecar_source, sidecar_guard = read_guarded_text(vault_root, sidecar)
            meta = json.loads(sidecar_source)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, PathGuardError):
            meta = {}
            sidecar_guard = None

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

    if restore_abs.exists():
        raise RecoverError(
            code="DEST_EXISTS",
            reason=(
                f"destination {restore_rel!r} already exists. Choose a "
                f"different restore_path, or move the existing file out of "
                f"the way first."
            ),
        )

    semantic: dict | None = None
    recovery_entries: list[semantic_writes.RecoveryEntry] = []
    destination_root_guard: PathGuard | None = None
    trash_census_guards: tuple[DirectoryCensusGuard, ...] = ()
    if trash_abs.is_file() and trash_rel.lower().endswith(".md"):
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
                    sidecar_guard,
                    sidecar_source,
                )
            )
        except (OSError, UnicodeDecodeError, PathGuardError) as error:
            code = getattr(error, "code", "RECOVER_FAILED")
            raise RecoverError(code=code, reason=str(error)) from error
    elif trash_abs.is_dir():
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
                            sidecar_guard,
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
                relation_reviews=relation_reviews,
            )
            if validate_only:
                return RecoverResult(
                    trash_path=trash_rel,
                    restored_path=restore_rel,
                    kind="directory" if trash_abs.is_dir() else "file",
                    warnings=[],
                    semantic=preflight.as_dict(),
                )

            def restore() -> None:
                try:
                    shutil.move(str(trash_abs), str(restore_abs))
                except OSError as error:
                    raise RecoverError(
                        code="RECOVER_FAILED",
                        reason=(
                            f"could not move {trash_rel!r} → {restore_rel!r}: {error}"
                        ),
                    ) from error

            committed = semantic_writes.commit_recovery(
                vault_root, preflight=preflight, mutate=restore
            )
            semantic = committed.as_dict()
        except semantic_writes.SemanticWriteError as error:
            raise RecoverError(code=error.code, reason=error.reason) from error
        except PathGuardError as error:
            raise RecoverError(code=error.code, reason=error.reason) from error
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
                kind="directory" if trash_abs.is_dir() else "file",
                warnings=[],
            )
        restore_abs.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(trash_abs), str(restore_abs))
        except OSError as e:
            raise RecoverError(
                code="RECOVER_FAILED",
                reason=f"could not move {trash_rel!r} → {restore_rel!r}: {e}",
            ) from e

    warnings: list[str] = []
    if sidecar.exists():
        try:
            sidecar.unlink()
        except OSError as e:
            warnings.append(
                f"recovered file ok but could not remove trash sidecar "
                f"{sidecar.name!r}: {e}"
            )

    index_feedback: dict | None = None
    restored_markdown = (
        sorted(restore_abs.rglob("*.md"))
        if restore_abs.is_dir()
        else ([restore_abs] if restore_abs.suffix.lower() == ".md" else [])
    )
    if restored_markdown:
        from . import file_watcher, index_sync

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
            report = index_sync.upsert_after_write(vault_root, restored_markdown)
        except Exception:  # noqa: BLE001 - restore remains authoritative
            log.exception("restored index refresh failed for %s", restore_rel)
            warnings.append(
                "recovery succeeded but derived-index refresh failed; run reconcile"
            )
            report = index_sync.failed_upsert_report(
                vault_root,
                restored_markdown,
                watcher=watcher_outcome,
            )
        else:
            report = index_sync.with_component(
                report
                if isinstance(report, index_sync.IndexSyncReport)
                else index_sync.unverified_upsert_report(
                    vault_root, restored_markdown
                ),
                watcher_outcome,
            )
        index_feedback = report.as_dict()

    today = today or dt.date.today()
    date_iso = today.isoformat()
    kind = "directory" if restore_abs.is_dir() else "file"
    restore_no_ext = (
        restore_rel.removesuffix(".md") if restore_rel.endswith(".md") else restore_rel
    )
    log_body = (
        f"Recovered {trash_rel!r} → {restore_rel!r} via exomem Tier 2. "
        f"kind={kind}."
    )
    if curated and allow_curated:
        log_body += f" allow_curated=true (target tree: {curated})."
    log_warning = write_log_entry(
        vault_root,
        date_iso=date_iso,
        op="recover_from_trash",
        rel_path_no_ext=restore_no_ext,
        body=log_body,
    )
    if log_warning:
        warnings.append(log_warning)

    return RecoverResult(
        trash_path=trash_rel,
        restored_path=restore_rel,
        kind=kind,
        warnings=warnings,
        semantic=semantic,
        index=index_feedback,
    )
