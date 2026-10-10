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


def test_a_public_unit_filter_never_names_a_private_extension(private_instances, vault):
    """A private kind spelled like a public filter value is another entry."""
    from exomem import find as find_module
    from exomem import freshness, lexstore
    from exomem.governance import principal
    from exomem.vocabulary import instances, registry_spec

    path = "Knowledge Base/Notes/Insights/private-filter.md"
    (vault / path).parent.mkdir(parents=True, exist_ok=True)
    (vault / path).write_text(
        "---\ntype: insight\nproject: private-project\n"
        "exomem_id: 55555555-5555-4555-8555-555555555555\n---\n"
        "# Private filter\n\n## Container\n\n### Protocol\n"
        "- id: local-protocol\n\nPrivate procedure with a stable address.\n"
    )
    with principal.request_scope(private_instances("full")):
        overlay = instances.select(vault, registry_spec("categories"), SCOPE).overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("schema_version: 1\ncategories: {}\nkinds:\n  protocol:\n    description: Local procedure\n")
        assert all(freshness.rebaseline(vault).values())
        lexstore.ensure_fresh(vault)

        def kinds(filters):
            hits = find_module.find(
                vault, query="private procedure", scope="kb-only", mode="keyword",
                graph=False, result_level="unit", filters=filters, limit=10,
            )
            return [hit.kind for hit in hits]

        # Find is a public operation: its `protocol` names the public entry.
        assert kinds({"unit.kind": {"$eq": "protocol"}}) == []
        assert kinds({"unit.kind": {"$ne": "protocol"}}) == ["protocol"]


def test_vector_candidates_take_their_selected_meaning_before_the_result_limit(private_instances, vault):
    """Hidden or unrecognized occurrences spend no slot; unpublished coverage is never a miss."""
    import numpy as np

    from exomem import embedding_index, semantic_index
    from exomem.governance import principal
    from exomem.vocabulary import instances, registry_spec

    heading = "## Container\n\n### Protocol\n- id: local-protocol\n\nShared procedure text.\n"
    pages = {
        "private": ("Knowledge Base/Notes/Insights/vector-private.md", "project: private-project\n", heading),
        "public": ("Knowledge Base/Notes/Insights/vector-public.md", "", "- [decision] Public procedure.\n\n" + heading),
        "unpublished": ("Knowledge Base/Notes/Insights/vector-unpublished.md", "", "Plain prose only.\n"),
    }
    for number, (path, extra, body) in enumerate(pages.values(), start=6):
        (vault / path).parent.mkdir(parents=True, exist_ok=True)
        (vault / path).write_text(
            f"---\ntype: insight\n{extra}exomem_id: {number}6666666-6666-4666-8666-666666666666\n---\n"
            f"# Vector {number}\n\n{body}"
        )
    index = embedding_index.EmbeddingIndex(vault)
    query = np.eye(index.dim, dtype=np.float32)[0]
    with principal.request_scope(private_instances("full")):
        overlay = instances.select(vault, registry_spec("categories"), SCOPE).overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("schema_version: 1\ncategories: {}\nkinds:\n  protocol:\n    description: Local procedure\n")
        for name in ("private", "public"):
            state = semantic_index.build_parent_index_state(vault, pages[name][0])
            # Every protocol heading is nearer the query than the public decision.
            vectors = np.asarray([
                query if occurrence.kind_raw == "Protocol" else np.eye(index.dim, dtype=np.float32)[1] + 0.5 * query
                for occurrence in state.occurrences
            ], dtype=np.float32)
            index.upsert_semantic_units(state, vectors, 0.0)

        def search(allowed):
            incomplete: list[str] = []
            hits = index.search_semantic_units(
                query, 1, allowed_parent_paths=allowed, validate=False, incomplete_out=incomplete
            )
            return [(hit.parent_path, hit.unit_ref) for hit in hits], incomplete

        published = {pages["private"][0], pages["public"][0]}
        private_unit = semantic_index.selected_parent_index_state(vault, pages["private"][0]).document.units[0]
        assert private_unit.kind == "protocol"
        assert search(published) == ([(pages["private"][0], private_unit.unit_ref)], [])
        public_units = semantic_index.selected_parent_index_state(vault, pages["public"][0]).document.units
        assert [unit.category for unit in public_units] == ["decision"]

    with principal.request_scope(private_instances("limited")):
        # The hidden private page and the public page's unrecognized heading outrank
        # the decision, yet neither takes the single slot.
        assert search(published) == ([(pages["public"][0], public_units[0].unit_ref)], [])
        allowed = published | {pages["unpublished"][0]}
        assert search(allowed)[1] == [pages["unpublished"][0]]
        unpublished = semantic_index.build_parent_index_state(vault, pages["unpublished"][0])
        assert unpublished.occurrences == ()
        index.upsert_semantic_units(unpublished, np.zeros((0, index.dim), dtype=np.float32), 0.0)
        assert search(allowed) == ([(pages["public"][0], public_units[0].unit_ref)], [])


