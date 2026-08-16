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


def governance_sidecar_path(vault_root: Path) -> Path:
    """Per-machine governance compiled-policy sidecar path (inspection only)."""
    return vault_root / kb_dirname() / ".governance.sqlite"


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
        from .recall_policy import iter_recall_markdown
        from .vault import walk_vault_md

        yield from iter_recall_markdown(vault_root, walk_vault_md(vault_root))
        return

    from . import find as find_module
    from .recall_policy import iter_recall_markdown

    kb = kb_index_root(vault_root)
    if kb.is_dir():
        yield from iter_recall_markdown(vault_root, find_module._walk_md(kb))


def rel_to_vault(vault_root: Path, path: Path) -> str | None:
    """Vault-relative POSIX path, or `None` when `path` is not in the vault.

    The single place derived indexes decide vault membership. `relative_to` is
    purely lexical, so mixing spellings of one directory — a resolved root
    against an unresolved path, or the reverse — declares every file in the
    vault to be outside it. Each caller reacts by skipping the file, so the
    result is a silently empty index rather than an error. Symlinked roots are
    ordinary: macOS `/tmp` is a link to `/private/tmp`, and synced or mounted
    vaults sit behind one routinely.

    Callers build their paths from the same root they pass here, so the lexical
    comparison answers first and costs what it always did; resolving is the
    fallback for the mixed-spelling case that started this. Membership is a
    question about location, not about link structure: an in-vault name whose
    target lives elsewhere stays a member, because symlinking an external file
    into a vault is a way of putting it in the vault. Paths that do not exist
    still answer, because deletion sync asks about files that are already gone.
    """
    path = Path(path)
    root = Path(vault_root)
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        pass
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def is_embeddable_path(path: Path) -> bool:
    """True when a path is markdown content that derived indexes should consider."""
    if path.suffix.lower() != ".md":
        return False
    return path.name.lower() not in SKIP_MARKDOWN_NAMES
