"""The `get` MCP tool: read a full vault file by path.

Read-only. The ergonomic counterpart to `find` (which returns excerpts) —
when Claude finds a page via `find` and wants to read/cite/build on it,
`get` returns the full frontmatter + body.

Path is vault-relative. Reads anywhere under the vault root:
- `Knowledge Base/...` — the compiled KB layer
- sibling top-level folders (e.g. `Reference/...`) — curated, hand-authored
  material that compiled notes link to, kept read-only via `_access.yaml`.
  `get` honors that by only reading.

The trailing `.md` is optional. Bare-name shortcuts (`Notes/Insights/foo`)
auto-prepend `Knowledge Base/` if the literal path doesn't exist — back-
compat with how this tool worked before the broadening.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import access, privacy_log, reserved_paths
from . import find as find_module
from .kbdir import kb_prefix
from .vault import (
    VaultPathError,
    VaultPathResolution,
    content_hash,
    resolve_under_vault,
)

log = logging.getLogger(__name__)


@dataclass
class GetResult:
    path: str  # vault-relative, with .md, normalized
    frontmatter: dict
    body: str  # markdown body without the frontmatter delimiters
    content: str  # full raw file (frontmatter delimiters + body)
    content_hash: str  # sha256 of `content` — echo to edit(expected_hash=...)
    mtime: float  # file mtime (advisory; hash is the real guard)

    def as_dict(self, include_raw: bool = False) -> dict:
        """Wire shape. `content` (the raw file text) ships only on request —
        it duplicates `body` + `frontmatter`, roughly doubling the payload of
        every read for a field the edit drift-guard never needs: the server
        always computes `content_hash` over the raw bytes here, so callers
        echo the hash without ever reconstructing the hashed text."""
        out = {
            "path": self.path,
            "frontmatter": self.frontmatter,
            "body": self.body,
            "content_hash": self.content_hash,
            "mtime": self.mtime,
        }
        if include_raw:
            out["content"] = self.content
        return out


@dataclass
class GetError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


@dataclass(frozen=True)
class PreparedPageRead:
    """A normalized lexical path and immutable bytes from one read target."""

    target: Path
    path: str
    missing_path: str
    resolved_relative: str
    raw: bytes
    mtime: float


class _SnapshotChanged(OSError):
    """The leaf named by a resolved target changed while it was being bound."""


def _is_link_or_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _snapshot_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _read_prepared_snapshot(target: Path) -> tuple[bytes, os.stat_result] | None:
    """Bind bytes to the resolved leaf without following a later name swap."""
    before = os.lstat(target)
    if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        return None

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        bound = os.fstat(descriptor)
        if (
            not stat.S_ISREG(bound.st_mode)
            or _snapshot_identity(before) != _snapshot_identity(bound)
        ):
            raise _SnapshotChanged("file changed while preparing read")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)

    try:
        after = os.lstat(target)
    except OSError as error:
        raise _SnapshotChanged("file changed while preparing read") from error
    if _is_link_or_reparse(after) or _snapshot_identity(after) != _snapshot_identity(bound):
        raise _SnapshotChanged("file changed while preparing read")
    return raw, bound


#: The fixed reason for bytes that are not UTF-8. The codec's own message
#: names the offending byte and its offset, which is content.
NOT_UTF8_REASON = "file is not UTF-8 text"


def unreadable_or_absent(
    vault_root: Path,
    relatives: tuple[str, ...],
    missing_path: str,
    reason: str,
    *,
    code: str = "UNREADABLE",
) -> GetError:
    """`code` where the caller may see the item, the absent refusal elsewhere.

    A file is known to be unreadable only once its bytes are in hand, and the
    full release decision needs the frontmatter those bytes failed to yield.
    Deciding by path first means a withheld file answers exactly like a
    missing one, whatever its bytes are; a scope that needs the frontmatter
    to classify the path withholds it. Any other refusal that reveals the
    item exists (an ambiguous on-disk spelling) passes its own `code`.
    """
    from .governance import egress

    for rel in dict.fromkeys(relatives):
        if egress.release_level_for_path_only(vault_root, rel) <= egress.LEVEL_NONE:
            # Leave the withheld receipt a decodable read of this page leaves.
            # The first decision records nothing, because above the floor the
            # outcome is an unreadable report, not a release.
            egress.release_level_for_path_only(vault_root, rel, receipt_decision="withheld")
            return GetError(code="NOT_FOUND", reason=f"file does not exist: {missing_path}")
    return GetError(code=code, reason=reason)


def prepare_page_read(vault_root: Path, *, path: str) -> PreparedPageRead:
    """Normalize, resolve, and bind one direct read to an immutable snapshot."""

    if not path or not path.strip():
        raise GetError(code="INVALID_PATH", reason="path is empty")

    rel = path.strip().replace("\\", "/").lstrip("/")
    if privacy_log.is_reserved_hosted_vault_path(rel):
        raise GetError(code="INVALID_PATH", reason="path is reserved by hosted runtime")
    # Only auto-append .md if the path has NO extension. Previously this
    # appended unconditionally, which made e.g. `foo.meta.json` resolve to
    # `foo.meta.json.md` and 404 — surfaced when trying to inspect trash
    # sidecars via `get`.
    rel = missing_path_for(rel)
    missing_path = rel

    candidate = vault_root / rel

    # Back-compat shortcut: if the literal path doesn't exist but the same
    # path under Knowledge Base/ does, use that. Lets callers write
    # `Notes/Insights/foo` without the leading prefix.
    if not candidate.exists() and not rel.startswith(kb_prefix()):
        kb_rel = kb_prefix() + rel
        kb_candidate = vault_root / kb_rel
        if kb_candidate.exists():
            candidate = kb_candidate
            rel = kb_rel

    try:
        resolution = resolve_under_vault(vault_root, rel, return_details=True)
    except VaultPathError as e:
        raise GetError(
            code="INVALID_PATH",
            reason=f"path escapes vault or is unreadable: {e.reason}",
        ) from None
    assert isinstance(resolution, VaultPathResolution)

    # `excluded` paths (_access.yaml) refuse identically to a missing file —
    # never a distinct error, never echoed differently — so the response is
    # not an existence oracle for content the tier marks truly private.
    if access.refuse_if_excluded(
        vault_root, resolution.relative
    ) or access.refuse_if_excluded(vault_root, resolution.resolved_relative):
        raise GetError(
            code="NOT_FOUND",
            reason=f"file does not exist: {missing_path}",
        )
    try:
        # The resolver may have followed a stable in-vault alias. Read the
        # exact resolved target it classified, not the caller spelling that
        # could be swapped after resolution. `resolve_physical_relative` finds
        # the on-disk spelling when it differs from the NFKC one this door
        # otherwise assumes -- a macOS-origin NFD name on a byte-exact
        # filesystem (Linux ext4) -- and refuses outright rather than guess
        # if two physical spellings collide; `physical=True` below then opens
        # exactly that confirmed spelling instead of re-normalizing it away.
        physical_relative = reserved_paths.resolve_physical_relative(
            vault_root, resolution.resolved_relative
        )
        expected_identity = reserved_paths.inspect_generic_file(
            vault_root, physical_relative, physical=True
        )
        snapshot = reserved_paths.read_generic_bytes(
            vault_root, physical_relative, physical=True
        )
        if snapshot.identity != expected_identity:
            raise unreadable_or_absent(
                vault_root,
                (resolution.relative, resolution.resolved_relative),
                missing_path,
                "file changed while being read",
            )
    except reserved_paths.ReservedPathLeafError as error:
        if error.code == "AMBIGUOUS_PATH":
            # Only a caller who may see the page learns it has two spellings;
            # to anyone else a withheld collision is simply absent.
            raise unreadable_or_absent(
                vault_root,
                (resolution.relative, resolution.resolved_relative),
                missing_path,
                (
                    f"{missing_path} matches more than one on-disk spelling; "
                    "refusing to guess which"
                ),
                code="AMBIGUOUS_PATH",
            ) from None
        if error.code in {
            "CAPABILITY_UNAVAILABLE",
            "IDENTITY_CHANGED",
            "MISSING",
            "RESERVED_PATH",
            "UNSAFE_PATH",
        }:
            raise GetError(
                code="NOT_FOUND",
                reason=f"file does not exist: {missing_path}",
            ) from None
        raise unreadable_or_absent(
            vault_root,
            (resolution.relative, resolution.resolved_relative),
            missing_path,
            "file could not be read safely",
        ) from None
    return PreparedPageRead(
        target=resolution.resolved,
        path=resolution.relative,
        missing_path=missing_path,
        resolved_relative=resolution.resolved_relative,
        raw=snapshot.data,
        mtime=snapshot.mtime,
    )


def missing_path_for(path: str) -> str:
    """The spelling a NOT_FOUND names for `path` — the caller's own input,
    normalized only by the suffix rule.

    Shared so the withheld branch in `op_get` raises the byte-identical string
    the absent branch raises. Two call sites formatting "the same" path is how
    a withheld item stops being indistinguishable from a missing one.

    Only auto-append .md when the path has NO extension. Appending
    unconditionally made e.g. `foo.meta.json` resolve to `foo.meta.json.md`
    and 404 — surfaced when inspecting trash sidecars via `get`.
    """
    rel = path.strip().replace("\\", "/").lstrip("/")
    last_segment = rel.rsplit("/", 1)[-1]
    return rel + ".md" if "." not in last_segment else rel


def get_page(
    vault_root: Path,
    *,
    path: str,
    _prepared: PreparedPageRead | None = None,
) -> GetResult:
    """Read any markdown file under the vault root.

    Accepts any vault-relative path. Examples:
    - `Knowledge Base/Notes/Insights/foo.md`
    - `Notes/Insights/foo` (auto-prepends `Knowledge Base/`, auto-adds `.md`)
    - `Reference/Strategy.md`
    - `Reference/AI Systems & Architecture.md`
    """
    prepared = _prepared or prepare_page_read(vault_root, path=path)

    relatives = (prepared.path, prepared.resolved_relative)
    try:
        content = prepared.raw.decode("utf-8")
    except UnicodeDecodeError:
        raise unreadable_or_absent(
            vault_root, relatives, prepared.missing_path, NOT_UTF8_REASON
        ) from None

    parsed = find_module._parse_page(
        prepared.target,
        prepared.mtime,
        vault_root,
        content=prepared.raw,
        resolved_relative=prepared.resolved_relative,
    )
    if parsed is None:
        raise unreadable_or_absent(
            vault_root,
            relatives,
            prepared.missing_path,
            f"could not parse {prepared.path} as a markdown file with frontmatter",
        )
    return GetResult(
        path=prepared.path,
        frontmatter=parsed.frontmatter,
        body=parsed.body,
        content=content,
        content_hash=content_hash(content),
        mtime=prepared.mtime,
    )
