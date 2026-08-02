"""Pure admission policy for ordinary recall candidates.

Records canonical data is intentionally available through structured queries,
not the ordinary semantic corpus.  Keep this boundary here so every recall
ingress can make the same decision before opening a candidate page.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Iterator
from pathlib import Path

from . import access, vault

RECALL_POLICY_VERSION = "records-manifest-only-v1"
_MAX_MANIFEST_BYTES = 512 * 1024


def is_recall_candidate(vault_root: Path, path: Path | str) -> bool:
    """Whether a Markdown path may enter ordinary semantic recall.

    Only an exact, locally indexable ``_collection.md`` under the exact Records
    layer is admitted.  Other paths remain ordinary candidates without being
    opened; Records descendants are rejected before their content is read.
    """
    root = Path(vault_root)
    rel = _vault_relative(root, path)
    if rel is None:
        return False
    parts = rel.split("/")
    # Validate the spelling we were handed before asking the OS to expand it.
    # That preserves the no-follow/reparse boundary while still allowing the
    # post-validation canonicalization below to catch Windows 8.3 aliases that
    # would otherwise look like an unrelated ordinary note.
    if not _safe_regular_file(root, parts):
        return False
    if _needs_canonical_alias_check(parts):
        canonical_parts = _canonical_parts_after_safe_validation(root, parts)
        # A changed spelling is an alias we cannot safely classify without
        # trusting a filesystem-specific path form.  Suppress it rather than
        # letting it bypass the exact Records boundary.
        if canonical_parts is None or canonical_parts != parts:
            return False
    if _is_records_alias(parts):
        # Casefold/Unicode aliases are never ordinary pages: on a
        # case-insensitive filesystem they can reach the same Records bytes.
        if not _is_records_descendant(parts):
            return False
        if len(parts) < 3 or parts[-1] != "_collection.md":
            return False
        if not access.is_indexable(root, rel):
            return False
        try:
            data, _guard = vault.read_bounded_guarded_bytes(
                root, rel, limit=_MAX_MANIFEST_BYTES
            )
            from . import structured_collections

            manifest = structured_collections.parse_manifest_bytes(root, rel, data)
        except (UnicodeError, ValueError, OSError, vault.PathGuardError):
            return False
        return manifest.semantic_profile == "records"
    if not access.is_indexable(root, rel):
        return False
    return True


def is_structured_only_path(vault_root: Path, path: Path | str) -> bool:
    """True for an exact Records-layer descendant without opening it."""
    rel = _vault_relative(Path(vault_root), path)
    return rel is not None and _is_records_alias(rel.split("/"))


def iter_recall_markdown(vault_root: Path, paths: Iterable[Path]) -> Iterator[Path]:
    """Yield ordinary-recall candidates, applying admission before page reads."""
    root = Path(vault_root)
    for path in paths:
        if is_recall_candidate(root, path):
            yield path


def recall_policy_identity(vault_root: Path) -> tuple[str, str]:
    """Static policy and local access-policy identities for semantic sidecars."""
    return RECALL_POLICY_VERSION, access.policy_fingerprint(Path(vault_root))


def _vault_relative(root: Path, path: Path | str) -> str | None:
    raw = str(path)
    if ("\\" in raw and os.name != "nt") or "\x00" in raw:
        return None
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _is_records_descendant(parts: list[str]) -> bool:
    return len(parts) >= 3 and parts[0] == vault.kb_dirname() and parts[1] == "Records"


def _is_records_alias(parts: list[str]) -> bool:
    return (
        len(parts) >= 3
        and parts[0].casefold() == vault.kb_dirname().casefold()
        and parts[1].casefold() == "records"
    )


def _safe_regular_file(root: Path, parts: list[str]) -> bool:
    current = root
    try:
        for index, part in enumerate(parts):
            current /= part
            info = current.lstat()
            is_reparse = bool(
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
            if stat.S_ISLNK(info.st_mode) or is_reparse:
                return False
            if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                return False
        return stat.S_ISREG(current.lstat().st_mode)
    except OSError:
        return False


def _canonical_parts_after_safe_validation(root: Path, parts: list[str]) -> list[str] | None:
    """Best-effort long-name comparison seam for Windows short-path aliases.

    ``resolve`` is deliberately after :func:`_safe_regular_file`: it is used
    only to compare a verified non-reparse path to the operating system's long
    spelling, never to authorize traversal through an alias or reparse point.
    Tests can replace this seam on any platform; native Windows additionally
    exercises the real 8.3 behaviour where the volume supports it.
    """
    try:
        resolved = (root.joinpath(*parts)).resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.parts or any(part in {"", ".", ".."} for part in resolved.parts):
        return None
    return list(resolved.parts)


def _needs_canonical_alias_check(parts: list[str]) -> bool:
    """Whether this platform can report a short-name alias for this spelling.

    Linux/macOS keep the hot corpus path syscall-free after the no-follow lstat
    validation above.  Windows uses the canonical comparison for all names: an
    8.3 alias cannot be recognized reliably from its text alone.
    """
    return os.name == "nt"
