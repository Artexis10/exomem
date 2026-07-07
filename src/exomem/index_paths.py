"""Shared path contract for semantic and derived indexes."""

from __future__ import annotations

import os
from pathlib import Path

from .kbdir import kb_dirname


INDEX_SCOPES = ("kb", "vault")

# Navigation files that are generated summaries/activity feeds, not user content.
SKIP_MARKDOWN_NAMES = frozenset({"log.md", "index.md"})


def sidecar_path(vault_root: Path) -> Path:
    """Per-machine text embedding sidecar path."""
    return vault_root / kb_dirname() / ".embeddings.sqlite"


def clip_sidecar_path(vault_root: Path) -> Path:
    """Per-machine CLIP image/video vector sidecar path."""
    return vault_root / kb_dirname() / ".clip.sqlite"


def kb_index_root(vault_root: Path) -> Path:
    """Historical KB-only semantic-index root."""
    return vault_root / kb_dirname()


def index_scope() -> str:
    """Return the semantic-index scope: `"kb"` (default) or `"vault"`."""
    raw = (os.environ.get("EXOMEM_INDEX_SCOPE") or "").strip().lower()
    return "vault" if raw == "vault" else "kb"


def iter_index_markdown(vault_root: Path):
    """Yield markdown paths covered by the current semantic-index scope.

    The walk contract is intentionally shared by rebuild, incremental index, audit
    drift detection, and claim indexing. Scope chooses the root set only; callers
    still apply content eligibility (`is_embeddable_path`), access policy, and
    their own content-specific filters.
    """
    if index_scope() == "vault":
        from .vault import walk_vault_md

        yield from walk_vault_md(vault_root)
        return

    from . import find as find_module

    kb = kb_index_root(vault_root)
    if kb.is_dir():
        yield from find_module._walk_md(kb)


def is_embeddable_path(path: Path) -> bool:
    """True when a path is markdown content that derived indexes should consider."""
    if path.suffix.lower() != ".md":
        return False
    return path.name.lower() not in SKIP_MARKDOWN_NAMES
