from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from exomem import epistemic_graph, relation_registry, traversal_profiles


def test_builtins_are_bounded_and_domain_distinct() -> None:
    profiles = traversal_profiles.builtin_profiles()
    assert set(profiles) == {"epistemic", "provenance", "causal", "decision", "all"}
    assert "support" in profiles["epistemic"].families
    assert "causality" not in profiles["epistemic"].families
    assert "causality" in profiles["causal"].families
    assert all(profile.builtin for profile in profiles.values())


def test_custom_profile_can_only_narrow_a_builtin() -> None:
    proposal = {"schema_version": 1, "profiles": {"lab-evidence": {
        "extends": "provenance", "add_relations": ["supports"],
        "remove_families": ["citation"], "direction": "outgoing",
        "max_nodes": 20,
    }}}
    loaded = traversal_profiles.load_profiles(proposal=proposal)
    assert loaded.findings == ()
    profile = loaded.resolve("lab-evidence")
    assert profile.extends == "provenance"
    assert profile.max_nodes == 20
    assert profile.direction == "outgoing"
    assert "supports" in profile.relations
    assert "citation" not in profile.families


def test_invalid_custom_profile_is_not_selectable() -> None:
    loaded = traversal_profiles.load_profiles(proposal={
        "schema_version": 1,
        "profiles": {"bad": {"extends": "missing", "max_nodes": 999}},
    })
    assert any(item["code"] == "invalid_parent" for item in loaded.findings)
    with pytest.raises(ValueError, match="INVALID_TRAVERSAL_PROFILE"):
        loaded.resolve("bad")


def test_parent_filter_expands_registered_extensions() -> None:
    registry = relation_registry.load_registry(proposal={
        "schema_version": 1,
        "extensions": {"science.replicates": {
            "parent": "supports", "description": "Reports independent reproduction"
        }},
    })
    profile = traversal_profiles.builtin_profiles(registry)["epistemic"]
    narrowed = traversal_profiles.narrow_relations(profile, ["supports"], registry)
    assert narrowed == frozenset({"supports", "science.replicates"})


def test_profile_save_uses_expected_hash(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    proposal = {"schema_version": 1, "profiles": {}}
    created = traversal_profiles.save_profiles(vault, proposal)
    with pytest.raises(ValueError, match="STALE_TRAVERSAL_PROFILES"):
        traversal_profiles.save_profiles(vault, proposal, expected_hash="old")
    updated = traversal_profiles.save_profiles(
        vault, proposal, expected_hash=created["content_hash"]
    )
    assert updated["created"] is False


def _write(vault: Path, rel: str, body: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_cross_file_extensions_are_precise_under_portable_profiles(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    registry = {
        "schema_version": 1,
        "extensions": {
            "science.replicates": {
                "parent": "supports", "description": "Reports independent reproduction"
            },
            "records.traces_to": {
                "parent": "derived_from", "description": "Traces a record to its source"
            },
            "systems.triggers": {
                "parent": "causes", "description": "Triggers a system transition"
            },
        },
    }
    _write(
        vault,
        "Knowledge Base/_Schema/relation-registry.yaml",
        yaml.safe_dump(registry, sort_keys=False),
    )
    for name in ("Study", "Source", "Event"):
        _write(vault, f"Knowledge Base/Notes/{name}.md", f"# {name}\n")
    source = """---
type: experiment
---
# Trial

## Finding
- relations: science.replicates: [[Knowledge Base/Notes/Study]]

The result reproduced.

- records.traces_to: [[Knowledge Base/Notes/Source]]
- systems.triggers: [[Knowledge Base/Notes/Event]]
- mystery.signal: [[Knowledge Base/Notes/Event]]
- See [[Knowledge Base/Notes/Study]]
"""
    source_path = "Knowledge Base/Notes/Trial.md"
    _write(vault, source_path, source)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    before_markdown = (vault / source_path).read_text(encoding="utf-8")
    before_edges = epistemic_graph.EpistemicGraphIndex(vault).edges()

    epistemic = epistemic_graph.graph_context(
        vault, path=source_path, traversal_profile="epistemic", depth=1
    )
    provenance = epistemic_graph.graph_context(
        vault, path=source_path, traversal_profile="provenance", depth=1
    )
    causal = epistemic_graph.graph_context(
        vault, path=source_path, traversal_profile="causal", depth=1
    )

    precise = next(edge for edge in epistemic["edges"] if edge["relation_type"] == "science.replicates")
    assert precise["parent_relation"] == "supports"
    assert precise["raw_relation"] == "science.replicates"
    assert precise["registry_status"] == "extension"
    assert {edge["relation_type"] for edge in provenance["edges"]} >= {"records.traces_to"}
    assert {edge["relation_type"] for edge in causal["edges"]} >= {"systems.triggers"}
    assert epistemic["profile"]["name"] == "epistemic"
    assert epistemic["excluded"]["unregistered"] == 1
    assert epistemic["warnings"][0]["examples"][0]["raw_relation"] == "mystery.signal"
    all_edges = epistemic_graph.EpistemicGraphIndex(vault).edges(source_path=source_path)
    assert sum(edge["registry_status"] == "unregistered" for edge in all_edges) == 1
    assert not any(edge["raw_relation"].lower() == "see" for edge in all_edges)
    assert (vault / source_path).read_text(encoding="utf-8") == before_markdown
    assert epistemic_graph.EpistemicGraphIndex(vault).edges() == before_edges


def test_registry_hash_drift_requires_rebuild_and_re_resolves_alias(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    proposal = {"schema_version": 1, "extensions": {"science.replicates": {
        "parent": "supports", "description": "Reports independent reproduction",
        "aliases": ["mirrors"],
    }}}
    _write(vault, "Knowledge Base/_Schema/relation-registry.yaml", yaml.safe_dump(proposal))
    _write(vault, "Knowledge Base/Notes/Target.md", "# Target\n")
    _write(vault, "Knowledge Base/Notes/Source.md", "# Source\n\n- mirrors: [[Knowledge Base/Notes/Target]]\n")
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    assert next(edge for edge in index.edges() if edge["raw_relation"] == "mirrors")["relation_type"] == "science.replicates"
    proposal["extensions"]["science.replicates"]["aliases"] = ["reproduces"]
    _write(vault, "Knowledge Base/_Schema/relation-registry.yaml", yaml.safe_dump(proposal))
    assert epistemic_graph.EpistemicGraphIndex(vault).available() is False
    assert "registry hash drift" in epistemic_graph.graph_drift(vault)[0]["reason"]
    rebuilt = epistemic_graph.EpistemicGraphIndex(vault)
    rebuilt.rebuild_all()
    changed = next(edge for edge in rebuilt.edges() if edge["raw_relation"] == "mirrors")
    assert changed["relation_type"] is None
    assert changed["registry_status"] == "unregistered"