STORAGE_SCOPE = "01ARZ3NDEKTSV4RRFFQ69G5FAX"


@pytest.fixture
def storage_scope(configured_boundary, vault):
    """A Scope over the private instance's storage that the host may deny later.

    It exists before arming, so a later denial is a live configuration change.
    """
    config, _ = configured_boundary
    (vault / "Knowledge Base/_Governance/scopes/vocabulary-storage.yaml").write_text(
        f"governance_version: 1\nid: {STORAGE_SCOPE}\n"
        'paths: ["_Schema/private/**", "_Schema/history/private/**"]\n', encoding="utf-8",
    )
    data = json.loads(config.read_text())
    data["default_denied_scope_ids"].append(STORAGE_SCOPE)
    config.write_text(json.dumps(data))
    return STORAGE_SCOPE


def test_a_live_revocation_withholds_warm_private_units_but_keeps_the_page_readable(
    configured_boundary, storage_scope, private_instances, vault
):
    """Warm private meaning never outlives the caller's admission; the raw page stays useful.

    The host denies the full client the Scope over the private instance's storage.
    The private page stays readable, but its private definitions are withheld.
    This catches a warm projection that keeps serving the private interpretation,
    and a refusal that withholds the readable page along with its definitions.
    """
    from exomem import commands, epistemic_graph, freshness, lexstore, semantic_contract
    from exomem.governance import principal
    from exomem.vocabulary import instances, registry_spec

    path = "Knowledge Base/Notes/Insights/revocable-interpretation.md"
    source = (
        "---\ntype: insight\nproject: private-project\n"
        "exomem_id: 77777777-7777-4777-8777-777777777777\n---\n"
        "# Revocable interpretation\n\n- [decision] Ship the shared rollout.\n\n"
        "## Container\n\n### Protocol\n- id: local-protocol\n\nPrivate procedure with a stable address.\n"
    )
    (vault / path).parent.mkdir(parents=True, exist_ok=True)
    (vault / path).write_text(source)
    full = private_instances("full")

    def observe():
        with principal.request_scope(full):
            found = commands.op_find(
                vault, query="private procedure", mode="keyword", graph=False, result_level="unit", limit=10,
            )
            # A degraded answer arrives as an envelope around the same hits.
            hits = found["hits"] if isinstance(found, dict) else found
            return {
                "page": commands.op_read_memory(vault, path=path),
                "protocol": commands.op_read_memory(vault, path=path, unit_ref=units["protocol"]),
                "decision": commands.op_read_memory(vault, path=path, unit_ref=units["decision"]),
                "graph": commands.op_graph_context(vault, path=path, depth=1),
                "kinds": sorted(str(hit.get("kind")) for hit in hits),
                "degraded": found.get("degraded", []) if isinstance(found, dict) else [],
            }

    with principal.request_scope(full):
        overlay = instances.select(vault, registry_spec("categories"), SCOPE).overlay(vault)
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("schema_version: 1\ncategories: {}\nkinds:\n  protocol:\n    description: Local procedure\n")
        selected = semantic_contract.build_page_state(vault, path, source)
        units = {
            "protocol": next(unit.unit_ref for unit in selected.document.units if unit.kind == "protocol"),
            "decision": next(unit.unit_ref for unit in selected.document.units if unit.category == "decision"),
        }
        assert all(freshness.rebaseline(vault).values())
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
        lexstore.ensure_fresh(vault)
    warm = observe()
    assert warm["protocol"]["status"] == "found"
    assert any(node["kind"] == "protocol" for node in warm["graph"]["nodes"])
    assert "protocol" in warm["kinds"]

    config, _ = configured_boundary
    data = json.loads(config.read_text())
    data["clients"][1]["denied_scope_ids"] = [storage_scope]
    config.write_text(json.dumps(data))
    revoked = observe()

    assert "Private procedure with a stable address." in revoked["page"]["body"]
    assert revoked["decision"]["status"] == "found"
    assert revoked["protocol"]["status"] == "unavailable"
    assert not any(node["kind"] == "protocol" for node in revoked["graph"]["nodes"])
    assert "protocol" not in revoked["kinds"]
    # The withheld units make the lexical unit answer incomplete, never a proved miss.
    assert "semantic_units_lexical" in revoked["degraded"]


