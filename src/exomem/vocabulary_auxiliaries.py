"""Exact code-owned auxiliary declarations for the vocabulary writer gate."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROLES = frozenset({"relation-review", "lifecycle-review", "source-backref", "index", "operation-log"})
_SEAL = object()


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _relative(root: Path, path: Path) -> str:
    try:
        return Path(path).absolute().relative_to(Path(root).absolute()).as_posix()
    except ValueError as exc:
        raise ValueError("VOCABULARY_AUXILIARY_PATH_INVALID") from exc


@dataclass(frozen=True, slots=True)
class DerivedAuxiliary:
    role: str
    path: str
    before_sha256: str | None
    after_sha256: str


@dataclass(frozen=True, slots=True)
class DerivedAuxiliaryManifest:
    root: Path
    primary_path: str
    primary_after_sha256: str
    entries: tuple[DerivedAuxiliary, ...]
    _seal: object = field(repr=False, compare=False, default=None)


def seal(
    vault_root: Path,
    *,
    primary: Any,
    derived: Iterable[tuple[str, Any]],
) -> DerivedAuxiliaryManifest:
    """Bind exact guarded derived outputs to one primary planned write."""
    root = Path(vault_root).resolve()
    if not isinstance(getattr(primary, "content", None), str):
        raise ValueError("VOCABULARY_AUXILIARY_CONTENT_INVALID")
    primary_path = _relative(root, Path(primary.path))
    entries: list[DerivedAuxiliary] = []
    for role, write in derived:
        guard = getattr(write, "guard", None)
        if role not in _ROLES or not isinstance(getattr(write, "content", None), str) or guard is None:
            raise ValueError("VOCABULARY_AUXILIARY_INVALID")
        path = _relative(root, Path(write.path))
        if path == primary_path:
            raise ValueError("VOCABULARY_AUXILIARY_PRIMARY_COLLISION")
        if guard.leaf_policy == "absent":
            before = None
        elif guard.leaf_policy == "content" and isinstance(guard.expected_content_hash, str):
            before = guard.expected_content_hash
        else:
            raise ValueError("VOCABULARY_AUXILIARY_GUARD_INVALID")
        after = _hash(write.content)
        # The batch seam suppresses byte-identical planned writes. A claim for
        # one would have no staged image to bind, so omit it before sealing.
        if before == after:
            continue
        entries.append(DerivedAuxiliary(role, path, before, after))
    if len({item.path for item in entries}) != len(entries):
        raise ValueError("VOCABULARY_AUXILIARY_DUPLICATE")
    return DerivedAuxiliaryManifest(root, primary_path, _hash(primary.content), tuple(entries), _SEAL)
