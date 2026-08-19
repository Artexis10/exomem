"""Shipped governance markdown leaves the user's note namespace (#488).

`Knowledge Base/_Schema/` held 17 product-owned markdown files -- 265 KB in the
package, 404 KB as observed on a real vault -- inside the directory Obsidian and
every other indexer treat as notes. Exomem skipped them in its own index
(`VAULT_SCAN_SKIP_DIRS`); nothing else could, and a second tool ranked three
scaffold documents above every real note for a natural-language query.

Every other member of that skip set is a dot-directory. `_Schema` was the only
non-dot member, and the only one in the note namespace.

The constraint the move had to carry: `_is_vault` was literally
`_Schema/SKILL.md exists`, so that file is the vault sentinel for
`resolve_vault`, `product_invoke`, `doctor` and the hosted runtime.
"""

from __future__ import annotations

from pathlib import Path

from exomem import init as init_module
from exomem import vault as vault_module


def _legacy_vault(root: Path) -> Path:
    """A vault as it existed before this change: shipped markdown in the KB."""
    schema = root / "Knowledge Base" / "_Schema"
    (schema / "references").mkdir(parents=True)
    (schema / "SKILL.md").write_text("legacy contract", encoding="utf-8")
    (schema / "references" / "frontmatter.md").write_text("legacy fm", encoding="utf-8")
    (schema / "project-keys.yaml").write_text("projects: {}\n", encoding="utf-8")
    return root


def _migrated_vault(root: Path) -> Path:
    target = vault_module.shipped_schema_target(root)
    (target / "references").mkdir(parents=True)
    (target / "SKILL.md").write_text("current contract", encoding="utf-8")
    (root / "Knowledge Base" / "_Schema").mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------- resolution


def test_the_new_location_is_a_dot_directory(tmp_path: Path) -> None:
    """That is the whole mechanism.

    Obsidian, git and every other vault consumer already skip dot-directories, so
    the content inherits the treatment rather than exomem having to persuade each
    consumer separately -- which it cannot do for tools it does not ship.
    """
    target = vault_module.shipped_schema_target(tmp_path)

    assert target.relative_to(tmp_path).as_posix() == ".exomem/schema"
    assert target.relative_to(tmp_path).parts[0].startswith(".")


def test_an_unmigrated_vault_still_reads_from_the_legacy_location(tmp_path: Path) -> None:
    _legacy_vault(tmp_path)

    assert vault_module.shipped_schema_root(tmp_path) == (
        tmp_path / "Knowledge Base" / "_Schema"
    )


def test_a_migrated_vault_reads_from_the_new_location(tmp_path: Path) -> None:
    _migrated_vault(tmp_path)

    assert vault_module.shipped_schema_root(tmp_path) == vault_module.shipped_schema_target(
        tmp_path
    )


def test_the_new_location_wins_when_both_are_present(tmp_path: Path) -> None:
    """The state a refresh leaves behind, before anything is reclaimed.

    Reading the legacy copy here would serve exactly the bytes the refresh just
    superseded, which is the drift this change exists to end rather than move.
    """
    _legacy_vault(tmp_path)
    _migrated_vault(tmp_path)

    root = vault_module.shipped_schema_root(tmp_path)

    assert root == vault_module.shipped_schema_target(tmp_path)
    assert (root / "SKILL.md").read_text(encoding="utf-8") == "current contract"


# ----------------------------------------------------------- vault identity


def test_a_legacy_vault_is_still_a_vault(tmp_path: Path) -> None:
    """The failure mode of getting this wrong is total.

    `_is_vault` decides whether `resolve_vault`, `product_invoke`, `doctor` and
    the hosted runtime will speak to a directory at all, so a vault that has not
    migrated must not stop being one.
    """
    _legacy_vault(tmp_path)

    assert vault_module._is_vault(tmp_path) is True


def test_a_migrated_vault_is_still_a_vault(tmp_path: Path) -> None:
    _migrated_vault(tmp_path)

    assert vault_module._is_vault(tmp_path) is True


