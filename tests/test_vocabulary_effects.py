from __future__ import annotations

from pathlib import Path

import pytest

from exomem import semantic_contract
from exomem.vocabulary_effects import CanonicalWriteImage, Effect, classify_additive_effects


def test_effect_details_are_immutable_snapshots_with_an_empty_default() -> None:
    default = Effect("entity.create", "entity.md")
    supplied = {"relation": "supports"}
    populated = Effect("edge.add", "note.md", details=supplied)
    supplied["relation"] = "links_to"

    assert default.as_dict()["details"] == {}
    assert populated.as_dict()["details"] == {"relation": "supports"}
    for effect in (default, populated):
        with pytest.raises(TypeError):
            effect.details["relation"] = "links_to"


def _image(path: str, before: str | None, after: str | None) -> CanonicalWriteImage:
    return CanonicalWriteImage(
        path=path,
        before=before.encode("utf-8") if before is not None else None,
        after=after.encode("utf-8") if after is not None else None,
    )


def _entity(title: str, *, entity_type: str = "organization") -> str:
    return (
        "---\n"
        "type: entity\n"
        "exomem_id: 11111111-1111-4111-8111-111111111111\n"
        f"title: {title}\n"
        f"entity_type: {entity_type}\n"
        "status: active\n"
        "---\n"
        f"# {title}\n"
    )


def test_classifies_a_new_canonical_entity_and_an_authored_edge(tmp_path: Path) -> None:
    entity_path = "Knowledge Base/Entities/Organizations/amber-guild.md"
    note_path = "Knowledge Base/Notes/connection.md"
    result = classify_additive_effects(
        tmp_path,
        (
            _image(entity_path, None, _entity("Amber Guild")),
            _image(
                note_path,
                None,
                "---\ntype: insight\nexomem_id: 22222222-2222-4222-8222-222222222222\n"
                "title: Connection\nstatus: active\n---\n"
                "# Connection\n\n- relates_to: [[Entities/Organizations/amber-guild]]\n",
            ),
        ),
        identity_paths_for_id=lambda _identifier: (),
    )

    assert result.state == "reviewed"
    assert [(effect.action, effect.path) for effect in result.effects] == [
        ("edge.add", note_path),
        ("entity.create", entity_path),
    ]
    assert len(result.digest) == 64


def test_classifies_only_new_type_and_relation_registry_definitions(tmp_path: Path) -> None:
    result = classify_additive_effects(
        tmp_path,
        (
            _image(
                "Knowledge Base/_Schema/entity-types.yaml",
                "schema_version: 1\nentity_types: {}\n",
                "schema_version: 1\nentity_types:\n  guild:\n"
                "    folder: Guilds\n    label: Guild\n    aliases: []\n"
                "    cue_nouns: []\n    capture_guidance: A durable group.\n",
            ),
            _image(
                "Knowledge Base/_Schema/relation-registry.yaml",
                "schema_version: 1\nextensions: {}\n",
                "schema_version: 1\nextensions:\n  guild.member:\n"
                "    description: Membership\n    parent: relates_to\n",
            ),
        ),
    )

    assert result.state == "reviewed"
    assert {(effect.action, effect.key) for effect in result.effects} == {
        ("entity_type.add", "guild"),
        ("relation_type.add", "guild.member"),
    }


def test_refuses_a_mixed_registry_addition_and_existing_entity_edit(tmp_path: Path) -> None:
    path = "Knowledge Base/Entities/Organizations/amber-guild.md"
    result = classify_additive_effects(
        tmp_path,
        (
            _image(path, _entity("Amber Guild"), _entity("Amber Guild") + "extra\n"),
            _image(
                "Knowledge Base/_Schema/entity-types.yaml",
                "schema_version: 1\nentity_types:\n  guild:\n"
                "    folder: Guilds\n    label: Guild\n    aliases: []\n"
                "    cue_nouns: []\n    capture_guidance: A durable group.\n",
                "schema_version: 1\nentity_types:\n  guild:\n"
                "    folder: Guilds\n    label: Guild revised\n    aliases: []\n"
                "    cue_nouns: []\n    capture_guidance: A durable group.\n  team:\n"
                "    folder: Teams\n    label: Team\n    aliases: []\n"
                "    cue_nouns: []\n    capture_guidance: A durable team.\n",
            ),
        ),
    )

    assert result.state == "mixed"
    assert [effect.action for effect in result.effects] == ["entity_type.add"]
    assert {reason.code for reason in result.reasons} == {
        "existing_entity_changed",
        "entity_type_definition_changed",
    }