def test_a_hidden_page_with_a_private_status_never_changes_a_limited_clients_find_consumers(
    configured_boundary, vault, tmp_path, monkeypatch
):
    """Evolution, context and link suggestions rank only what their caller may see.

    Both vaults carry the same private status definition; only the second has a
    hidden page that uses it. Unadmitted, the hidden page's status refused the
    limited client's evolution and context and emptied its suggestions.
    """
    import shutil

    from exomem import commands, freshness, lexstore, state_migration
    from exomem.governance import principal
    from exomem.vocabulary import instances, registry_spec

    config, authenticate = configured_boundary
    data = json.loads(config.read_text())
    data["vocabulary"] = {
        "public": {"namespace": "Knowledge Base/_Schema/public", "history": "public", "overrides": {}},
        "private": {SCOPE: {"namespace": "Knowledge Base/_Schema/private", "history": "private"}},
        "destinations": {}, "selections": {},
    }
    data["capture_paths"] += ["Knowledge Base/_Schema/public", "Knowledge Base/_Schema/history/public"]
    config.write_text(json.dumps(data))
    public = "Knowledge Base/Notes/public.md"
    (vault / public).write_text(
        "---\nproject: public-project\ntype: insight\n"
        "exomem_id: 11111111-1111-4111-8111-111111111111\n---\n# Public\n\n"
        "- [decision] public information decision.\n\npublic information\n", encoding="utf-8")
    twin = tmp_path / "twin"
    shutil.copytree(vault, twin)
    (twin / "Knowledge Base/Notes/private.md").write_text(
        "---\ntype: insight\nproject: private-project\nstatus: secret-lifecycle\n"
        "exomem_id: 99999999-9999-4999-8999-999999999999\n---\n# Hidden\n\n"
        "- [decision] public information decision from the hidden page.\n\n"
        "Links [[Notes/public]].\n" + "public information\n" * 20, encoding="utf-8")
    observed = []
    for root in (vault, twin):
        monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
        stop = state_migration.assert_offline_migration_authority(source="hidden status twin")
        state_migration.arm_connector_boundary_offline(root, authority=stop)
        with principal.request_scope(authenticate("full")):
            overlay = instances.select(root, registry_spec("statuses"), SCOPE).overlay(root)
            overlay.parent.mkdir(parents=True, exist_ok=True)
            overlay.write_text("schema_version: 1\nentries:\n  secret-lifecycle:\n    attributes: {class: live}\n")
            freshness.rebaseline(root)
            lexstore.ensure_fresh(root)
        with principal.request_scope(authenticate("limited")):
            context = commands.op_connect_memory(root, operation="context", query="public information")
            observed.append({
                "evolution": commands.op_evolution(root, query="public information"),
                "review": commands.op_review_memory(root, mode="evolution", query="public information"),
                "context": {key: context[key] for key in ("available", "claims")},
                "suggestions": commands.op_connect_memory(
                    root, operation="suggest-links",
                    draft_title="public information", draft_body="public information decision",
                ),
            })

    assert observed[0] == observed[1]
    assert [item["path"] for item in observed[1]["suggestions"]] == [public]
    assert list(observed[1]["context"]["claims"]) == [public]


