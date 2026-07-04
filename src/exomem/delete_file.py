"""The `delete_file` Tier 2 op: trash a file with safety rails.

Deletes are NEVER permanent at this layer. The file is moved to
`Knowledge Base/_trash/YYYY-MM-DD/HHMMSS-<sanitized-original-path>.md`
alongside a `.meta.json` sidecar capturing the original location,
timestamp, inbound link count at trash, and which force-flags were used.
Recovery is `move_file` from the trash path back to the original.
Permanent removal happens desk-side via `rm Knowledge Base/_trash/...`.

The trash semantics are deliberate: an LLM-driven workflow shouldn't be
able to lose data even when it's persuaded (or confused) into pressing
"delete". The guards (`confirm=true`, `force_orphan`, `force_superseded`,
`allow_curated`) stay — they still mark the action as deliberate even
when it's reversible.

Refuses Sources/ and Evidence/ (append-only). Curated trees need
`allow_curated=true`. Trash items themselves can't be re-deleted via
this op (they're already trashed); use the filesystem.
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
    parse_frontmatter,
    resolve_under_vault,
    write_log_entry,
)


log = logging.getLogger(__name__)

TRASH_SUBPATH = "_trash"


@dataclass
class DeleteFileResult:
    path: str
    trash_path: str
    trash_meta_path: str
    inbound_link_count: int
    inbound_ignored_count: int
    warnings: list[str]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "trash_path": self.trash_path,
            "trash_meta_path": self.trash_meta_path,
            "inbound_link_count": self.inbound_link_count,
            "inbound_ignored_count": self.inbound_ignored_count,
            "warnings": self.warnings,
        }


@dataclass
class DeleteFileError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def delete_file(
    vault_root: Path,
    *,
    path: str,
    confirm: bool,
    force_orphan: bool = False,
    force_superseded: bool = False,
    allow_curated: bool = False,
    expected_dead_inbound: list[str] | None = None,
    today: dt.date | None = None,
    now: dt.datetime | None = None,
) -> DeleteFileResult:
    if not confirm:
        raise DeleteFileError(
            code="UNCONFIRMED",
            reason=(
                "delete_file requires `confirm=true` explicitly. "
                f"Deletes go to {kb_prefix()}_trash/ (recoverable) but the "
                "action is still deliberate. Supersession via `replace` is "
                "the preferred path for compiled material (SKILL.md rule 6)."
            ),
        )

    try:
        abs_path, rel_path = resolve_under_vault(
            vault_root, path, must_exist=True, must_be_file=True
        )
    except VaultPathError as e:
        raise DeleteFileError(code=e.code, reason=e.reason) from e

    # Trash items can't be re-trashed.
    parts = rel_path.split("/")
    if len(parts) >= 2 and parts[0] == kb_dirname() and parts[1] == TRASH_SUBPATH:
        raise DeleteFileError(
            code="ALREADY_TRASHED",
            reason=(
                f"{rel_path} is already in {TRASH_SUBPATH}/. To recover, "
                f"use `move_file`. To permanently remove, do it filesystem-side."
            ),
        )

    append_only = in_append_only_tree(rel_path)
    if append_only:
        raise DeleteFileError(
            code="APPEND_ONLY",
            reason=(
                f"{rel_path} is in {append_only}/ which is append-only "
                f"(SKILL.md rule 2). Deletions are forbidden — supersede instead."
            ),
        )

    curated = in_curated_tree(rel_path)
    if curated and not allow_curated:
        raise DeleteFileError(
            code="CURATED_PROTECTED",
            reason=(
                f"{rel_path} is in curated tree {curated!r} (desk-managed). "
                f"Pass `allow_curated=true` only if you really mean it."
            ),
        )

    # Supersession history check.
    fm: dict = {}
    fm_warn: str | None = None
    if rel_path.endswith(".md"):
        try:
            text = abs_path.read_text(encoding="utf-8")
            fm, _, _ = parse_frontmatter(text)
            if fm.get("superseded_by") and not force_superseded:
                raise DeleteFileError(
                    code="SUPERSEDED_HISTORY",
                    reason=(
                        f"{rel_path} has `superseded_by:` set — it's part of "
                        f"the supersession chain. Trashing it breaks the "
                        f"chain. Pass `force_superseded=true` to override."
                    ),
                )
            if fm.get("status") == "active" and fm.get("type") == "entity":
                fm_warn = (
                    f"trashed active entity {rel_path!r} — consider "
                    f"archiving via supersession instead."
                )
        except (OSError, UnicodeDecodeError):
            pass

    # Normalize expected_dead_inbound for filtering.
    expected_set: set[str] = set()
    if expected_dead_inbound:
        for raw in expected_dead_inbound:
            n = str(raw).strip().replace("\\", "/").lstrip("/")
            if not n.endswith(".md"):
                n = n + ".md"
            if not n.startswith(kb_prefix()):
                n = kb_prefix() + n
            expected_set.add(n)

    # Inbound-link check (with `expected_dead_inbound` filtering).
    inbound_all = find_inbound_wikilinks(vault_root, rel_path)
    inbound_ignored = [m for m in inbound_all if m.path in expected_set]
    inbound = [m for m in inbound_all if m.path not in expected_set]

    if inbound and not force_orphan:
        sample = ", ".join(
            f"{m.path}:{m.line_number}" for m in inbound[:3]
        )
        more = f" (+{len(inbound) - 3} more)" if len(inbound) > 3 else ""
        raise DeleteFileError(
            code="INBOUND_LINKS",
            reason=(
                f"{rel_path} has {len(inbound)} inbound wikilink(s) "
                f"(after filtering {len(inbound_ignored)} expected-dead): "
                f"{sample}{more}. Trashing would orphan those links. "
                f"Pass `force_orphan=true` to override, or add the source "
                f"file(s) to `expected_dead_inbound` if you're trashing "
                f"them as part of the same workflow."
            ),
        )

    warnings: list[str] = []
    if fm_warn:
        warnings.append(fm_warn)
    if force_orphan and inbound:
        warnings.append(
            f"force_orphan=true: trashed with {len(inbound)} inbound link(s) "
            f"still pointing here. Run `audit` to surface the new broken links."
        )

    # Move to trash.
    now = now or dt.datetime.now()
    today = today or now.date()
    date_dir = now.strftime("%Y-%m-%d")
    time_prefix = now.strftime("%H%M%S")
    sanitized = rel_path.replace(kb_prefix(), "", 1).replace("/", "__")
    trash_filename = f"{time_prefix}-{sanitized}"
    trash_dir = kb_root(vault_root) / TRASH_SUBPATH / date_dir
    trash_dir.mkdir(parents=True, exist_ok=True)
    trash_abs = trash_dir / trash_filename

    # Collision: never overwrite an existing trash entry.
    if trash_abs.exists():
        i = 2
        while True:
            stem, dot, ext = trash_filename.rpartition(".")
            if dot:
                alt = f"{stem}-{i}.{ext}"
            else:
                alt = f"{trash_filename}-{i}"
            alt_abs = trash_dir / alt
            if not alt_abs.exists():
                trash_abs = alt_abs
                trash_filename = alt
                break
            i += 1

    # Register the self-authored removal BEFORE the move so the watcher's
    # delete event for the KB path is dropped (we purge the sidecar ourselves
    # below). Harmless if the move then fails — the entry TTLs out.
    try:
        from . import file_watcher
        file_watcher.register_self_delete(vault_root, [rel_path])
    except Exception:  # noqa: BLE001 — suppression is best-effort
        log.debug("self-delete suppression registration failed", exc_info=True)

    try:
        shutil.move(str(abs_path), str(trash_abs))
    except OSError as e:
        raise DeleteFileError(
            code="TRASH_FAILED",
            reason=f"could not move {rel_path} to trash: {e}",
        ) from e

    # Drop the file's rows from the index sidecars (embedding + lexical).
    # Soft no-op wherever a backend is unavailable.
    try:
        from . import index_sync
        index_sync.delete_after_remove(vault_root, [rel_path])
    except Exception:  # noqa: BLE001 — sidecars are best-effort
        log.exception("index delete failed for %s; sidecar may be stale", rel_path)

    # Write metadata sidecar capturing what we know at trash time.
    meta = {
        "original_path": rel_path,
        "trashed_at": now.isoformat(),
        "deleted_by": "exomem",
        "inbound_link_count_at_trash": len(inbound_all),
        "inbound_ignored_at_trash": [m.as_dict() for m in inbound_ignored],
        "inbound_remaining_at_trash": [m.as_dict() for m in inbound],
        "force_orphan_used": bool(force_orphan and inbound),
        "force_superseded_used": bool(force_superseded and fm.get("superseded_by")),
        "allow_curated_used": bool(curated and allow_curated),
        "frontmatter_snapshot": _make_json_safe(fm),
    }
    meta_abs = trash_dir / f"{trash_filename}.meta.json"
    try:
        meta_abs.write_text(
            json.dumps(meta, indent=2, default=str), encoding="utf-8"
        )
    except OSError as e:
        warnings.append(f"trashed file ok but meta sidecar write failed: {e}")

    trash_rel = trash_abs.resolve().relative_to(vault_root.resolve()).as_posix()

    today_iso = today.isoformat()
    rel_no_ext = rel_path.removesuffix(".md") if rel_path.endswith(".md") else rel_path
    log_body = (
        f"Trashed {rel_path!r} → {trash_rel!r} via exomem Tier 2. "
        f"inbound_links_at_trash={len(inbound_all)} "
        f"(ignored={len(inbound_ignored)}, remaining={len(inbound)})."
    )
    if force_orphan:
        log_body += " force_orphan=true."
    if force_superseded:
        log_body += " force_superseded=true."
    if curated and allow_curated:
        log_body += f" allow_curated=true (target tree: {curated})."
    log_warning = write_log_entry(
        vault_root,
        date_iso=today_iso,
        op="delete_file (trash)",
        rel_path_no_ext=rel_no_ext,
        body=log_body,
    )
    if log_warning:
        warnings.append(log_warning)

    try:
        trash_meta_rel = meta_abs.resolve().relative_to(vault_root.resolve()).as_posix()
    except (ValueError, OSError):
        trash_meta_rel = ""

    return DeleteFileResult(
        path=rel_path,
        trash_path=trash_rel,
        trash_meta_path=trash_meta_rel,
        inbound_link_count=len(inbound),
        inbound_ignored_count=len(inbound_ignored),
        warnings=warnings,
    )


def _make_json_safe(obj):
    """Convert a frontmatter dict into something json.dumps can handle.

    YAML loads date/datetime as native types; JSON doesn't serialize those
    without a custom encoder. We coerce to ISO strings recursively.
    """
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    return obj