def test_a_directory_with_neither_sentinel_is_not_a_vault(tmp_path: Path) -> None:
    (tmp_path / "Knowledge Base").mkdir()

    assert vault_module._is_vault(tmp_path) is False


# ---------------------------------------------------------------- deployment


def test_a_fresh_vault_has_no_shipped_markdown_in_its_notes(tmp_path: Path) -> None:
    """The measurement #488 opens with, asserted rather than described."""
    init_module.init_vault(tmp_path)
    schema = tmp_path / "Knowledge Base" / "_Schema"

    stray = sorted(path.name for path in schema.rglob("*.md"))

    assert stray == []
    deployed = sorted(vault_module.shipped_schema_target(tmp_path).rglob("*.md"))
    assert len(deployed) == 17


def test_a_fresh_vault_keeps_its_registries_where_the_user_can_see_them(
    tmp_path: Path,
) -> None:
    """Only the shipped markdown moves.

    The YAML registries are per-vault configuration the user is expected to edit,
    they are small, and they are not markdown -- so they are not what pollutes a
    note index, and hiding them in a dot-directory would cost discoverability for
    no benefit.
    """
    init_module.init_vault(tmp_path)
    schema = tmp_path / "Knowledge Base" / "_Schema"

    present = sorted(path.name for path in schema.iterdir())

    assert "project-keys.yaml" in present
    assert "relation-registry.yaml" in present
    assert "traversal-profiles.yaml" in present


def test_refresh_deploys_to_the_new_location_and_removes_nothing(
    tmp_path: Path,
) -> None:
    """An upgrade may add, never delete. Reclaiming is its own explicit step."""
    init_module.init_vault(tmp_path)
    legacy = tmp_path / "Knowledge Base" / "_Schema"
    legacy.mkdir(parents=True, exist_ok=True)
    stale = legacy / "SKILL.md"
    stale.write_text("an older copy this vault still has", encoding="utf-8")

    init_module.refresh_shipped_schema(tmp_path)

    assert stale.is_file(), "a refresh deleted from the user's note namespace"
    assert (vault_module.shipped_schema_target(tmp_path) / "SKILL.md").is_file()


def test_the_skill_index_travels_with_the_skills_it_indexes(tmp_path: Path) -> None:
    """A registry left behind would point into a directory its entries left.

    It was also absent from the product-owned set before, so a vault's copy was
    never refreshed and could name skills whose shipped definitions had changed.
    """
    init_module.init_vault(tmp_path)
    target = vault_module.shipped_schema_target(tmp_path)

    assert (target / "workflow-skills" / "index.yaml").is_file()
    assert not (tmp_path / "Knowledge Base" / "_Schema" / "workflow-skills").exists()


def test_the_agent_is_told_where_the_skill_actually_is() -> None:
    """`bootstrap` hands the agent a vault-relative path to read.

    A stale path here is not cosmetic: it is the address the agent is told to
    open, so it would fail on every migrated vault while every test that only
    checked the filesystem stayed green.
    """
    from exomem import workflow_skills

    for entry in workflow_skills.bootstrap_entries():
        assert entry["path"].startswith(".exomem/schema/workflow-skills/")
        assert entry["path"].endswith("/SKILL.md")


# ----------------------------------------------------------------- reclaim


def test_reclaim_previews_before_it_deletes(tmp_path: Path) -> None:
    """`apply=False` is the default, and it must not touch the vault.

    A dry run is what makes the deletion something the user agrees to rather than
    something they discover.
    """
    init_module.init_vault(tmp_path)
    init_module.refresh_shipped_schema(tmp_path)
    legacy = tmp_path / "Knowledge Base" / "_Schema"
    legacy.mkdir(parents=True, exist_ok=True)
    duplicate = legacy / "SKILL.md"
    duplicate.write_bytes(
        (vault_module.shipped_schema_target(tmp_path) / "SKILL.md").read_bytes()
    )

    result = init_module.reclaim_legacy_shipped_schema(tmp_path)

    assert result["applied"] is False
    assert "SKILL.md" in result["removed"]
    assert result["reclaimed_kb"] > 0
    assert duplicate.is_file(), "a preview deleted a file"


