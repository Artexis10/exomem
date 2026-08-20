"""Pure admission policy for ordinary recall candidates.

Records canonical data is intentionally available through structured queries,
not the ordinary semantic corpus.  Keep this boundary here so every recall
ingress can make the same decision before opening a candidate page.
"""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import access, freshness, vault

RECALL_POLICY_VERSION = "structured-collection-manifest-only-v2"
_MAX_MANIFEST_BYTES = 512 * 1024

# `_canonical_parts_after_safe_validation` compares a candidate's resolved
# spelling against the resolved vault root. The candidate side varies; the root
# side is the same directory for every candidate in a request, and a 600-
# candidate query paid 600 identical `_getfinalpathname` calls for it (#283).
# Memoizing only the fixed side keeps the alias check itself untouched: every
# candidate is still resolved fresh.
#
# Safe in the direction that matters. If the root is swapped underneath us, the
# remembered value no longer prefixes the candidate's freshly-resolved path, so
# `relative_to` raises and the candidate is REJECTED. A stale entry can refuse a
# legitimate page; it cannot admit one that escapes the boundary.
_RESOLVED_ROOTS: dict[str, Path] = {}
_RESOLVED_ROOTS_LOCK = threading.Lock()
_MAX_RESOLVED_ROOTS = 32


def _resolved_root(root: Path) -> Path:
    """``root.resolve()``, memoized per vault root."""
    key = str(root)
    with _RESOLVED_ROOTS_LOCK:
        remembered = _RESOLVED_ROOTS.get(key)
    if remembered is not None:
        return remembered
    # Outside the lock: `resolve()` is a syscall, and holding a global lock
    # across it would serialize every concurrent reader on a cold cache.
    resolved = root.resolve()
    with _RESOLVED_ROOTS_LOCK:
        if len(_RESOLVED_ROOTS) >= _MAX_RESOLVED_ROOTS:
            # Bounded, and a whole-map clear rather than an LRU: the working set
            # is one vault in production and a handful of tmp roots in tests, so
            # eviction never runs hot enough to need finer bookkeeping.
            _RESOLVED_ROOTS.clear()
        _RESOLVED_ROOTS[key] = resolved
    return resolved


def clear_resolved_roots() -> None:
    """Forget memoized root resolutions (test seam; also used by cache eviction)."""
    with _RESOLVED_ROOTS_LOCK:
        _RESOLVED_ROOTS.clear()


@dataclass(frozen=True, slots=True)
class RecallBatchPath:
    """One safe, present Markdown identity captured for index publication."""

    path: Path
    rel_path: str
    signature: freshness.FileSignature
    guard: vault.PathGuard


@dataclass(frozen=True, slots=True)
class RecallBatch:
    """A single guarded admission snapshot for a Markdown index fan-out.

    Identity maintenance needs every live Markdown page; semantic consumers need
    only the policy-admitted subset.  Missing and invalid input are deliberately
    outside both sets: a stale event must not look like a current suppressed
    record and trigger a semantic-only purge of an unrelated later edit.
    """

    policy_version: str
    access_policy_fingerprint: str
    identity_paths: tuple[RecallBatchPath, ...]
    admitted_paths: tuple[RecallBatchPath, ...]
    suppressed_paths: tuple[RecallBatchPath, ...]
    missing_paths: tuple[str, ...]
    invalid_paths: tuple[str, ...]

    def revalidate(self, vault_root: Path) -> bool:
        """Prove the policy and every captured live source still match."""
        if recall_policy_identity(vault_root) != (
            self.policy_version,
            self.access_policy_fingerprint,
        ):
            return False
        admitted = {item.rel_path for item in self.admitted_paths}
        for item in self.identity_paths:
            try:
                item.guard.recheck(vault_root)
                if freshness.stat_signature(item.path) != item.signature:
                    return False
            except (OSError, vault.PathGuardError):
                return False
            if is_recall_candidate(vault_root, item.path) != (item.rel_path in admitted):
                return False
        return True


