"""Private registry operations keep their definitions and audit evidence private."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_connector_boundary import SCOPE
from test_connector_boundary import configured_boundary as configured_boundary

from exomem import entity_types, registry_history
from exomem.governance.principal import library_scope
from exomem.vocabulary import registry
from exomem.vocabulary.registry import content_hash


@pytest.fixture
def private_instances(configured_boundary, vault):
    from exomem import state_migration
    from exomem.vocabulary import registry_specs

    config, authenticate = configured_boundary
    public = {"namespace": "Knowledge Base/_Schema/public", "history": "public", "overrides": {}}
    for spec in registry_specs().values():
        if spec.overlay(vault).exists():
            public["overrides"][spec.stem] = {"overlay": spec.overlay(vault).relative_to(vault).as_posix()}
    data = json.loads(config.read_text())
    data["vocabulary"] = {
        "public": public,
        "private": {SCOPE: {"namespace": "Knowledge Base/_Schema/private", "history": "private"}},
        "destinations": {},
        "selections": {},
    }
    data["capture_paths"] += ["Knowledge Base/_Schema/public", "Knowledge Base/_Schema/history/public"]
    config.write_text(json.dumps(data))
    authority = state_migration.assert_offline_migration_authority(source="private vocabulary fixture")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    return authenticate


def test_authenticated_private_promotion_cannot_change_public_observations(private_instances, vault):
    from exomem import commands
    from exomem.governance import egress, principal

    limited, full = private_instances("limited"), private_instances("full")
    proposal = {"upsert": {"venue": {
        "label": "Venue", "guidance": "A place for recurring meetings.", "parent": "concept",
        "aliases": ["meeting-place"], "attributes": {"folder": "Venues"},
    }}}
    with principal.request_scope(limited):
        before = commands.op_schema_memory(vault, "inspect", subject="entity-types")
        proposed_before = commands.op_schema_memory(vault, "propose", subject="entity-types", proposal=proposal)
    shared_log = (vault / "Knowledge Base/log.md").read_bytes()
    with principal.request_scope(full):
        private = commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)
        saved = commands.op_schema_memory(
            vault, "save", subject="entity-types", registry_scope=SCOPE, proposal=proposal,
            expected_hash=private["content_hash"], why="Private recurring places",
        )
        assert saved["valid"]
        assert entity_types.load_entity_types(vault, registry_scope=SCOPE).resolve("meeting-place").folder == "Venues"
    assert (vault / "Knowledge Base/log.md").read_bytes() == shared_log
    with principal.request_scope(limited):
        assert commands.op_schema_memory(vault, "inspect", subject="entity-types") == before
        assert commands.op_schema_memory(vault, "propose", subject="entity-types", proposal=proposal) == proposed_before
        for path in (saved["saved"]["path"], saved["saved"]["history"]["snapshot"]):
            assert egress.release_level_for_path_only(vault, path) == egress.LEVEL_NONE
        history_note = Path(saved["saved"]["history"]["snapshot"]).parent / "notes.md"
        (vault / history_note).write_text("# Private history explanation\n", encoding="utf-8")
        assert not egress.quick_page_visible(vault, history_note.as_posix(), principal=limited)
        assert commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)["available"] is False
        public_saved = commands.op_schema_memory(
            vault, "save", subject="entity-types", proposal=proposal,
            expected_hash=before["content_hash"], why="Public recurring places",
        )
        assert public_saved["valid"]
    with principal.request_scope(full):
        private_after = commands.op_schema_memory(vault, "inspect", subject="entity-types", registry_scope=SCOPE)
        assert private_after["content_hash"] == saved["saved"]["content_hash"]
        history = commands.op_schema_memory(vault, "history", subject="entity-types", registry_scope=SCOPE)
        assert history["versions"][0]["why"] == "Private recurring places"
        restored = commands.op_schema_memory(
            vault, "restore", subject="entity-types", registry_scope=SCOPE,
            version=history["versions"][0]["version"], expected_hash=private_after["content_hash"],
            why="Retain only the private core",
        )
        assert restored["valid"]
        assert entity_types.load_entity_types(vault, registry_scope=SCOPE).resolve("venue") is None
        assert entity_types.load_entity_types(vault).resolve("venue") is not None
    assert b"Private recurring places" not in (vault / "Knowledge Base/log.md").read_bytes()
    assert b"Retain only the private core" not in (vault / "Knowledge Base/log.md").read_bytes()
    assert b"Public recurring places" in (vault / "Knowledge Base/log.md").read_bytes()
    assert shared_log != (vault / "Knowledge Base/log.md").read_bytes()


def test_prospective_selector_never_overrides_canonical_destination(private_instances, vault):
    from exomem.governance import principal
    from exomem.vocabulary import instances

    with principal.request_scope(private_instances("full")):
        with pytest.raises(registry.RegistryError, match="binding is ambiguous"):
            instances.page_scope(vault, "", {"type": "entity"}, prospective=True)
        assert instances.page_scope(
            vault, "", {"type": "entity"}, registry_scope="public", prospective=True
        ) is None
        assert instances.page_scope(
            vault, "", {"type": "entity", "project": "private-project"}, prospective=True
        ) == SCOPE
        with pytest.raises(registry.RegistryError, match="binding is ambiguous"):
            instances.page_scope(
                vault, "Knowledge Base/Entities/Venues/private.md",
                {"type": "entity", "project": "private-project"}, registry_scope="public",
            )


def test_entity_creation_selects_instance_before_type_derived_folder(private_instances, vault):
    from exomem import commands
    from exomem.governance import principal

    with principal.request_scope(private_instances("full")):
        for selector, folder in (("public", "Public Venues"), (SCOPE, "Private Venues")):
            inspected = commands.op_schema_memory(
                vault, "inspect", subject="entity-types", registry_scope=selector
            )
            saved = commands.op_schema_memory(
                vault, "save", subject="entity-types", registry_scope=selector,
                expected_hash=inspected["content_hash"], why="Keep instance-specific venue destinations",
                proposal={"upsert": {"venue": {
                    "label": "Venue", "guidance": "A stable meeting place.", "parent": "concept",
                    "aliases": ["meeting-place"],
                    "attributes": {"folder": folder, "optional_frontmatter": ["project"]},
                }}},
            )
            assert saved["valid"]
        with pytest.raises(ValueError, match="REGISTRY_UNAVAILABLE"):
            commands.op_connect_memory(
                vault, "create-entity", entity_type="meeting-place", name="Unselected venue",
                summary="A meeting place with no selected instance.",
            )
        public = commands.op_connect_memory(
            vault, "create-entity", entity_type="meeting-place", name="Public venue",
            summary="A place for public meetings.", registry_scope="public",
        )
        private = commands.op_connect_memory(
            vault, "create-entity", entity_type="meeting-place", name="Private venue",
            summary="A place for private meetings.", project="private-project", registry_scope=SCOPE,
        )
    assert Path(public["path"]).parent.name == "Public Venues"
    assert Path(private["path"]).parent.name == "Private Venues"


def test_legacy_armed_registry_requires_explicit_assignment(configured_boundary, vault):
    from exomem import commands, state_migration
    from exomem.governance import principal

    _, authenticate = configured_boundary
    authority = state_migration.assert_offline_migration_authority(source="legacy instance assignment fixture")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(authenticate("full")):
        result = commands.op_schema_memory(vault, "inspect", subject="entity-types")
    assert result == {"subject": "entity-types", "available": False, "reason": "registry_assignment_required"}


def test_explicit_public_history_does_not_override_an_unsafe_companion(private_instances, vault):
    from exomem.governance import egress, principal

    path = "Knowledge Base/_Schema/history/public/entity-types/retained.yaml"
    history = vault / path
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text("schema_version: 1\n", encoding="utf-8")
    with principal.request_scope(private_instances("limited")):
        assert egress.release_level_for_path_only(vault, path) == egress.LEVEL_FULL
        companion = vault / f"{path}.md"
        companion.symlink_to("missing-companion.md")
        assert egress.release_level_for_path_only(vault, path) == egress.LEVEL_NONE


def test_arming_requires_existing_history_assignment(private_instances, vault):
    from exomem import state_migration
    from exomem.governance import connector_boundary

    legacy = registry_history.history_dir(vault, entity_types.SPEC.stem)
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "retained.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    authority = state_migration.assert_offline_migration_authority(source="existing registry history fixture")
    with pytest.raises(registry.RegistryError, match="legacy history needs explicit assignment"):
        state_migration.arm_connector_boundary_offline(vault, authority=authority)
    assert connector_boundary.read_requirement(vault)["version"] == 2


def test_colliding_extensions_keep_their_own_cached_meaning(vault: Path) -> None:
    """A public promotion cannot redirect a private type's alias or folder."""
    public_path = entity_types.SPEC.overlay(vault)
    private_path = public_path.with_name("private-types.yaml")
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(
        "schema_version: 1\nentity_types:\n  venue:\n    parent: concept\n    label: Venue\n    capture_guidance: A place.\n"
        "    folder: Public Places\n    aliases: [meeting-place]\n", encoding="utf-8",
    )
    private_path.write_text(
        "schema_version: 1\nentity_types:\n  venue:\n    parent: concept\n    label: Venue\n    capture_guidance: A place.\n"
        "    folder: Private Places\n    aliases: [meeting-place]\n", encoding="utf-8",
    )
    private = replace(
        entity_types.SPEC, instance_id="private-scope", stem="private-types",
        overlay=lambda root: root / private_path.relative_to(vault),
    )
    public = registry.load(entity_types.SPEC, vault)
    hidden = registry.load(private, vault)
    assert public.typed.resolve("meeting-place").folder == "Public Places"
    assert hidden.typed.resolve("meeting-place").folder == "Private Places"
    public_path.write_text(
        public_path.read_text().replace("Public Places", "Public Venues"), encoding="utf-8"
    )
    registry.invalidate(vault, path=public_path)
    assert registry.load(entity_types.SPEC, vault).typed.resolve("venue").folder == "Public Venues"
    unchanged = registry.load(private, vault)
    assert unchanged.typed.resolve("venue").folder == "Private Places"
    assert unchanged.content_hash == hidden.content_hash
    assert unchanged.effective_digest == hidden.effective_digest