def test_reclaim_removes_only_verified_duplicates(tmp_path: Path) -> None:
    init_module.init_vault(tmp_path)
    legacy = tmp_path / "Knowledge Base" / "_Schema"
    legacy.mkdir(parents=True, exist_ok=True)
    duplicate = legacy / "SKILL.md"
    duplicate.write_bytes(
        (vault_module.shipped_schema_target(tmp_path) / "SKILL.md").read_bytes()
    )

    result = init_module.reclaim_legacy_shipped_schema(tmp_path, apply=True)

    assert result["applied"] is True
    assert "SKILL.md" in result["removed"]
    assert not duplicate.exists()
    # And the content is still available from the location that now serves it.
    assert (vault_module.shipped_schema_target(tmp_path) / "SKILL.md").is_file()


def test_an_edited_legacy_file_is_declined_not_deleted(tmp_path: Path) -> None:
    """The edit is the only copy of whatever the user changed.

    Deleting it because the product could regenerate the ORIGINAL destroys work
    the product cannot regenerate at all.
    """
    init_module.init_vault(tmp_path)
    legacy = tmp_path / "Knowledge Base" / "_Schema"
    legacy.mkdir(parents=True, exist_ok=True)
    edited = legacy / "SKILL.md"
    edited.write_text("the user changed this", encoding="utf-8")

    result = init_module.reclaim_legacy_shipped_schema(tmp_path, apply=True)

    assert edited.read_text(encoding="utf-8") == "the user changed this"
    assert result["removed"] == []
    assert [entry["path"] for entry in result["declined"]] == ["SKILL.md"]


def test_reclaim_never_touches_anything_it_does_not_own(tmp_path: Path) -> None:
    """`_Schema/` also holds registries, contracts, reviews and user files.

    The glob is the whole safety property: a reclaim that walked the directory
    instead would delete a user's private skill because it happened to sit
    beside product content.
    """
    init_module.init_vault(tmp_path)
    schema = tmp_path / "Knowledge Base" / "_Schema"
    mine = schema / "my-own-notes-about-the-schema.md"
    mine.write_text("mine", encoding="utf-8")
    registry = schema / "project-keys.yaml"
    registry_before = registry.read_bytes()

    init_module.reclaim_legacy_shipped_schema(tmp_path, apply=True)

    assert mine.read_text(encoding="utf-8") == "mine"
    assert registry.read_bytes() == registry_before


def test_reclaim_declines_when_nothing_has_been_deployed_yet(tmp_path: Path) -> None:
    """Removing the only copy because the replacement is missing is data loss."""
    _legacy_vault(tmp_path)

    result = init_module.reclaim_legacy_shipped_schema(tmp_path, apply=True)

    assert result["removed"] == []
    assert (tmp_path / "Knowledge Base" / "_Schema" / "SKILL.md").is_file()
    assert any("not yet deployed" in entry["reason"] for entry in result["declined"])


# ------------------------------------------------------------- portability


def test_the_moved_schema_still_survives_a_hosted_export() -> None:
    """Moving a file must not also change whether it survives an export.

    `_is_unregistered_hidden_state` fails ALL dot-prefixed paths closed --
    deliberately, so a new machine-local cache cannot silently enter an archive.
    The shipped contract used to live under `Knowledge Base/` and was canonical
    by default; putting it in a dot-directory without registering it would have
    dropped it from every hosted export, and `_valid_vault_scaffold` requires
    `SKILL.md`, so the restored vault would then be refused outright.
    """
    from exomem import hosted_portability

    classification = hosted_portability.classify_artifact(
        ".exomem/schema/SKILL.md"
    )

    assert classification.artifact_class is hosted_portability.ArtifactClass.CANONICAL
    assert classification.rule_id == "portable-shipped-schema"


def test_an_unregistered_dot_directory_is_still_disposable() -> None:
    """The rule this one is threaded in front of has to keep working.

    Registering the schema must not turn the fail-closed default into a
    fail-open one for the next hidden sidecar somebody adds.
    """
    from exomem import hosted_portability

    classification = hosted_portability.classify_artifact(
        ".some-new-cache/state.bin"
    )

    assert classification.artifact_class is hosted_portability.ArtifactClass.DISPOSABLE_RUNTIME