def test_refuses_unknown_schema_and_duplicate_image_paths(tmp_path: Path) -> None:
    result = classify_additive_effects(
        tmp_path,
        (
            _image("Knowledge Base/_Schema/unknown.yaml", None, "value: true\n"),
            _image("Knowledge Base/Notes/a.md", None, "# A\n"),
            _image("Knowledge Base/Notes/a.md", "# A\n", "# B\n"),
        ),
    )

    assert result.state == "unsupported"
    assert result.effects == ()
    assert {reason.code for reason in result.reasons} == {
        "duplicate_image_path",
        "unknown_schema_change",
    }


def test_refuses_removing_an_existing_relation_type_definition(tmp_path: Path) -> None:
    result = classify_additive_effects(
        tmp_path,
        (
            _image(
                "Knowledge Base/_Schema/relation-registry.yaml",
                "schema_version: 1\nextensions:\n  guild.member:\n"
                "    description: Membership\n    parent: relates_to\n",
                None,
            ),
        ),
    )

    assert result.state == "non_additive"
    assert result.effects == ()
    assert [reason.code for reason in result.reasons] == ["relation_type_definition_changed"]


def test_digest_binds_the_exact_batch_bytes(tmp_path: Path) -> None:
    first = classify_additive_effects(
        tmp_path,
        (_image("Knowledge Base/Notes/a.md", None, "# First\n"),),
    )
    second = classify_additive_effects(
        tmp_path,
        (_image("Knowledge Base/Notes/a.md", None, "# Second\n"),),
    )

    assert first.digest != second.digest


def test_refuses_turning_an_existing_note_into_an_entity(tmp_path: Path) -> None:
    path = "Knowledge Base/Entities/Organizations/amber-guild.md"
    result = classify_additive_effects(
        tmp_path,
        (
            _image(
                path,
                "---\ntype: insight\ntitle: Amber Guild\nstatus: active\n---\n# Amber Guild\n",
                _entity("Amber Guild"),
            ),
        ),
        identity_paths_for_id=lambda _identifier: (),
    )

    assert result.state == "non_additive"
    assert [reason.code for reason in result.reasons] == ["existing_page_converted_to_entity"]


def test_classifies_frontmatter_and_rich_semantic_block_edges(tmp_path: Path) -> None:
    target = "Knowledge Base/Notes/target.md"
    source = "Knowledge Base/Notes/source.md"
    result = classify_additive_effects(
        tmp_path,
        (
            _image(
                target,
                None,
                "---\ntype: insight\nexomem_id: 33333333-3333-4333-8333-333333333333\n"
                "title: Target\nstatus: active\n---\n# Target\n",
            ),
            _image(
                source,
                None,
                "---\ntype: insight\nexomem_id: 44444444-4444-4444-8444-444444444444\n"
                "title: Source\nstatus: active\nrelated: [[Notes/target]]\n---\n"
                "# Source\n\n## Claim\n- id: c1\n- status: active\n"
                "- relations: supports: [[Notes/target]]\n\nBody.\n",
            ),
        ),
    )

    assert result.state == "reviewed"
    assert [effect.action for effect in result.effects] == ["edge.add", "edge.add"]
    assert {effect.details["relation"] for effect in result.effects} == {"links_to", "supports"}


def test_noop_project_keys_and_unresolved_endpoint_are_not_approved(tmp_path: Path) -> None:
    no_op = classify_additive_effects(
        tmp_path,
        (
            _image(
                "Knowledge Base/_Schema/project-keys.yaml",
                "projects: {}\n",
                "projects: {}\n",
            ),
        ),
    )
    unresolved = classify_additive_effects(
        tmp_path,
        (
            _image(
                "Knowledge Base/Notes/source.md",
                None,
                "---\ntype: insight\nexomem_id: 55555555-5555-4555-8555-555555555555\n"
                "title: Source\nstatus: active\n---\n# Source\n\n"
                "- supports: [[Notes/missing]]\n",
            ),
        ),
    )

    assert no_op.state == "reviewed"
    assert no_op.effects == ()
    assert unresolved.state == "unavailable"
    assert [reason.code for reason in unresolved.reasons] == ["edge_endpoint_unavailable"]


