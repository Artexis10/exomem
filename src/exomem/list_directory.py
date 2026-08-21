"""The `list_directory` Tier 2 op: list files/subfolders at a vault path.

Read-only. Works anywhere under vault root, including curated trees
(consistent with `get`). For files with frontmatter, surfaces the
`type` field so callers can scan typed content quickly.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from . import access, privacy_log, reserved_paths
from .vault import (
    VaultPathError,
    parse_frontmatter,
    resolve_under_vault,
)

log = logging.getLogger(__name__)


@dataclass
class DirectoryEntry:
    name: str
    type: str  # "file" or "directory"
    path: str  # vault-relative POSIX
    size_bytes: int | None
    updated: str | None  # ISO date if available
    frontmatter_type: str | None = None  # for .md files only

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "updated": self.updated,
            "frontmatter_type": self.frontmatter_type,
        }


@dataclass
class ListDirectoryResult:
    path: str
    entries: list[DirectoryEntry]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "entries": [e.as_dict() for e in self.entries],
        }


@dataclass
class ListDirectoryError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def list_directory(
    vault_root: Path,
    *,
    path: str,
    recursive: bool = False,
    include_hidden: bool = False,
) -> ListDirectoryResult:
    # Empty string means vault root.
    if path is None or not str(path).strip():
        rel_path = ""
    else:
        try:
            _, rel_path = resolve_under_vault(vault_root, path, must_exist=True)
        except VaultPathError as e:
            raise ListDirectoryError(code=e.code, reason=e.reason) from e
        # Scoped-probe refusal: an excluded dir OR an excluded file probed as
        # `path` reads as missing — byte-identical to resolve_under_vault's own
        # must_exist failure — before the must_be_dir check below can leak an
        # excluded file's existence via NOT_A_DIR.
        if access.refuse_if_excluded(vault_root, rel_path):
            raise ListDirectoryError(
                code="NOT_FOUND", reason=f"path does not exist: {rel_path}"
            )
        try:
            _target_abs, rel_path = resolve_under_vault(
                vault_root, path, must_exist=True, must_be_dir=True
            )
        except VaultPathError as e:
            raise ListDirectoryError(code=e.code, reason=e.reason) from e

    try:
        held_entries = reserved_paths.list_generic_tree(
            vault_root, rel_path or ".", recursive=recursive
        )
    except reserved_paths.ReservedPathLeafError:
        raise ListDirectoryError(
            code="NOT_FOUND", reason=f"path does not exist: {rel_path}"
        ) from None

    entries: list[DirectoryEntry] = []
    for held_entry in held_entries:
        parts = held_entry.relative_path.split("/")
        if not recursive and len(parts) != 1:
            continue
        if any(privacy_log.is_reserved_hosted_vault_path(part) for part in parts):
            continue
        if not include_hidden and any(
            part.startswith(".") or part == "_attachments" for part in parts
        ):
            continue
        child_rel = (
            held_entry.relative_path
            if not rel_path
            else f"{rel_path.rstrip('/')}/{held_entry.relative_path}"
        )
        if access.refuse_if_excluded(vault_root, child_rel):
            continue
        entries.append(_entry_for_held(held_entry, child_rel))

    # Stable ordering: directories first, then files; alpha within each group.
    entries.sort(key=lambda e: (0 if e.type == "directory" else 1, e.path.lower()))

    return ListDirectoryResult(path=rel_path, entries=entries)


def _entry_for_held(
    entry: reserved_paths.GenericTreeEntry, rel_path: str
) -> DirectoryEntry:
    is_dir = entry.identity.kind == "directory"
    updated: str | None = None
    fm_type: str | None = None

    if entry.mtime is not None:
        updated = dt.datetime.fromtimestamp(entry.mtime).date().isoformat()

    if not is_dir and entry.markdown is not None:
        try:
            text = entry.markdown.decode("utf-8")
            fm, _, _ = parse_frontmatter(text)
            t = fm.get("type")
            if t:
                fm_type = str(t)
        except (OSError, UnicodeDecodeError):
            pass

    return DirectoryEntry(
        name=entry.relative_path.rsplit("/", 1)[-1],
        type="directory" if is_dir else "file",
        path=rel_path,
        size_bytes=entry.size_bytes,
        updated=updated,
        frontmatter_type=fm_type,
    )
