"""The `create_directory` Tier 2 op: mkdir at a vault path.

Refuses curated trees by default (Cognitive Core/, Domains/, Prompt Bank/,
Products/, Personal Context/) — pass `allow_curated=true` to override.
Append-only trees (Sources/, Evidence/) are not refused at the directory
level since those subfolders auto-materialize on add/preserve writes
anyway.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from .vault import (
    VaultPathError,
    in_curated_tree,
    resolve_under_vault,
    write_log_entry,
)


log = logging.getLogger(__name__)


@dataclass
class CreateDirectoryResult:
    path: str
    created: bool  # False if it already existed
    warnings: list[str]

    def as_dict(self) -> dict:
        return {"path": self.path, "created": self.created, "warnings": self.warnings}


@dataclass
class CreateDirectoryError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def create_directory(
    vault_root: Path,
    *,
    path: str,
    parents: bool = True,
    allow_curated: bool = False,
    today: dt.date | None = None,
) -> CreateDirectoryResult:
    try:
        abs_path, rel_path = resolve_under_vault(vault_root, path)
    except VaultPathError as e:
        raise CreateDirectoryError(code=e.code, reason=e.reason) from e

    curated = in_curated_tree(rel_path)
    if curated and not allow_curated:
        raise CreateDirectoryError(
            code="CURATED_PROTECTED",
            reason=(
                f"{rel_path} is in curated tree {curated!r} (desk-managed). "
                f"Pass `allow_curated=true` only if you are genuinely "
                f"building infrastructure inside this tree."
            ),
        )

    already_existed = abs_path.exists()
    if already_existed and not abs_path.is_dir():
        raise CreateDirectoryError(
            code="NOT_A_DIR",
            reason=f"{rel_path} exists but is not a directory",
        )

    if not already_existed:
        try:
            abs_path.mkdir(parents=parents, exist_ok=False)
        except FileNotFoundError as e:
            raise CreateDirectoryError(
                code="MISSING_PARENT",
                reason=(
                    f"intermediate folders missing for {rel_path}; "
                    f"call with parents=true or create them first ({e})"
                ),
            ) from e
        except OSError as e:
            raise CreateDirectoryError(
                code="MKDIR_FAILED",
                reason=f"could not create {rel_path}: {e}",
            ) from e

    warnings: list[str] = []
    if not already_existed:
        today = today or dt.date.today()
        date_iso = today.isoformat()
        log_body = f"Created directory via kb-mcp Tier 2."
        if curated and allow_curated:
            log_body += f" allow_curated=true (target tree: {curated})."
        log_warning = write_log_entry(
            vault_root,
            date_iso=date_iso,
            op="create_directory",
            rel_path_no_ext=rel_path,
            body=log_body,
        )
        if log_warning:
            warnings.append(log_warning)

    return CreateDirectoryResult(
        path=rel_path, created=not already_existed, warnings=warnings
    )
