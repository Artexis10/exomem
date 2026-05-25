"""The `create_file` Tier 2 op: write a file at an arbitrary vault path.

Tier 2 escape hatch. Use when the file doesn't fit a Tier 1 typed-note
shape — new folder structures (`Identity/`, `Templates/`), skill files,
config, scratch. For typed notes, use `note`/`add`/`link`/`preserve`.

Refuses by default:
- Sources/ and Evidence/ (append-only — use `add` / `preserve`).
- Curated trees (`Cognitive Core/`, `Domains/`, `Prompt Bank/`, `Products/`,
  `Personal Context/`) unless `allow_curated=true` is passed explicitly.

If `frontmatter` is supplied, it's prepended to `content` as a YAML block
with `created`/`updated` filled to today (unless caller specified them).
Otherwise `content` is written verbatim — caller is responsible for any
frontmatter in the body.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .vault import (
    PlannedWrite,
    VaultPathError,
    batch_atomic_write,
    in_append_only_tree,
    in_curated_tree,
    resolve_under_vault,
    serialize_frontmatter,
    write_log_entry,
)


log = logging.getLogger(__name__)


@dataclass
class CreateFileResult:
    path: str
    warnings: list[str]

    def as_dict(self) -> dict:
        return {"path": self.path, "warnings": self.warnings}


@dataclass
class CreateFileError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def create_file(
    vault_root: Path,
    *,
    path: str,
    content: str,
    frontmatter: dict[str, Any] | None = None,
    overwrite: bool = False,
    allow_curated: bool = False,
    today: dt.date | None = None,
) -> CreateFileResult:
    try:
        abs_path, rel_path = resolve_under_vault(vault_root, path)
    except VaultPathError as e:
        raise CreateFileError(code=e.code, reason=e.reason) from e

    append_only = in_append_only_tree(rel_path)
    if append_only:
        raise CreateFileError(
            code="APPEND_ONLY",
            reason=(
                f"{rel_path} is in {append_only}/ which is append-only. "
                f"Use `add` for sources or `preserve` for evidence."
            ),
        )

    curated = in_curated_tree(rel_path)
    if curated and not allow_curated:
        raise CreateFileError(
            code="CURATED_PROTECTED",
            reason=(
                f"{rel_path} is in curated tree {curated!r} (desk-managed). "
                f"Pass `allow_curated=true` only if you are genuinely "
                f"building infrastructure inside this tree."
            ),
        )

    if abs_path.exists():
        if not overwrite:
            raise CreateFileError(
                code="FILE_EXISTS",
                reason=(
                    f"{rel_path} already exists. Pass `overwrite=true` to "
                    f"replace, or use `edit` / `set_frontmatter_field` / "
                    f"`append_to_file` for surgical changes."
                ),
            )
        if not abs_path.is_file():
            raise CreateFileError(
                code="NOT_A_FILE",
                reason=f"{rel_path} exists but is not a regular file",
            )

    today = today or dt.date.today()
    date_iso = today.isoformat()

    if frontmatter is not None:
        fm = dict(frontmatter)
        fm.setdefault("created", date_iso)
        fm.setdefault("updated", date_iso)
        fm_block = serialize_frontmatter(fm)
        body = content if content.endswith("\n") else content + "\n"
        full_text = f"---\n{fm_block}\n---\n{body}"
    else:
        full_text = content

    warnings: list[str] = []
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        batch_atomic_write([PlannedWrite(path=abs_path, content=full_text)])
    except Exception as e:
        log.exception("create_file write failed for %s", rel_path)
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise

    rel_no_ext = rel_path.removesuffix(".md") if rel_path.endswith(".md") else rel_path
    log_body_parts: list[str] = []
    op_word = "create_file (overwrite)" if overwrite and abs_path.exists() else "create_file"
    log_body_parts.append(f"Created via kb-mcp Tier 2. {len(full_text):,} chars.")
    if frontmatter is not None:
        log_body_parts.append(f"Frontmatter keys: {list(frontmatter.keys())}.")
    if curated and allow_curated:
        log_body_parts.append(f"allow_curated=true (target tree: {curated}).")
    log_warning = write_log_entry(
        vault_root,
        date_iso=date_iso,
        op=op_word,
        rel_path_no_ext=rel_no_ext,
        body=" ".join(log_body_parts),
    )
    if log_warning:
        warnings.append(log_warning)

    return CreateFileResult(path=rel_path, warnings=warnings)