def test_private_history_preserves_reason_without_touching_shared_logs(vault: Path) -> None:
    """A private save must succeed without consuming or rotating the public log."""
    shared_log = vault / "Knowledge Base/log.md"
    # An unreadable shared log cannot become a dependency of private history.
    shared_log.write_bytes(b"\xff public log awaiting repair\n")
    previous = "schema_version: 1\n"
    rendered = "schema_version: 1\nentries: {}\n"
    overlay = vault / "Knowledge Base/_Schema/private-example.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(previous, encoding="utf-8")
    reason = "Private definition\nRetain the full reason."

    with library_scope():
        saved = registry_history.commit(
            vault,
            path=overlay,
            stem="private-example",
            rendered=rendered,
            operation="save",
            why=reason,
            before_hash=content_hash(previous),
            after_hash=content_hash(rendered),
            previous=previous,
            private=True,
        )
        history = registry_history.history_view(vault, stem="private-example")
        restored = registry_history.read_version(
            vault, stem="private-example", version=saved["version"]
        )

    assert overlay.read_text(encoding="utf-8") == rendered
    assert restored == previous
    assert history["versions"][0]["why"] == reason
    assert history["versions"][0]["before_hash"] == content_hash(previous)
    assert history["versions"][0]["after_hash"] == content_hash(rendered)
    assert shared_log.read_bytes() == b"\xff public log awaiting repair\n"


