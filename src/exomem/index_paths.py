"""Shared path contract for semantic and derived indexes."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from . import state_paths
from .kbdir import kb_dirname

INDEX_SCOPES = ("kb", "vault")

# Navigation files that are generated summaries/activity feeds, not user content.
SKIP_MARKDOWN_NAMES = frozenset({"log.md", "index.md"})


def sidecar_path(vault_root: Path) -> Path:
    """Per-machine text embedding sidecar path."""
    return state_paths.vault_state_dir(vault_root) / ".embeddings.sqlite"


def clip_sidecar_path(vault_root: Path) -> Path:
    """Per-machine CLIP image/video vector sidecar path."""
    return state_paths.vault_state_dir(vault_root) / ".clip.sqlite"


def governance_sidecar_path(vault_root: Path) -> Path:
    """Per-machine governance compiled-policy sidecar path (inspection only)."""
    return state_paths.vault_state_dir(vault_root) / ".governance.sqlite"


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


def index_markdown_admitter(vault_root: Path) -> Callable[[str], bool] | None:
    """A one-path twin of `iter_index_markdown`, or None where it has none.

    The predicate answers whether `iter_index_markdown` yields the page that
    `rel_to_vault` spells as `rel_path`, by visiting only that page's ancestry:
    the KB walk's own per-entry rule (`find_corpus.walk_md_admits`), then recall
    admission, exactly as the walk applies them. It is for a caller that needs
    the verdict for a few sidecar paths -- the write advisory's top-k candidates
    -- and used to walk the whole corpus per write to build a set it then probed
    a handful of times. One predicate shares its directory listings across
    calls, so use a fresh one per pass. The vault scope has no one-path twin
    yet: None tells the caller to walk.
    """
    if index_scope() == "vault":
        return None
    from .find_corpus import walk_md_admits
    from .recall_policy import is_recall_candidate

    root = Path(vault_root)
    kb = kb_index_root(root)
    listings: dict[Path, frozenset[str] | None] = {}

    def admits(rel_path: str) -> bool:
        path = root.joinpath(*str(rel_path).split("/"))
        if rel_to_vault(root, path) != rel_path:
            return False  # not a spelling the walk produces
        return walk_md_admits(kb, path, listings) and is_recall_candidate(root, path)

    return admits


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