def test_refuses_invalid_utf8_and_duplicate_yaml_keys(tmp_path: Path) -> None:
    invalid = classify_additive_effects(
        tmp_path,
        (CanonicalWriteImage("Knowledge Base/Notes/binary.md", None, b"\xff"),),
    )
    duplicate_yaml = classify_additive_effects(
        tmp_path,
        (
            _image(
                "Knowledge Base/_Schema/entity-types.yaml",
                "schema_version: 1\nentity_types: {}\n",
                "schema_version: 1\nentity_types: {}\nentity_types: {}\n",
            ),
        ),
    )
    oversized = classify_additive_effects(
        tmp_path,
        (CanonicalWriteImage("Knowledge Base/Notes/large.md", None, b"x" * (1024 * 1024 + 1)),),
    )

    assert invalid.state == "unsupported"
    assert [reason.code for reason in invalid.reasons] == ["markdown_parse_invalid"]
    assert duplicate_yaml.state == "unsupported"
    assert [reason.code for reason in duplicate_yaml.reasons] == ["registry_invalid"]
    assert oversized.state == "unsupported"
    assert [reason.code for reason in oversized.reasons] == ["image_too_large"]


def test_refuses_relation_alias_edit_and_duplicate_or_unverified_entity_ids(tmp_path: Path) -> None:
    alias_edit = classify_additive_effects(
        tmp_path,
        (
            _image(
                "Knowledge Base/_Schema/relation-registry.yaml",
                "schema_version: 1\nextensions:\n  guild.member:\n"
                "    description: Membership\n    parent: relates_to\n    aliases: [member]\n",
                "schema_version: 1\nextensions:\n  guild.member:\n"
                "    description: Membership\n    parent: relates_to\n    aliases: [membership]\n",
            ),
        ),
    )
    duplicate_ids = classify_additive_effects(
        tmp_path,
        (
            _image("Knowledge Base/Entities/Organizations/a.md", None, _entity("A")),
            _image("Knowledge Base/Entities/Organizations/b.md", None, _entity("B")),
        ),
        identity_paths_for_id=lambda _identifier: (),
    )
    no_index = classify_additive_effects(
        tmp_path,
        (_image("Knowledge Base/Entities/Organizations/a.md", None, _entity("A")),),
    )

    assert alias_edit.state == "non_additive"
    assert [reason.code for reason in alias_edit.reasons] == ["relation_type_definition_changed"]
    assert duplicate_ids.state == "unsupported"
    assert [reason.code for reason in duplicate_ids.reasons] == ["duplicate_batch_identity"]
    assert no_index.state == "unavailable"
    assert [reason.code for reason in no_index.reasons] == ["identity_uniqueness_unavailable"]


def test_complete_effect_projection_does_not_hide_the_33rd_generic_wikilink(tmp_path: Path) -> None:
    source = "Knowledge Base/Notes/source.md"
    links = " ".join(f"[[Notes/target-{index}]]" for index in range(33))
    result = classify_additive_effects(
        tmp_path,
        (
            _image(
                source,
                None,
                "---\ntype: insight\nexomem_id: 66666666-6666-4666-8666-666666666666\n"
                "title: Source\nstatus: active\n---\n# Source\n\n" + links + "\n",
            ),
        ),
        resolver_entries=[
            (f"Knowledge Base/Notes/target-{index}.md", f"Target {index}")
            for index in range(32)
        ],
        identity_reader=lambda _path: "exomem://memory/77777777-7777-4777-8777-777777777777",
    )

    assert result.state == "unavailable"
    assert [reason.code for reason in result.reasons] == ["edge_endpoint_unavailable"]


def test_complete_effect_page_state_lifts_only_the_effect_projection_cap(tmp_path: Path) -> None:
    body = " ".join(f"[[Notes/target-{index}]]" for index in range(33))
    source = "---\ntype: insight\ntitle: Source\nstatus: active\n---\n# Source\n\n" + body

    ordinary = semantic_contract.build_page_state(tmp_path, "Knowledge Base/Notes/source.md", source)
    complete = semantic_contract.build_page_state(
        tmp_path,
        "Knowledge Base/Notes/source.md",
        source,
        complete_authored_effects=True,
    )

    assert len(ordinary.body_wikilinks) == 32
    assert len(complete.body_wikilinks) == 33
