"""The `append_to_file` Tier 2 op: append text to an existing file.

Refuses Sources/ (immutable post-write). Allowed on Evidence/ sidecars
and general vault files. Curated trees require `allow_curated=true`.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from .kbdir import kb_dirname
from .vault import (
    PlannedWrite,
    VaultPathError,
    batch_atomic_write,
    in_append_only_tree,
    in_curated_tree,
    resolve_under_vault,
    write_log_entry,
)


log = logging.getLogger(__name__)


@dataclass
class AppendResult:
    path: str
    bytes_appended: int
    warnings: list[str]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "bytes_appended": self.bytes_appended,
            "warnings": self.warnings,
        }


@dataclass
class AppendError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def append_to_file(
    vault_root: Path,
    *,
    path: str,
    content: str,
    allow_curated: bool = False,
    today: dt.date | None = None,
) -> AppendResult:
    if content is None:
        raise AppendError(code="INVALID_APPEND", reason="content is required")

    try:
        abs_path, rel_path = resolve_under_vault(
            vault_root, path, must_exist=True, must_be_file=True
        )
    except VaultPathError as e:
        raise AppendError(code=e.code, reason=e.reason) from e

    # Sources/ is fully immutable. Evidence/ allows appends to sidecars and
    # description files (description.md style); the raw artifacts there are
    # binary and wouldn't be markdown-appended anyway.
    parts = rel_path.split("/")
    head = parts[1] if parts[0] == kb_dirname() and len(parts) > 1 else parts[0]
    if head == "Sources":
        raise AppendError(
            code="APPEND_ONLY",
            reason=(
                f"{rel_path} is in Sources/ which is immutable per "
                f"SKILL.md rule 2. Add a corrective source or compile a "
                f"downstream note instead."
            ),
        )

    curated = in_curated_tree(rel_path)
    if curated and not allow_curated:
        raise AppendError(
            code="CURATED_PROTECTED",
            reason=(
                f"{rel_path} is in curated tree {curated!r} (desk-managed). "
                f"Pass `allow_curated=true` to override."
            ),
        )

    existing = abs_path.read_text(encoding="utf-8")
    # Ensure a single newline boundary between existing tail and new content.
    if existing and not existing.endswith("\n"):
        joiner = "\n"
    else:
        joiner = ""
    new_text = existing + joiner + content

    warnings: list[str] = []
    try:
        batch_atomic_write(
            [PlannedWrite(path=abs_path, content=new_text)],
            vault_root=vault_root,
        )
    except Exception as e:
        log.exception("append_to_file write failed for %s", rel_path)
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise

    today = today or dt.date.today()
    date_iso = today.isoformat()
    rel_no_ext = rel_path.removesuffix(".md") if rel_path.endswith(".md") else rel_path
    bytes_appended = len((joiner + content).encode("utf-8"))
    log_body = f"Appended {bytes_appended:,} bytes via exomem Tier 2."
    if curated and allow_curated:
        log_body += f" allow_curated=true (target tree: {curated})."
    log_warning = write_log_entry(
        vault_root,
        date_iso=date_iso,
        op="append_to_file",
        rel_path_no_ext=rel_no_ext,
        body=log_body,
    )
    if log_warning:
        warnings.append(log_warning)

    return AppendResult(
        path=rel_path, bytes_appended=bytes_appended, warnings=warnings
    )

# `in_append_only_tree` import kept available for callers/tests that want
# the consistent guard; the local `head` check above is narrower because
# Evidence/ sidecars are intentionally allowed here.
_ = in_append_only_tree