def test_an_archive_from_either_layout_passes_the_scaffold_check() -> None:
    """A vault exported before the move must still restore after it."""
    from exomem import hosted_portability

    legacy = [{"path": "Knowledge Base/index.md"}, {"path": "Knowledge Base/_Schema/SKILL.md"}]
    migrated = [{"path": "Knowledge Base/index.md"}, {"path": ".exomem/schema/SKILL.md"}]

    hosted_portability._verify_required_scaffold(legacy)
    hosted_portability._verify_required_scaffold(migrated)


def test_the_real_schema_reader_works_on_both_layouts(tmp_path: Path) -> None:
    """`load_source_schema` parses the references, and it is a real read.

    Routing the resolver is not the same as proving the consumer uses it: a
    reader that kept its own hard-coded path would still pass every resolver
    test while failing on every vault of the other shape.
    """
    from exomem import schema as schema_module

    init_module.init_vault(tmp_path)
    migrated = schema_module.load_source_schema(tmp_path)
    assert migrated.source_types

    # Now make the same vault look pre-#488: references only in the old place.
    target = vault_module.shipped_schema_target(tmp_path)
    legacy = tmp_path / "Knowledge Base" / "_Schema"
    (legacy / "references").mkdir(parents=True, exist_ok=True)
    for name in ("frontmatter.md", "page-types.md"):
        (legacy / "references" / name).write_bytes((target / "references" / name).read_bytes())
    (legacy / "SKILL.md").write_bytes((target / "SKILL.md").read_bytes())
    import shutil

    shutil.rmtree(target)

    assert vault_module.shipped_schema_root(tmp_path) == legacy
    legacy_schema = schema_module.load_source_schema(tmp_path)
    assert legacy_schema.source_types == migrated.source_types


def test_the_hosted_scaffold_check_accepts_both_layouts(tmp_path: Path) -> None:
    """It gates whether the hosted runtime will serve a vault at all."""
    from exomem import hosted_runtime

    init_module.init_vault(tmp_path)
    assert hosted_runtime._valid_vault_scaffold(tmp_path) is True

    target = vault_module.shipped_schema_target(tmp_path)
    legacy = tmp_path / "Knowledge Base" / "_Schema"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "SKILL.md").write_bytes((target / "SKILL.md").read_bytes())
    import shutil

    shutil.rmtree(target)

    assert hosted_runtime._valid_vault_scaffold(tmp_path) is True


def test_the_new_location_is_skipped_by_the_vault_wide_walk(tmp_path: Path) -> None:
    """Otherwise the move relocates the pollution instead of removing it.

    `_Schema` was in `VAULT_SCAN_SKIP_DIRS` because `find(scope="vault")` reaches
    through `walk_vault_md`. Writing the same markdown to a path that set does
    not cover would leave exomem's own vault-wide search ranking the shipped
    contract above real notes -- the #488 symptom, moved rather than fixed.
    """
    init_module.init_vault(tmp_path)
    target = vault_module.shipped_schema_target(tmp_path)
    assert list(target.rglob("*.md")), "fixture is empty; the assertion would be vacuous"

    walked = {
        path.relative_to(tmp_path).as_posix()
        for path in vault_module.walk_vault_md(tmp_path)
    }
    assert not [rel for rel in walked if rel.startswith(vault_module.SHIPPED_SCHEMA_DIRNAME)]


def test_the_incremental_patcher_skips_the_new_location_too(tmp_path: Path) -> None:
    """The event-driven counterpart of the full walk, and it must agree with it.

    `in_excluded_scan_dir` is what the watcher consults per path. A path the full
    rebuild skips but the patcher indexes is the exact bypass that predicate
    exists to prevent.
    """
    assert vault_module.in_excluded_scan_dir(".exomem/schema/SKILL.md") is True
    assert vault_module.in_excluded_scan_dir(".exomem/schema/references/frontmatter.md") is True
    assert vault_module.in_excluded_scan_dir("Knowledge Base/Notes/real-note.md") is False