def test_a_limited_client_validates_a_planning_value_from_the_bound_public_overlay(private_instances, vault):
    """Planning admission checks the overlay the bound public instance reads.

    It used to admit the unbound default path, which a limited client cannot read,
    and so refused a value the public instance defines.
    """
    from exomem import planning, planning_values
    from exomem.governance import principal
    from exomem.vocabulary import instances

    overlay = instances.select(vault, planning_values.SPEC).overlay(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("schema_version: 1\nentries:\n  status.parked:\n    attributes: {class: open}\n")
    with principal.request_scope(private_instances("limited")):
        item = planning.normalize_item({"title": "Later", "status": "parked"}, vault_root=vault)
    assert item["status"] == "parked"


def test_a_legacy_armed_vault_refuses_an_unassigned_planning_value_and_keeps_shipped_ones(
    configured_boundary, vault
):
    """Version 1 assigns no instance, so the legacy overlay's values are unavailable.

    The selection error used to escape the Planning validator instead of its refusal.
    """
    from exomem import planning, planning_values, state_migration
    from exomem.governance import principal
    from exomem.structured_collections import CollectionError

    _, authenticate = configured_boundary
    overlay = planning_values.registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("schema_version: 1\nentries:\n  status.parked:\n    attributes: {class: open}\n")
    authority = state_migration.assert_offline_migration_authority(source="legacy planning values fixture")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(authenticate("full")):
        shipped = planning.normalize_item({"title": "Now", "status": "candidate"}, vault_root=vault)
        with pytest.raises(CollectionError) as refused:
            planning.normalize_item({"title": "Later", "status": "parked"}, vault_root=vault)
    assert shipped["status"] == "candidate"
    assert refused.value.code == "PLANNING_VALUES_UNAVAILABLE"



MEETING_TYPE_OVERLAY = (
    "schema_version: 1\nentries:\n  meeting-note:\n"
    "    attributes: {role: compiled, folder: Notes/Meetings}\n"
)


def _blocking_codes(vault, path, page_type, *, overwrite=False):
    """What the write gate blocks for a draft page with no semantic unit."""
    from exomem import commands

    validation = commands.op_manage_memory_file(
        vault, operation="create", path=path, content="# Sync\n\nThe crew met.\n",
        frontmatter={"type": page_type, "status": "active"}, overwrite=overwrite, validate_only=True,
    )
    return {finding["code"] for finding in validation["contract_result"]["blocking_findings"]}


def test_a_limited_client_classifies_a_note_type_from_the_bound_public_overlay(private_instances, vault):
    """Note-type admission checks the overlay the bound public instance reads.

    It used to admit the unbound default path, which a limited client cannot read,
    and so judged a type that the public instance defines as no type at all.
    """
    from exomem import note_types
    from exomem.governance import principal
    from exomem.vocabulary import instances

    overlay = instances.select(vault, note_types.SPEC).overlay(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(MEETING_TYPE_OVERLAY, encoding="utf-8")
    # A limited client creates only in capture paths, so it edits an existing page.
    page = "Knowledge Base/Notes/Meetings/2026-10-sync.md"
    (vault / page).parent.mkdir(parents=True, exist_ok=True)
    (vault / page).write_text("---\ntype: meeting-note\n---\n# Sync\n", encoding="utf-8")
    with principal.request_scope(private_instances("limited")):
        codes = _blocking_codes(vault, page, "meeting-note", overwrite=True)
    # The bound type is compiled, so the write gate asks for a semantic unit.
    assert "missing_semantic_unit" in codes


def test_a_legacy_armed_vault_refuses_an_unassigned_note_type_and_keeps_shipped_ones(
    configured_boundary, vault
):
    """Version 1 assigns no instance, so the legacy overlay's types are unavailable.

    The selection error used to escape the write gate instead of its refusal.
    """
    from exomem import note_types, state_migration
    from exomem.cli_ops import OpError
    from exomem.governance import principal

    _, authenticate = configured_boundary
    overlay = note_types.registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(MEETING_TYPE_OVERLAY, encoding="utf-8")
    authority = state_migration.assert_offline_migration_authority(source="legacy note types fixture")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(authenticate("full")):
        shipped = _blocking_codes(vault, "Knowledge Base/Notes/Insights/2026-10-sync.md", "insight")
        with pytest.raises(OpError) as refused:
            _blocking_codes(vault, "Knowledge Base/Notes/Meetings/2026-10-sync.md", "meeting-note")
    assert "missing_semantic_unit" in shipped
    assert refused.value.code == "NOTE_TYPE_DEFINITION_UNAVAILABLE"


def test_a_legacy_armed_vault_reports_unassigned_registries_instead_of_failing_bootstrap(
    configured_boundary, vault
):
    """Version 1 assigns no instance, so legacy definitions make their registries unavailable.

    The selection error used to escape the bootstrap's entity, relation and
    source-taxonomy blocks and fail the whole bootstrap.
    """
    from exomem import commands, entity_types, relation_registry, source_taxonomy, state_migration
    from exomem.governance import principal

    _, authenticate = configured_boundary
    for spec in (entity_types.SPEC, relation_registry.SPEC, source_taxonomy.KIND_SPEC):
        spec.overlay(vault).parent.mkdir(parents=True, exist_ok=True)
        spec.overlay(vault).write_text("schema_version: 1\n", encoding="utf-8")
    authority = state_migration.assert_offline_migration_authority(source="legacy registries fixture")
    state_migration.arm_connector_boundary_offline(vault, authority=authority)
    with principal.request_scope(authenticate("full")):
        bootstrap = commands.op_bootstrap(vault, profile="full")
        resolved = commands.op_schema_memory(
            vault, "resolve-entity-type", subject="entity-types", requested_type="person"
        )
    assert bootstrap["entity_registry"]["reason"] == "registry_assignment_required"
    assert bootstrap["source_taxonomy"]["reason"] == "registry_assignment_required"
    assert resolved == {
        "subject": "entity-types", "available": False, "reason": "registry_assignment_required",
    }


def test_a_limited_client_reads_entity_types_from_the_bound_public_overlay(private_instances, vault):
    """Bootstrap and schema_memory admit the overlay the bound public instance reads.

    They used to admit the unbound default path, which a limited client cannot
    read, and so withheld a type that the public instance defines.
    """
    from exomem import commands
    from exomem.governance import principal

    proposal = {"upsert": {"venue": {
        "label": "Venue", "guidance": "A place for recurring meetings.", "parent": "concept",
        "attributes": {"folder": "Venues"},
    }}}
    with principal.request_scope(private_instances("full")):
        inspected = commands.op_schema_memory(vault, "inspect", subject="entity-types")
        saved = commands.op_schema_memory(
            vault, "save", subject="entity-types", proposal=proposal,
            expected_hash=inspected["content_hash"], why="Public recurring places",
        )
    assert saved["valid"]
    with principal.request_scope(private_instances("limited")):
        bootstrap = commands.op_bootstrap(vault, profile="full")
        resolved = commands.op_schema_memory(
            vault, "resolve-entity-type", subject="entity-types", requested_type="venue"
        )
    assert "venue" in [item["id"] for item in bootstrap["entity_registry"]["types"]]
    assert [match["id"] for match in resolved["exact_matches"]] == ["venue"]
