"""init: bootstrap a fresh Knowledge Base scaffold into a vault.

A new user (no existing KB) needs the three load-bearing files — `index.md`,
`log.md`, `_Schema/SKILL.md` — plus the typed folder tree, before the writers
work. `init_vault` lays the whole structure down in one shot from the bundled
`_scaffold/`. The shipped `_Schema` is a genericized snapshot of the canonical
contract (placeholder projects/paths); adapt `project-keys.yaml` to your own.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import indexes
from .entity_types import ENTITY_TYPE_REGISTRY
from .kbdir import kb_dirname
from .vault import render_wikilinks_for_vault

_SCAFFOLD = Path(__file__).parent / "_scaffold"

# Typed folder tree laid down up-front. Deeper folders (Sources/Articles,
# Notes/Research/<Project>, …) are created on demand by the writers.
_FOLDERS = (
    "Sources",
    "Notes/Research",
    "Notes/Insights",
    "Notes/Failures",
    "Notes/Patterns",
    "Notes/Experiments",
    "Notes/Productions",
    *(f"Entities/{definition.folder}" for definition in ENTITY_TYPE_REGISTRY),
    "Evidence",
)

#: Scaffold entries the PRODUCT owns: the governance contract the agent follows.
#: `install-skill` redeploys its copy of these from this same source on every
#: upgrade, but `init` is skipped once a Knowledge Base exists, so the vault
#: copy stayed frozen at whatever version created the vault. The two then drift,
#: and both are live — `product_invoke` resolves the vault copy while agents
#: read the deployed one — which makes it a correctness problem, not clutter
#: (#488).
#:
#: Everything NOT matched here is per-vault configuration the user owns
#: (project-keys.yaml, relation-registry.yaml, semantic-language-registry.yaml,
#: traversal-profiles.yaml) or their own content, and is never overwritten.
_SHIPPED_SCHEMA_GLOBS = (
    "_Schema/SKILL.md",
    "_Schema/references/*.md",
    "_Schema/workflow-skills/*/SKILL.md",
)


def shipped_schema_sources() -> list[Path]:
    """Product-owned scaffold files, resolved from the bundled package."""
    found: list[Path] = []
    for pattern in _SHIPPED_SCHEMA_GLOBS:
        found.extend(sorted(_SCAFFOLD.glob(pattern)))
    return found


def refresh_shipped_schema(vault_root: Path) -> list[str]:
    """Re-deploy the product-owned governance docs into an existing vault.

    Returns the vault-relative paths actually rewritten — empty when already
    current, so a caller can say "nothing to do" honestly. Only files whose
    bytes differ are touched, so this is a no-op on a current vault and never
    churns mtimes for the file watcher.

    Deliberately NOT `init_vault(force=True)`: that overlays the whole scaffold,
    including the per-vault YAML registries the user is expected to edit.
    """
    vault_root = Path(vault_root)
    kb = vault_root / kb_dirname()
    if not kb.is_dir():
        return []
    refreshed: list[str] = []
    for src in shipped_schema_sources():
        dest = kb / src.relative_to(_SCAFFOLD)
        try:
            if dest.is_file() and dest.read_bytes() == src.read_bytes():
                continue
        except OSError:
            pass
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        refreshed.append(dest.relative_to(vault_root).as_posix())
    return refreshed


def init_vault(vault_root: Path, *, force: bool = False) -> dict:
    """Create `<vault_root>/Knowledge Base/` with the starter scaffold.

    Copies the bundled scaffold (index.md, log.md, _Schema/) and lays down the
    typed folder tree. Raises ``FileExistsError`` if `Knowledge Base/` already
    exists, unless ``force=True`` (which overlays the scaffold without deleting
    any existing files).
    """
    vault_root = Path(vault_root)
    kb = vault_root / kb_dirname()
    if kb.exists() and not force:
        raise FileExistsError(
            f"{kb} already exists. Pass force=True to overlay the scaffold "
            "(existing files are kept), or choose an empty vault."
        )

    created: list[str] = []
    for src in sorted(_SCAFFOLD.rglob("*")):
        scaffold_rel = src.relative_to(_SCAFFOLD)
        dest = kb / scaffold_rel
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and (
            not force or scaffold_rel == Path("Entities") / "index.md"
        ):
            continue
        shutil.copy2(src, dest)
        created.append(dest.relative_to(vault_root).as_posix())

    for folder in _FOLDERS:
        (kb / folder).mkdir(parents=True, exist_ok=True)

    entity_index = kb / "Entities" / "index.md"
    if entity_index.is_file():
        current = entity_index.read_text(encoding="utf-8")
        refreshed = indexes._refresh_entities_subindex_text(
            current,
            counts_by_type=indexes._count_entities(kb / "Entities"),
        )
        refreshed = render_wikilinks_for_vault(refreshed, vault_root)
        if refreshed != current:
            entity_index.write_text(refreshed, encoding="utf-8")

    from .activation_manifest import ensure_manifest, manifest_path

    activation_path = manifest_path(vault_root)
    activation_missing = not activation_path.exists()
    ensure_manifest(vault_root)
    if activation_missing:
        created.append(activation_path.relative_to(vault_root).as_posix())

    return {"vault": str(vault_root), "kb": str(kb), "created": created}
