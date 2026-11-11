"""init: bootstrap a fresh Knowledge Base scaffold into a vault.

A new user (no existing KB) needs the three load-bearing files — `index.md`,
`log.md`, `_Schema/SKILL.md` — plus the typed folder tree, before the writers
work. `init_vault` lays the whole structure down in one shot from the bundled
`_scaffold/`. The shipped `_Schema` is a genericized snapshot of the canonical
contract (placeholder projects/paths); adapt `project-keys.yaml` to your own.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from . import indexes
from .entity_types import (
    ENTITY_TYPE_REGISTRY,
    extension_registry_path,
    load_entity_types,
)
from .kbdir import kb_dirname
from .vault import (
    PathGuard,
    PathGuardError,
    PlannedWrite,
    batch_atomic_write,
    read_guarded_text,
    render_wikilinks_for_vault,
    shipped_schema_target,
)

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
    "_Schema/examples/**/*.md",
    "_Schema/references/*.md",
    "_Schema/workflow-skills/*/SKILL.md",
    # The registry of those skills. Product-owned for the same reason they are,
    # and it has to travel with them: a reader resolving a skill path relative to
    # this file would otherwise look in the directory the skills just left. It
    # was also absent from this list before, which meant `refresh_shipped_schema`
    # never updated it and a vault's index could drift from the skills it names.
    "_Schema/workflow-skills/index.yaml",
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
    vault_root.mkdir(parents=True, exist_ok=True)
    kb = vault_root / kb_dirname()
    if not kb.is_dir():
        return []
    from . import workflow_contracts

    workflow_contracts.ensure_migration_marker(vault_root)
    refreshed: list[str] = []
    writes: list[PlannedWrite] = []
    for src in shipped_schema_sources():
        # `_Schema/SKILL.md` -> `<vault>/.exomem/schema/SKILL.md`. The scaffold
        # keeps its `_Schema/` prefix because that is the shape the claude.ai
        # `.skill` zip and `install-skill` both deploy; only the vault's copy
        # moves out of the note namespace (#488).
        relative = src.relative_to(_SCAFFOLD).as_posix().split("/", 1)[1]
        dest = shipped_schema_target(vault_root) / relative
        try:
            current, guard = read_guarded_text(vault_root, dest)
            if current == src.read_text(encoding="utf-8"):
                continue
        except FileNotFoundError:
            try:
                guard = PathGuard.capture(
                    vault_root, dest.relative_to(vault_root).as_posix(), leaf_policy="absent"
                )
            except PathGuardError as error:
                raise workflow_contracts.WorkflowContractError(
                    "WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE"
                ) from error
        except (OSError, UnicodeDecodeError, PathGuardError) as error:
            raise workflow_contracts.WorkflowContractError(
                "WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE"
            ) from error
        writes.append(PlannedWrite(dest, src.read_text(encoding="utf-8"), guard=guard))
        refreshed.append(dest.relative_to(vault_root).as_posix())
    if writes:
        try:
            batch_atomic_write(writes, vault_root=vault_root)
        except PathGuardError as error:
            raise workflow_contracts.WorkflowContractError(
                "WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE"
            ) from error
    return refreshed


def _store_mode(vault_root: Path) -> bool:
    from .vault_store import mode

    return mode.store_mode_active(vault_root)


def _commit_offline(vault_root: Path, operation: str, change_of, then=None) -> None:
    """Commit ``change_of(view)`` to a store-mode vault as the offline owner, in one transaction.

    The CLIs that overlay or reclaim schema pages run with the service stopped, so they take the
    route that ``store-mode migrate`` and ``rollback`` take: the owner's lease, the operator
    credential and the store's one writer. ``change_of`` reads the store under that writer and
    returns the change, or None when there is nothing to commit. ``then`` writes what the store
    does not own, after the commit and still as the owner: nothing is written before admission.
    """
    from .collection_store import admission
    from .vault_store import read, write

    def step(_session, _token, writer) -> None:
        with read.snapshot(vault_root) as view:
            change = change_of(view)
        if change is not None:
            write.commit(writer, change)
        if then is not None:
            then()

    admission.store_mode_route(Path(vault_root).resolve(), operation, step)


def _scaffold_plan(vault_root: Path) -> tuple[list[Path], list[tuple[Path, Path]]]:
    """The folders an overlay creates, and each scaffold file with where an overlay puts it.

    It writes nothing. The activation conventions are left out: the packaged ones are the base
    registry, not the add/drop grammar accepted at this vault-owned override path, so even a force
    overlay leaves an owner's override intact.
    """
    kb = vault_root / kb_dirname()
    # The product-owned markdown lands outside the note namespace; everything else -- index.md,
    # log.md, the typed tree, and the per-vault YAML registries that also live under `_Schema/` --
    # is the user's and stays in the Knowledge Base where they can see and edit it (#488).
    product_owned = {source.relative_to(_SCAFFOLD) for source in shipped_schema_sources()}
    folders: list[Path] = []
    plan: list[tuple[Path, Path]] = []
    for src in sorted(_SCAFFOLD.rglob("*")):
        scaffold_rel = src.relative_to(_SCAFFOLD)
        if scaffold_rel == Path("_Schema/activation-conventions.yaml"):
            continue
        if scaffold_rel in product_owned:
            dest = shipped_schema_target(vault_root) / scaffold_rel.as_posix().split("/", 1)[1]
        else:
            dest = kb / scaffold_rel
        if src.is_dir():
            # A directory that exists only to hold product-owned markdown is not created in the
            # Knowledge Base at all; its files carry their own `mkdir(parents=True)` at the new
            # location.
            if not any(str(candidate).startswith(str(scaffold_rel)) for candidate in product_owned):
                folders.append(dest)
            continue
        plan.append((src, dest))
    return folders, plan


def _overlay_in_store(vault_root: Path) -> dict:
    """``init --force`` on a store-mode vault, as the offline owner.

    Nothing is written before the owner's route admits the call. Then one commit puts each scaffold
    page that the store holds differently or not at all, and the migration marker and activation
    manifest that file mode would create when the vault lacks them; the projector writes their
    files afterwards, and nothing writes their Markdown here. An Entities index that exists is
    kept, as file mode keeps it, and its counts are refreshed from the stored pages in the same
    commit. What the store does not own (the folders, the product-owned copies under the dot
    folder) is written on disk after the commit, as file mode writes it.
    """
    from . import workflow_contracts
    from .activation_manifest import manifest_path, new_manifest
    from .vault_store import ownership, read, write

    kb = vault_root / kb_dirname()
    folders, files = _scaffold_plan(vault_root)
    pages: dict[str, Path] = {}
    copies: list[tuple[Path, Path]] = []
    for src, dest in files:
        rel = dest.relative_to(vault_root).as_posix()
        if ownership.classify(rel) is ownership.Ownership.PAGE:
            pages[rel] = src
        else:
            copies.append((src, dest))
    marker = workflow_contracts.migration_marker_path(vault_root).relative_to(vault_root).as_posix()
    manifest = manifest_path(vault_root).relative_to(vault_root).as_posix()
    entity_index = f"{kb_dirname()}/Entities/index.md"
    entities = f"{kb_dirname()}/Entities/"
    entity_registry = load_entity_types(vault_root)
    created: list[str] = []

    def overlay(view) -> write.StoreChange | None:
        current = view.bodies(pages)
        puts: dict[str, bytes] = {}
        for rel, src in pages.items():
            data = src.read_bytes()
            if (rel == entity_index and rel in current) or current.get(rel) == data:
                continue
            puts[rel] = data
        index = puts.get(entity_index, current.get(entity_index))
        if index is not None:
            text = index.decode("utf-8")
            counts = indexes.count_entity_pages(
                (meta.path[len(entities):] for meta in view.pages(entities)), entity_registry
            )
            refreshed = render_wikilinks_for_vault(
                indexes._refresh_entities_subindex_text(
                    text, counts_by_type=counts, registry=entity_registry
                ),
                vault_root,
            )
            if refreshed != text:
                puts[entity_index] = refreshed.encode("utf-8")
        created.extend(sorted(puts))
        # The marker comes first in file mode, before any scaffold change; here it is in the commit.
        for rel, missing in (
            (marker, workflow_contracts.new_migration_marker(vault_root)),
            (manifest, new_manifest(vault_root)),
        ):
            if missing is not None:
                puts[rel] = missing.encode("utf-8")
        if manifest in puts:
            created.append(manifest)
        if not puts:
            return None
        return write.StoreChange(
            write.Origin.GOVERNED,
            "exomem init --force",
            puts=tuple(write.PagePut(rel, body) for rel, body in sorted(puts.items())),
            expect={
                rel: write.Expect(sha256=hashlib.sha256(current[rel]).hexdigest())
                if rel in current
                else write.Expect(absent=True)
                for rel in puts
            },
        )

    def write_files() -> None:
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)
        for src, dest in copies:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            created.append(dest.relative_to(vault_root).as_posix())
        for folder in _FOLDERS:
            (kb / folder).mkdir(parents=True, exist_ok=True)
        # The registry is a page: the store says whether the vault holds it, not its file.
        registry = extension_registry_path(vault_root).relative_to(vault_root).as_posix()
        if read.page_exists(vault_root, registry):
            for definition in entity_registry.extensions.values():
                if definition.status == "active":
                    (kb / "Entities" / definition.folder).mkdir(parents=True, exist_ok=True)

    _commit_offline(vault_root, "exomem init --force", overlay, then=write_files)
    return {"vault": str(vault_root), "kb": str(kb), "created": created}


def init_vault(
    vault_root: Path,
    *,
    force: bool = False,
    initialize_state: bool = True,
) -> dict:
    """Create `<vault_root>/Knowledge Base/` with the starter scaffold.

    Copies the bundled scaffold (index.md, log.md, _Schema/) and lays down the
    typed folder tree. Raises ``FileExistsError`` if `Knowledge Base/` already
    exists, unless ``force=True`` (which overlays the scaffold without deleting
    any existing files). A genuinely new vault also receives its empty external
    state manifest. Callers building a renameable staging vault must pass
    ``initialize_state=False`` and initialize state only after publication,
    because the per-vault state key binds the final vault path.
    """
    vault_root = Path(vault_root)
    if force and _store_mode(vault_root):
        return _overlay_in_store(vault_root)
    vault_root.mkdir(parents=True, exist_ok=True)
    kb = vault_root / kb_dirname()
    fresh_vault = not kb.exists()
    if kb.exists() and not force:
        raise FileExistsError(
            f"{kb} already exists. Pass force=True to overlay the scaffold "
            "(existing files are kept), or choose an empty vault."
        )

    created: list[str] = []
    from . import workflow_contracts

    workflow_contracts.ensure_migration_marker(vault_root)
    folders, files = _scaffold_plan(vault_root)
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
    for src, dest in files:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and (not force or dest == kb / "Entities" / "index.md"):
            continue
        shutil.copy2(src, dest)
        created.append(dest.relative_to(vault_root).as_posix())

    for folder in _FOLDERS:
        (kb / folder).mkdir(parents=True, exist_ok=True)

    entity_registry = load_entity_types(vault_root)
    if extension_registry_path(vault_root).is_file():
        for definition in entity_registry.extensions.values():
            if definition.status == "active":
                (kb / "Entities" / definition.folder).mkdir(parents=True, exist_ok=True)

    entity_index = kb / "Entities" / "index.md"
    if entity_index.is_file():
        current = entity_index.read_text(encoding="utf-8")
        refreshed = indexes._refresh_entities_subindex_text(
            current,
            counts_by_type=indexes._count_entities(kb / "Entities", registry=entity_registry),
            registry=entity_registry,
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

    if fresh_vault and initialize_state:
        from . import state_migration

        authority = state_migration.assert_offline_migration_authority(
            source="fresh vault initialization",
        )
        state_migration.migrate_vault_state_offline(
            vault_root,
            authority=authority,
        )

    return {"vault": str(vault_root), "kb": str(kb), "created": created}


def reclaim_legacy_shipped_schema(vault_root: Path, *, apply: bool = False) -> dict:
    """Remove the pre-#488 copy of the shipped markdown from the note namespace.

    Explicit by design, and `apply=False` by default. An upgrade that silently
    deletes 265 KB from inside a user's Obsidian vault is the wrong behaviour
    even when every byte is reproducible from the package -- the vault is the
    artifact the product promises the user owns, and a deletion inside it should
    be something they asked for.

    Removes a legacy file ONLY when the new location holds one with identical
    bytes, and only for paths matching `_SHIPPED_SCHEMA_GLOBS`. A legacy file the
    user has edited is reported as declined rather than deleted, because the edit
    is the only copy of whatever they changed. Everything else under `_Schema/` --
    the YAML registries, `contracts/`, `relation-reviews/`, `private-skills/`, the
    activation manifest, and anything they put there themselves -- is out of
    scope and never inspected.
    """
    vault_root = Path(vault_root)
    if _store_mode(vault_root):
        return _reclaim_in_store(vault_root, apply=apply)
    legacy_root = vault_root / kb_dirname() / "_Schema"
    target_root = shipped_schema_target(vault_root)
    removed: list[str] = []
    declined: list[dict[str, str]] = []
    reclaimed_bytes = 0

    for source in shipped_schema_sources():
        relative = source.relative_to(_SCAFFOLD).as_posix().split("/", 1)[1]
        legacy = legacy_root / relative
        current = target_root / relative
        if not legacy.is_file():
            continue
        if not current.is_file():
            declined.append({"path": relative, "reason": "not yet deployed to .exomem/schema"})
            continue
        try:
            if legacy.read_bytes() != current.read_bytes():
                declined.append({"path": relative, "reason": "differs from the deployed copy"})
                continue
            size = legacy.stat().st_size
            if apply:
                legacy.unlink()
            removed.append(relative)
            reclaimed_bytes += size
        except OSError as error:
            declined.append({"path": relative, "reason": f"unreadable ({error})"})

    if apply:
        # Only directories the removals emptied, and only if they are empty --
        # never a directory still holding anything, which by definition is not
        # product-owned.
        for candidate in sorted(
            (path for path in legacy_root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                candidate.rmdir()
            except OSError:
                pass

    return {
        "applied": apply,
        "removed": removed,
        "declined": declined,
        "reclaimed_kb": round(reclaimed_bytes / 1024, 1),
    }


def _reclaim_in_store(vault_root: Path, *, apply: bool) -> dict:
    """``reclaim_legacy_shipped_schema`` on a store-mode vault, where the legacy copies are pages.

    The preview and the apply compare the stored bytes with the deployed copy, so a preview names
    exactly what the apply removes. The apply deletes the equal pages in one commit as the offline
    owner, each bound to the bytes it compared; the projector removes their files, and the folders
    that empties are left for it.
    """
    from .vault_store import read, write

    legacy = f"{kb_dirname()}/_Schema"
    target_root = shipped_schema_target(vault_root)
    relatives = [
        source.relative_to(_SCAFFOLD).as_posix().split("/", 1)[1]
        for source in shipped_schema_sources()
    ]
    report: dict = {}

    def judge(view) -> write.StoreChange | None:
        stored = view.bodies(f"{legacy}/{relative}" for relative in relatives)
        removed: list[str] = []
        declined: list[dict[str, str]] = []
        reclaimed = 0
        expect: dict[str, write.Expect] = {}
        for relative in relatives:
            body = stored.get(f"{legacy}/{relative}")
            if body is None:
                continue
            current = target_root / relative
            if not current.is_file():
                declined.append({"path": relative, "reason": "not yet deployed to .exomem/schema"})
                continue
            try:
                same = body == current.read_bytes()
            except OSError as error:
                declined.append({"path": relative, "reason": f"unreadable ({error})"})
                continue
            if not same:
                declined.append({"path": relative, "reason": "differs from the deployed copy"})
                continue
            removed.append(relative)
            reclaimed += len(body)
            expect[f"{legacy}/{relative}"] = write.Expect(
                sha256=hashlib.sha256(body).hexdigest()
            )
        report.update(
            applied=apply,
            removed=removed,
            declined=declined,
            reclaimed_kb=round(reclaimed / 1024, 1),
        )
        if not apply or not expect:
            return None
        return write.StoreChange(
            write.Origin.GOVERNED,
            "exomem reclaim-schema --apply",
            deletes=tuple(write.PageDelete(rel) for rel in expect),
            expect=expect,
        )

    if apply:
        _commit_offline(vault_root, "exomem reclaim-schema --apply", judge)
    else:
        with read.snapshot(vault_root) as view:
            judge(view)
    return report
