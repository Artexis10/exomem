from __future__ import annotations

from pathlib import Path

import pytest

from exomem import relation_registry, semantic_blocks


def _proposal(**extensions):
    return {"schema_version": 1, "extensions": extensions}


def test_core_is_single_source_for_parser_and_graph() -> None:
    from exomem import epistemic_graph

    core = relation_registry.core_registry()
    assert len(core.core) == 24
    assert semantic_blocks.RELATION_TYPES == core.keys
    assert epistemic_graph.RELATION_TYPES == core.keys


def test_extension_alias_parent_and_scope_resolution() -> None:
    registry = relation_registry.load_registry(proposal=_proposal(
        **{"science.replicates": {
            "parent": "supports", "description": "Reports independent reproduction",
            "aliases": ["replicates"], "scope": {"page_types": ["experiment"]},
        }}
    ))
    resolved = registry.resolve("replicates", page_type="experiment", origin="semantic_relation")
    assert (resolved.canonical, resolved.parent, resolved.status) == (
        "science.replicates", "supports", "alias"
    )
    assert registry.resolve(
        "replicates", page_type="insight", origin="semantic_relation"
    ).status == "scope_violation"


def test_collisions_and_incomplete_semantics_are_stable_findings() -> None:
    registry = relation_registry.load_registry(proposal=_proposal(
        supports={"parent": "supports", "description": "bad"},
        **{"science.empty": {"parent": "not_core", "description": ""}},
    ))
    assert sorted(finding["code"] for finding in registry.findings) == [
        "collision", "invalid_key", "invalid_parent", "missing_description"
    ]


def test_unknown_semantic_relation_is_retained_but_validation_reports_it() -> None:
    body = "## Finding\n- relations: science.replicates: [[Target]]\n\nObserved."
    document = semantic_blocks.parse_semantic_blocks(body)
    assert document.blocks[0].relations[0].kind == "science.replicates"
    assert document.errors[0].code == "unsupported_relation"
    assert semantic_blocks.parse_semantic_blocks(body, validate=False).errors == []


def test_save_is_atomic_and_expected_hash_guarded(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    proposal = _proposal(**{"science.replicates": {
        "parent": "supports", "description": "Reports independent reproduction"
    }})
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
