from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from exomem import markdown_relations, relation_registry, semantic_blocks


def _proposal(**extensions):
    return {"schema_version": 1, "extensions": extensions}


def test_core_is_single_source_for_parser_and_graph() -> None:
    from exomem import epistemic_graph

    core = relation_registry.core_registry()
    assert len(core.core) == 25
    assert markdown_relations.RELATION_TYPES == core.keys
    assert semantic_blocks.RELATION_TYPES == core.keys
    assert epistemic_graph.RELATION_TYPES == core.keys


def test_legacy_relation_contract_matches_static_golden() -> None:
    golden = yaml.safe_load(
        (Path(__file__).parent / "golden" / "relation_compatibility.yaml").read_text(
            encoding="utf-8"
        )
    )
    actual = {
        key: {
            "direction": definition.direction,
            **({"inverse": definition.inverse} if definition.inverse else {}),
            "origins": sorted(definition.origins),
        }
        for key, definition in relation_registry.core_registry().core.items()
    }
    assert actual == golden["relations"]
    assert golden["graph_context_default_profile"] == "all"


def test_extension_alias_parent_and_scope_resolution() -> None:
    registry = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "science.replicates": {
                    "parent": "supports",
                    "description": "Reports independent reproduction",
                    "aliases": ["replicates"],
                    "scope": {"page_types": ["experiment"]},
                }
            }
        )
    )
    resolved = registry.resolve("replicates", page_type="experiment", origin="semantic_relation")
    assert (resolved.canonical, resolved.parent, resolved.status) == (
        "science.replicates",
        "supports",
        "alias",
    )
    assert (
        registry.resolve("replicates", page_type="insight", origin="semantic_relation").status
        == "scope_violation"
    )


def test_collisions_and_incomplete_semantics_are_stable_findings() -> None:
    registry = relation_registry.load_registry(
        proposal=_proposal(
            supports={"parent": "supports", "description": "bad"},
            **{"science.empty": {"parent": "not_core", "description": ""}},
        )
    )
    assert sorted(finding["code"] for finding in registry.findings) == [
        "collision",
        "invalid_key",
        "invalid_parent",
        "missing_description",
    ]


def test_unknown_semantic_relation_is_retained_but_validation_reports_it() -> None:
    body = "## Finding\n- relations: science.replicates: [[Target]]\n\nObserved."
    document = semantic_blocks.parse_semantic_blocks(body)
    assert document.blocks[0].relations[0].kind == "science.replicates"
    assert document.errors[0].code == "unsupported_relation"
    assert semantic_blocks.parse_semantic_blocks(body, validate=False).errors == []


def test_canonical_markdown_relations_use_registry_extensions_and_retain_unknowns(
    tmp_path: Path,
) -> None:
    from exomem import epistemic_graph

    vault = tmp_path / "vault"
    relation_registry.save_registry(
        vault,
        _proposal(
            **{
                "science.replicates": {
                    "parent": "supports",
                    "description": "Reports independent reproduction",
                }
            }
        ),
    )
    source = vault / "Knowledge Base/Notes/source.md"
    target = vault / "Knowledge Base/Notes/target.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Target\n", encoding="utf-8")
    source.write_text(
        "# Source\n\n## Relations\n"
        "- science.replicates [[Knowledge Base/Notes/target]]\n"
        "- science.unreviewed [[Knowledge Base/Notes/target]]\n",
        encoding="utf-8",
    )

    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    edges = index.edges(source_path="Knowledge Base/Notes/source.md")

    extension = next(edge for edge in edges if edge["raw_relation"] == "science.replicates")
    assert (extension["relation_type"], extension["parent_relation"]) == (
        "science.replicates",
        "supports",
    )
    assert (extension["origin"], extension["registry_status"]) == (
        "markdown_relation",
        "extension",
    )
    unknown = next(edge for edge in edges if edge["raw_relation"] == "science.unreviewed")
    assert unknown["relation_type"] is None
    assert (unknown["origin"], unknown["registry_status"]) == (
        "markdown_relation",
        "unregistered",
    )
    assert not any(edge["origin"] == "wikilink" for edge in edges)


def test_save_is_atomic_and_expected_hash_guarded(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    proposal = _proposal(
        **{
            "science.replicates": {
                "parent": "supports",
                "description": "Reports independent reproduction",
            }
        }
    )
    created = relation_registry.save_registry(vault, proposal)
    path = vault / created["path"]
    before = path.read_bytes()
    with pytest.raises(ValueError, match="STALE_RELATION_REGISTRY"):
        relation_registry.save_registry(vault, proposal, expected_hash="stale")
    assert path.read_bytes() == before
    saved = relation_registry.save_registry(vault, proposal, expected_hash=created["content_hash"])
    assert saved["previous_hash"] == created["content_hash"]


def test_observed_relation_cannot_be_deleted() -> None:
    with pytest.raises(ValueError, match="OBSERVED_RELATION_DELETION"):
        relation_registry.save_registry(
            Path("/unused"), _proposal(), observed_keys={"science.replicates"}
        )


def test_replacement_and_inverse_cycles_are_rejected() -> None:
    replacement_cycle = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "science.old_a": {
                    "parent": "supports",
                    "description": "Old A",
                    "status": "deprecated",
                    "replaced_by": "science.old_b",
                },
                "science.old_b": {
                    "parent": "supports",
                    "description": "Old B",
                    "status": "deprecated",
                    "replaced_by": "science.old_a",
                },
            }
        )
    )
    assert any(item["code"] == "relation_cycle" for item in replacement_cycle.findings)
    inverse_cycle = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "science.a": {"parent": "supports", "description": "A", "inverse": "science.b"},
                "science.b": {"parent": "supports", "description": "B", "inverse": "science.c"},
                "science.c": {"parent": "supports", "description": "C", "inverse": "science.a"},
            }
        )
    )
    assert any(item["code"] == "relation_cycle" for item in inverse_cycle.findings)


def test_invalid_node_kind_and_active_replacement_are_rejected() -> None:
    registry = relation_registry.load_registry(
        proposal=_proposal(
            **{
                "science.new": {"parent": "supports", "description": "New"},
                "science.current": {
                    "parent": "supports",
                    "description": "Current",
                    "replaced_by": "science.new",
                    "source_kinds": ["not valid"],
                },
            }
        )
    )
    codes = {item["code"] for item in registry.findings}
    assert {"invalid_node_kind", "invalid_replacement"} <= codes