def test_private_heading_read_uses_its_instance_without_publishing_that_meaning(private_instances, vault):
    from exomem import commands, epistemic_graph, freshness, semantic_contract, semantic_index
    from exomem.governance import principal
    from exomem.vocabulary import instances, registry_spec

    path = "Knowledge Base/Notes/Insights/private-interpretation.md"
    source = (
        "---\ntype: insight\nproject: private-project\n"
        "exomem_id: 44444444-4444-4444-8444-444444444444\n---\n"
        "# Private interpretation\n\n## Container\n\n### Protocol\n"
        "- id: local-protocol\n\nPrivate procedure with a stable address.\n"
    )
    (vault / path).parent.mkdir(parents=True, exist_ok=True)
    (vault / path).write_text(source)
    with principal.request_scope(private_instances("full")):
        overlay = instances.select(vault, registry_spec("categories"), SCOPE).overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("schema_version: 1\ncategories: {}\nkinds:\n  protocol:\n    description: Local procedure\n")
        assert semantic_index.build_parent_index_state(vault, path).document.units == ()
        selected = semantic_contract.build_page_state(vault, path, source)
        assert [unit.kind for unit in selected.document.units] == ["protocol"]
        assert semantic_index.from_semantic_page_state(selected).document.units == ()
        assert all(freshness.rebaseline(vault).values())
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
        context = commands.op_graph_context(vault, path=path, depth=1)
        assert any(node["kind"] == "protocol" for node in context["nodes"]), context
        unit_ref = selected.document.units[0].unit_ref
        exact = commands.op_graph_context(vault, unit_ref=unit_ref, depth=0)
        assert exact["unit_status"] == "found"
        assert exact["seeds"][0]["metadata"]["unit_ref"] == unit_ref
    with principal.request_scope(private_instances("limited")):
        assert commands.op_graph_context(vault, path=path)["nodes"] == []
