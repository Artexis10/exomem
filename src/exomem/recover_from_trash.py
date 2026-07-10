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
from dataclasses import dataclass
from pathlib import Path

from .kbdir import kb_dirname, kb_prefix
from .vault import (
    VaultPathError,
    in_append_only_tree,
    in_curated_tree,
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

    def as_dict(self) -> dict:
        return {
            "trash_path": self.trash_path,
            "restored_path": self.restored_path,
            "kind": self.kind,
            "warnings": self.warnings,
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
    if sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}

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
    )
