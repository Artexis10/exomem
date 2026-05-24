"""The `delete_file` Tier 2 op: remove a file with safety rails.

Refuses Sources/ and Evidence/ (append-only). Curated trees need
`allow_curated=true`. Requires explicit `confirm=true`. Refuses if the
file has inbound wikilinks unless `force_orphan=true`. Warns on pages
with `superseded_by:` set (history) unless `force_superseded=true`.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from .vault import (
    VaultPathError,
    find_inbound_wikilinks,
    in_append_only_tree,
    in_curated_tree,
    parse_frontmatter,
    resolve_under_vault,
    write_log_entry,
)


log = logging.getLogger(__name__)


@dataclass
class DeleteFileResult:
    path: str
    inbound_link_count: int
    warnings: list[str]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "inbound_link_count": self.inbound_link_count,
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
    today: dt.date | None = None,
) -> DeleteFileResult:
    if not confirm:
        raise DeleteFileError(
            code="UNCONFIRMED",
            reason=(
                "delete_file requires `confirm=true` explicitly. "
                "Deletions are permanent — supersession via `replace` is "
                "the preferred path for compiled material (SKILL.md rule 6)."
            ),
        )

    try:
        abs_path, rel_path = resolve_under_vault(
            vault_root, path, must_exist=True, must_be_file=True
        )
    except VaultPathError as e:
        raise DeleteFileError(code=e.code, reason=e.reason) from e

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
                        f"the supersession chain. Deleting it breaks history. "
                        f"Pass `force_superseded=true` to override."
                    ),
                )
            if fm.get("status") == "active" and fm.get("type") == "entity":
                fm_warn = (
                    f"deleted active entity {rel_path!r} — consider "
                    f"archiving via supersession instead."
                )
        except (OSError, UnicodeDecodeError):
            pass

    # Inbound-link check.
    inbound = find_inbound_wikilinks(vault_root, rel_path)
    if inbound and not force_orphan:
        sample = ", ".join(
            f"{m.path}:{m.line_number}" for m in inbound[:3]
        )
        more = f" (+{len(inbound) - 3} more)" if len(inbound) > 3 else ""
        raise DeleteFileError(
            code="INBOUND_LINKS",
            reason=(
                f"{rel_path} has {len(inbound)} inbound wikilink(s): "
                f"{sample}{more}. Deletion would orphan those links. "
                f"Pass `force_orphan=true` to override (and consider `move_file` "
                f"with update_wikilinks=true instead)."
            ),
        )

    warnings: list[str] = []
    if fm_warn:
        warnings.append(fm_warn)
    if force_orphan and inbound:
        warnings.append(
            f"force_orphan=true: deleted with {len(inbound)} inbound link(s) "
            f"still pointing here. Run `audit` to surface the new broken links."
        )

    try:
        abs_path.unlink()
    except OSError as e:
        raise DeleteFileError(
            code="DELETE_FAILED",
            reason=f"could not delete {rel_path}: {e}",
        ) from e

    today = today or dt.date.today()
    date_iso = today.isoformat()
    rel_no_ext = rel_path.removesuffix(".md") if rel_path.endswith(".md") else rel_path
    log_body = (
        f"Deleted {rel_path!r} via kb-mcp Tier 2. "
        f"inbound_links_at_delete={len(inbound)}."
    )
    if force_orphan:
        log_body += " force_orphan=true."
    if force_superseded:
        log_body += " force_superseded=true."
    if curated and allow_curated:
        log_body += f" allow_curated=true (target tree: {curated})."
    log_warning = write_log_entry(
        vault_root,
        date_iso=date_iso,
        op="delete_file",
        rel_path_no_ext=rel_no_ext,
        body=log_body,
    )
    if log_warning:
        warnings.append(log_warning)

    return DeleteFileResult(
        path=rel_path,
        inbound_link_count=len(inbound),
        warnings=warnings,
    )
