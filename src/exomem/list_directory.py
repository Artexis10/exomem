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

from . import privacy_log
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
        target_abs = vault_root.resolve()
        rel_path = ""
    else:
        try:
            target_abs, rel_path = resolve_under_vault(
                vault_root, path, must_exist=True, must_be_dir=True
            )
        except VaultPathError as e:
            raise ListDirectoryError(code=e.code, reason=e.reason) from e

    entries: list[DirectoryEntry] = []
    for child_abs in _walk(target_abs, recursive=recursive, include_hidden=include_hidden):
        try:
            child_rel = child_abs.resolve().relative_to(vault_root.resolve()).as_posix()
        except ValueError:
            continue
        entries.append(_entry_for(child_abs, child_rel))

    # Stable ordering: directories first, then files; alpha within each group.
    entries.sort(key=lambda e: (0 if e.type == "directory" else 1, e.path.lower()))

    return ListDirectoryResult(path=rel_path, entries=entries)


def _walk(directory: Path, *, recursive: bool, include_hidden: bool):
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return
    for child in children:
        name = child.name
        if privacy_log.is_reserved_hosted_vault_path(name):
            continue
        if not include_hidden and (name.startswith(".") or name == "_attachments"):
            continue
        yield child
        if recursive and child.is_dir():
            yield from _walk(child, recursive=True, include_hidden=include_hidden)


def _entry_for(child: Path, rel_path: str) -> DirectoryEntry:
    is_dir = child.is_dir()
    size: int | None = None
    updated: str | None = None
    fm_type: str | None = None

    try:
        st = child.stat()
        if not is_dir:
            size = st.st_size
        updated = dt.datetime.fromtimestamp(st.st_mtime).date().isoformat()
    except OSError:
        pass

    if not is_dir and child.suffix.lower() == ".md":
        try:
            text = child.read_text(encoding="utf-8")
            fm, _, _ = parse_frontmatter(text)
            t = fm.get("type")
            if t:
                fm_type = str(t)
        except (OSError, UnicodeDecodeError):
            pass

    return DirectoryEntry(
        name=child.name,
        type="directory" if is_dir else "file",
        path=rel_path,
        size_bytes=size,
        updated=updated,
        frontmatter_type=fm_type,
    )
