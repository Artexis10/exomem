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
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from .kbdir import kb_dirname, kb_prefix
from .vault import (
    VaultPathError,
    find_inbound_wikilinks,
    in_append_only_tree,
    in_curated_tree,
    kb_root,
    resolve_under_vault,
    write_log_entry,
)

log = logging.getLogger(__name__)

TRASH_SUBPATH = "_trash"


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
    if not confirm:
        raise DeleteDirectoryError(
            code="UNCONFIRMED",
            reason=(
                "delete_directory requires `confirm=true` explicitly. "
                f"Deletes go to {kb_prefix()}_trash/ (recoverable) but the "
                "action is still deliberate."
            ),
        )

    try:
        abs_path, rel_path = resolve_under_vault(
            vault_root, path, must_exist=True, must_be_dir=True
        )
    except VaultPathError as e:
        raise DeleteDirectoryError(code=e.code, reason=e.reason) from e

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

    # Enumerate contents.
    contents = sorted(abs_path.rglob("*"), key=lambda item: item.as_posix())
    files = [p for p in contents if p.is_file()]
    if files and not recursive:
        raise DeleteDirectoryError(
            code="NOT_EMPTY",
            reason=(
                f"{rel_path} contains {len(files)} file(s). Pass "
                f"`recursive=true` to trash the whole tree."
            ),
        )

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

    # Move the whole tree into trash.
    now = now or dt.datetime.now()
    today = today or now.date()
    date_dir = now.strftime("%Y-%m-%d")
    time_prefix = now.strftime("%H%M%S")
    sanitized = rel_path.replace(kb_prefix(), "", 1).replace("/", "__")
    trash_basename = f"{time_prefix}-{sanitized}"
    trash_root = kb_root(vault_root) / TRASH_SUBPATH / date_dir
    trash_root.mkdir(parents=True, exist_ok=True)
    trash_abs = trash_root / trash_basename
    i = 2
    while trash_abs.exists():
        trash_abs = trash_root / f"{trash_basename}-{i}"
        i += 1

    # Capture vault-relative paths for every .md file in the doomed tree —
    # before the move, while they still resolve under vault_root. Used to
    # purge the embedding sidecar after the trash move succeeds.
    md_rels_to_unindex: list[str] = []
    for md in md_files:
        try:
            md_rels_to_unindex.append(
                md.resolve().relative_to(vault_root.resolve()).as_posix()
            )
        except ValueError:
            continue

    # Register the self-authored removals BEFORE the move so the watcher's
    # per-file delete events are dropped (sidecar purge happens below).
    if md_rels_to_unindex:
        try:
            from . import file_watcher
            file_watcher.register_self_delete(vault_root, md_rels_to_unindex)
        except Exception:  # noqa: BLE001 — suppression is best-effort
            log.debug("self-delete suppression registration failed", exc_info=True)

    try:
        shutil.move(str(abs_path), str(trash_abs))
    except OSError as e:
        raise DeleteDirectoryError(
            code="TRASH_FAILED",
            reason=f"could not move {rel_path} to trash: {e}",
        ) from e

    index_feedback: dict | None = None
    if md_rels_to_unindex:
        try:
            from . import index_sync
            raw_report = index_sync.delete_after_remove(
                vault_root, md_rels_to_unindex
            )
            report = (
                raw_report
                if isinstance(raw_report, index_sync.IndexSyncReport)
                else index_sync.observed_delete_report(
                    md_rels_to_unindex, degraded=False
                )
            )
        except Exception:  # noqa: BLE001 — sidecars are best-effort
            log.exception(
                "index delete failed for trashed tree %s; sidecar may be stale",
                rel_path,
            )
            warnings.append(
                "trash succeeded but derived-index cleanup failed; run reconcile"
            )
            report = index_sync.observed_delete_report(
                md_rels_to_unindex, degraded=True
            )
        index_feedback = report.as_dict()

    # Metadata sidecar.
    meta = {
        "original_path": rel_path,
        "trashed_at": now.isoformat(),
        "deleted_by": "exomem",
        "file_count_at_trash": len(files),
        "md_file_count_at_trash": len(md_files),
        "inbound_link_count_at_trash": inbound_total,
        "force_orphan_used": bool(force_orphan and inbound_total > 0),
        "allow_curated_used": bool(curated and allow_curated),
        "recursive_used": bool(recursive),
    }
    meta_abs = trash_root / f"{trash_abs.name}.meta.json"
    try:
        meta_abs.write_text(
            json.dumps(meta, indent=2, default=str), encoding="utf-8"
        )
    except OSError as e:
        warnings.append(f"trashed dir ok but meta sidecar write failed: {e}")

    trash_rel = trash_abs.resolve().relative_to(vault_root.resolve()).as_posix()

    today_iso = today.isoformat()
    log_body = (
        f"Trashed directory {rel_path!r} → {trash_rel!r} via exomem Tier 2. "
        f"files={len(files)}, md_files={len(md_files)}, "
        f"inbound_links_at_trash={inbound_total}."
    )
    if recursive:
        log_body += " recursive=true."
    if force_orphan:
        log_body += " force_orphan=true."
    if curated and allow_curated:
        log_body += f" allow_curated=true (target tree: {curated})."
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