def partition_markdown_paths(vault_root: Path, paths: Iterable[Path | str]) -> RecallBatch:
    """Capture ordered, de-duplicated Markdown identities and recall admission.

    This is the sole admission snapshot used by incremental index fan-out.
    Paths are re-rooted from a safe vault-relative spelling before any leaf is
    opened, and every live candidate gets a content guard plus a stat signature
    that callers revalidate immediately before publishing semantic sidecars.
    """
    root = Path(vault_root)
    policy_version, access_fingerprint = recall_policy_identity(root)
    identity: list[RecallBatchPath] = []
    admitted: list[RecallBatchPath] = []
    suppressed: list[RecallBatchPath] = []
    missing: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()

    for raw_path in paths:
        rel = _vault_relative(root, raw_path)
        if rel is None:
            invalid.append("<unsafe>")
            continue
        if rel in seen:
            continue
        seen.add(rel)
        if not rel.lower().endswith(".md"):
            invalid.append(rel)
            continue
        path = root.joinpath(*rel.split("/"))
        if not os.path.lexists(path):
            missing.append(rel)
            continue
        if not _safe_regular_file(root, rel.split("/")):
            invalid.append(rel)
            continue
        try:
            admitted_here = is_recall_candidate(root, path)
            # Structured-only Records must never have their body opened merely
            # because an index event arrived. A stable no-follow guard plus the
            # event signature is enough to make their later purge drift-safe.
            if admitted_here:
                _text, guard = vault.read_guarded_text(root, path)
            else:
                guard = vault.PathGuard.capture(root, rel, leaf_policy="stable")
            signature = freshness.stat_signature(path)
            guard.recheck(root)
        except (OSError, UnicodeError, vault.PathGuardError):
            # The leaf may have vanished after lstat; re-classify only that
            # exact safe identity and never route it as a semantic suppression.
            (missing if not os.path.lexists(path) else invalid).append(rel)
            continue
        item = RecallBatchPath(path, rel, signature, guard)
        identity.append(item)
        if admitted_here:
            admitted.append(item)
        else:
            suppressed.append(item)

    return RecallBatch(
        policy_version,
        access_fingerprint,
        tuple(identity),
        tuple(admitted),
        tuple(suppressed),
        tuple(missing),
        tuple(invalid),
    )


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
    if _is_structured_alias(parts):
        # Casefold/Unicode aliases are never ordinary pages: on a
        # case-insensitive filesystem they can reach the same Records bytes.
        if not _is_structured_descendant(parts):
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
        return manifest.semantic_profile in {"records", "planning"}
    if not access.is_indexable(root, rel):
        return False
    return True


def is_structured_only_path(vault_root: Path, path: Path | str) -> bool:
    """True for an exact Records-layer descendant without opening it."""
    rel = _vault_relative(Path(vault_root), path)
    return rel is not None and _is_structured_alias(rel.split("/"))


def iter_recall_markdown(vault_root: Path, paths: Iterable[Path]) -> Iterator[Path]:
    """Yield ordinary-recall candidates, applying admission before page reads."""
    root = Path(vault_root)
    for path in paths:
        if is_recall_candidate(root, path):
            yield path


def recall_policy_identity(vault_root: Path) -> tuple[str, str]:
    """Static policy and local access-policy identities for semantic sidecars."""
    return RECALL_POLICY_VERSION, access.policy_fingerprint(Path(vault_root))


def recall_publication_policy_identity(vault_root: Path) -> tuple[str, str] | None:
    """Exact bounded policy identity suitable for sidecar publication."""
    snapshot = access.publication_policy_snapshot(Path(vault_root))
    if snapshot is None:
        return None
    return RECALL_POLICY_VERSION, snapshot.fingerprint


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


def _is_structured_descendant(parts: list[str]) -> bool:
    return len(parts) >= 3 and parts[0] == vault.kb_dirname() and parts[1] in {"Records", "Planning"}


def _is_structured_alias(parts: list[str]) -> bool:
    return (
        len(parts) >= 3
        and parts[0].casefold() == vault.kb_dirname().casefold()
        and parts[1].casefold() in {"records", "planning"}
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
        resolved = (root.joinpath(*parts)).resolve().relative_to(_resolved_root(root))
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
