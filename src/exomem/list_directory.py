"""The `list_directory` Tier 2 op: list files/subfolders at a vault path.

Read-only. Works anywhere under vault root, including curated trees
(consistent with `get`). For files with frontmatter, surfaces the
`type` field so callers can scan typed content quickly.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import access, privacy_log, reserved_paths
from .collection_store import ownership
from .vault import (
    VaultPathError,
    VaultPathResolution,
    parse_frontmatter,
    resolve_under_vault,
)

log = logging.getLogger(__name__)

#: A cursor holds one vault path and two small fields; anything longer is not one.
_MAX_CURSOR_CHARS = 4096


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
    #: More visible entries follow this page; always False for a whole listing.
    truncated: bool = False

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


def entry_sort_key(entry_type: str, path: str) -> tuple[int, str, str]:
    """Directories first, then files; by path without regard to case, then exactly."""
    return (0 if entry_type == "directory" else 1, path.lower(), path)


def encode_cursor(listing: str, recursive: bool, last: DirectoryEntry) -> str:
    """An opaque continuation after `last`, bound to one listing.

    It carries only a path the caller already received, so it is not signed: a
    forged cursor can only start this listing at another point of the folder.
    """
    payload = {"v": 1, "path": listing, "recursive": recursive,
               "after": list(entry_sort_key(last.type, last.path))}
    # ASCII escapes keep a name that is not valid UTF-8 encodable; it round-trips.
    raw = json.dumps(payload, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: object, listing: str, recursive: bool) -> tuple[int, str, str]:
    """The sort key a cursor resumes after; INVALID_CURSOR for any other listing."""
    try:
        if not isinstance(token, str) or not 0 < len(token) <= _MAX_CURSOR_CHARS:
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        after = payload["after"]
        if (payload["v"] != 1 or payload["path"] != listing or payload["recursive"] is not recursive
                or not isinstance(after, list) or len(after) != 3 or after[0] not in (0, 1)
                or not all(isinstance(part, str) for part in after[1:])):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ListDirectoryError(
            code="INVALID_CURSOR", reason="cursor does not continue this listing"
        ) from None
    return (after[0], after[1], after[2])


@ownership.reader
def list_directory(
    vault_root: Path,
    *,
    path: str,
    recursive: bool = False,
    include_hidden: bool = False,
    limit: int | None = None,
    after: tuple[int, str, str] | None = None,
    exclude: Callable[[str], bool] | None = None,
) -> ListDirectoryResult:
    """List a folder; with `limit`, one page after the `after` sort key.

    A page reads bytes and frontmatter, and runs egress decisions, only for
    the entries it returns; the folder's children are still each opened and
    stat'ed once to enumerate it. `exclude` drops vault paths before they can
    take a place on the page.
    """
    from .governance import egress

    # A caller other than the owner lists what it may see: a withheld file is
    # not an entry, and a folder holding only withheld files reads as a folder
    # that is not there. The owner's listing is unchanged (`keep` is None).
    collapsed: dict[str, bool] = {}
    # Empty string means vault root.
    if path is None or not str(path).strip():
        rel_path = ""
        resolved_path = ""
    else:
        try:
            resolution = resolve_under_vault(vault_root, path, must_exist=True, return_details=True)
        except VaultPathError as e:
            raise ListDirectoryError(code=e.code, reason=e.reason) from e
        assert isinstance(resolution, VaultPathResolution)
        rel_path, resolved_path = resolution.relative, resolution.resolved_relative
        # Scoped-probe refusal: an excluded dir OR an excluded file probed as
        # `path` reads as missing — byte-identical to resolve_under_vault's own
        # must_exist failure — before the must_be_dir check below can leak an
        # excluded file's existence via NOT_A_DIR.
        if access.refuse_if_excluded(vault_root, rel_path) or access.refuse_if_excluded(vault_root, resolved_path):
            raise ListDirectoryError(
                code="NOT_FOUND", reason=f"path does not exist: {rel_path}"
            )
    # Capture both spellings and their ancestors before any tree read can outlive a namespace change.
    if any(ownership.relevant("" if relative == "." else relative, ancestors=True)
           for relative in (rel_path, resolved_path)):
        ownership.current(vault_root)
    keep = egress.restricted_release_filter(vault_root)
    if rel_path:
        if keep is not None and _withheld_target(vault_root, rel_path, keep, include_hidden, collapsed):
            raise ListDirectoryError(
                code="NOT_FOUND", reason=f"path does not exist: {rel_path}"
            )
        try:
            _target_abs, rel_path = resolve_under_vault(
                vault_root, path, must_exist=True, must_be_dir=True
            )
        except VaultPathError as e:
            raise ListDirectoryError(code=e.code, reason=e.reason) from e

    def child_path(relative_path: str) -> str:
        return relative_path if not rel_path else f"{rel_path.rstrip('/')}/{relative_path}"

    def listed(relative_path: str, is_dir: bool) -> bool:
        parts = relative_path.split("/")
        if not recursive and len(parts) != 1:
            return False
        if not _listable(parts, include_hidden):
            return False
        child_rel = child_path(relative_path)
        if access.refuse_if_excluded(vault_root, child_rel):
            return False
        if exclude is not None and exclude(child_rel):
            return False
        return keep is None or not _withheld_target(
            vault_root, child_rel, keep, include_hidden, collapsed, is_dir=is_dir
        )

    truncated = False

    def released(batch: list[str]) -> list[str]:
        # The owner's leaf keeps everything (`keep` is None), yet the terminal
        # egress pass still drops tombstoned pages, and every entry while
        # governance fails closed. A cursor names the page's last entry, so the
        # page holds only what that pass releases, decided by the pass itself.
        if keep is not None or not batch:
            return batch
        payload = egress.filter_withheld_entries(
            vault_root, {"entries": [{"path": child_path(relative)} for relative in batch]}
        )
        kept = {entry["path"] for entry in payload.get("entries") or ()}
        return [relative for relative in batch if child_path(relative) in kept]

    def page(candidates: list[tuple[str, str]]) -> list[str]:
        # One visible entry past the page decides `truncated`, so a page that
        # ends exactly at the last entry reports no further page.
        nonlocal truncated
        chosen: list[str] = []
        batch: list[str] = []
        for relative_path, kind in sorted(
            candidates, key=lambda candidate: entry_sort_key(candidate[1], child_path(candidate[0]))
        ):
            if after is not None and entry_sort_key(kind, child_path(relative_path)) <= after:
                continue
            if not listed(relative_path, kind == "directory"):
                continue
            batch.append(relative_path)
            if len(chosen) + len(batch) > limit:
                chosen.extend(released(batch))
                batch = []
                if len(chosen) > limit:
                    break
        chosen.extend(released(batch))
        truncated = len(chosen) > limit
        return chosen[:limit]

    try:
        held_entries = reserved_paths.list_generic_tree(
            vault_root, rel_path or ".", recursive=recursive,
            select=None if limit is None else page,
        )
    except reserved_paths.ReservedPathLeafError:
        raise ListDirectoryError(
            code="NOT_FOUND", reason=f"path does not exist: {rel_path}"
        ) from None

    entries = [
        _entry_for_held(held_entry, child_path(held_entry.relative_path))
        for held_entry in held_entries
        # A page's entries were decided while it was chosen.
        if limit is not None
        or listed(held_entry.relative_path, held_entry.identity.kind == "directory")
    ]
    entries.sort(key=lambda e: entry_sort_key(e.type, e.path))

    return ListDirectoryResult(path=rel_path, entries=entries, truncated=truncated)


def _listable(parts: list[str], include_hidden: bool) -> bool:
    if any(privacy_log.is_reserved_hosted_vault_path(part) for part in parts):
        return False
    return include_hidden or not any(
        part.startswith(".") or part == "_attachments" for part in parts
    )


def _withheld_target(
    vault_root: Path,
    rel_path: str,
    keep,
    include_hidden: bool,
    collapsed: dict[str, bool],
    *,
    is_dir: bool | None = None,
) -> bool:
    """True when the caller may not see this file, or anything in this folder.

    A folder is withheld when it holds at least one listable file the caller
    may not see and none it may: in a vault without those files the folder
    would not exist. An empty folder, or one holding only unlisted files, is
    listed as it always was. Files are decided lazily, in walk order, and the
    walk stops at the first visible one.
    """
    if is_dir is None:
        is_dir = (Path(vault_root) / rel_path).is_dir()
    if not is_dir:
        return not keep(rel_path)
    cached = collapsed.get(rel_path)
    if cached is not None:
        return cached
    withheld_seen = False
    result = False
    try:
        # Names and kinds decide this; no file in the subtree is opened.
        held = reserved_paths.list_generic_names(vault_root, rel_path or ".", recursive=True)
    except reserved_paths.ReservedPathLeafError:
        held = ()
    for relative_path, kind in held:
        if kind == "directory":
            continue
        parts = relative_path.split("/")
        if not _listable(parts, include_hidden):
            continue
        child_rel = f"{rel_path.rstrip('/')}/{relative_path}" if rel_path else relative_path
        if access.refuse_if_excluded(vault_root, child_rel):
            continue
        if keep(child_rel):
            break
        withheld_seen = True
    else:
        result = withheld_seen
    collapsed[rel_path] = result
    return result


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
