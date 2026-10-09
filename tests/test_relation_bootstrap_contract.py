from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_bootstrap_compact_budget import COMPACT_BYTE_CEILING

from exomem import commands, relation_registry
from exomem.governance.principal import library_scope


def _write_registry(vault: Path, count: int) -> None:
    path = relation_registry.extension_registry_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "extensions": {
                    f"vault.synthetic_{index}": {
                        "parent": "relates_to",
                        "description": f"Synthetic reviewed meaning {index}.",
                        "direction": "directed",
                        "aliases": [f"synthetic_{index}"],
                    }
                    for index in range(count)
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_every_bootstrap_profile_exposes_bounded_relation_currency(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_registry(vault, 40)

    for profile in ("compact", "full", "diagnostics"):
        result = commands.op_bootstrap(
            vault, profile=profile, **({"section": "all"} if profile == "compact" else {})
        )
        relation = result["relation_vocabulary"]

        assert relation["contract_version"]
        assert relation["core_version"]
        if profile == "compact":
            assert "core_vocabulary" not in relation
        else:
            assert len(relation["core_vocabulary"]) == 33
        assert relation["extension_count"] == 40
        assert relation["extension_hash"] == relation_registry.load_registry(
            vault
        ).extension_hash
        assert relation["inventory_route"] == {
            "available": True,
            "route": {
                "tool": "connect_memory",
                "args": {"operation": "resolve-relation"},
            },
        }
        assert "extensions" not in relation


def test_compact_bootstrap_exposes_relation_routes_and_decision_choices(
    tmp_path: Path,
) -> None:
    with library_scope():
        result = commands.op_bootstrap(tmp_path / "vault", profile="compact", section="all")
    workflow = result["vocabulary_workflow"]

    resolve = workflow["relation_type"]["resolve"]
    assert resolve["available"] is True
    assert resolve["route"]["tool"] == "connect_memory"
    assert resolve["route"]["args"]["operation"] == "resolve-relation"

    propose = workflow["relation_type"]["propose"]
    assert propose["available"] is True
    assert propose["route"]["tool"] == "schema_memory"
    assert propose["route"]["args"]["operation"] == "propose-relation"
    assert propose["route"]["args"]["subject"] == "relations"

    apply = workflow["relation_type"]["apply"]
    assert apply["available"] is True
    assert apply["route"]["tool"] == "schema_memory"
    assert apply["route"]["args"]["operation"] == "save-relations"
    assert apply["route"]["args"]["subject"] == "relations"

    choices = workflow["decision"]["choice_contracts"]["relation-type/v1"]
    assert set(choices) == {"reuse", "propose-new", "generic", "no-edge", "defer"}
    assert set(choices["reuse"]["choice"]["required"]) == {"canonical"}
    assert set(choices["propose-new"]["choice"]["required"]) == {"canonical", "definition"}
    assert choices["generic"]["choice"] is None
    assert choices["no-edge"]["choice"] is None
    assert choices["defer"]["choice"] is None


def test_compact_entity_guidance_keeps_the_existing_v1_constraints(tmp_path: Path) -> None:
    with library_scope():
        rule = commands.op_bootstrap(tmp_path / "vault", profile="compact", section="all")["entity_registry"][
            "capture_rule"
        ].lower()

    for phrase in (
        "after durable work",
        "bounded",
        "new durable facts or relations",
        "requires why",
        "folder",
        "frontmatter",
    ):
        assert phrase in rule


@pytest.mark.parametrize(
    ("route_name", "kwargs", "guard"),
    [
        ("propose", {}, "INCOMPLETE_RELATION_PROPOSAL"),
        (
            "apply",
            {"proposal": {"upsert": {}}, "expected_hash": "a" * 64},
            "WHY_REQUIRED",
        ),
    ],
)
def test_relation_workflow_routes_reach_relation_schema_guards(
    tmp_path: Path,
    route_name: str,
    kwargs: dict[str, object],
    guard: str,
) -> None:
    with library_scope():
        route = commands.op_bootstrap(tmp_path / "vault", profile="compact", section="all")[
            "vocabulary_workflow"
        ]["relation_type"][route_name]["route"]

        with pytest.raises(ValueError, match=guard):
            commands.op_schema_memory(tmp_path / "vault", **route["args"], **kwargs)


def test_compact_bootstrap_does_not_inline_unbounded_extension_definitions(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_registry(vault, 200)

    reference = commands.op_bootstrap(vault, profile="compact", section="all")
    core = commands.op_bootstrap(vault, profile="compact")

    # The core is under its ceiling; the complete reference payload stays under the
    # bound the old compact ceiling set, and inlines no extension definition.
    assert len(json.dumps(core, ensure_ascii=False).encode("utf-8")) <= COMPACT_BYTE_CEILING
    encoded = json.dumps(reference, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 63_300
    assert "Synthetic reviewed meaning 199" not in encoded.decode("utf-8")


def test_generic_scaffold_and_workflow_skills_teach_relation_governance() -> None:
    root = Path("src/exomem/_scaffold/_Schema")
    paths = [
        root / "SKILL.md",
        root / "references/operations.md",
        *sorted((root / "workflow-skills").glob("*/SKILL.md")),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert "resolve-relation" in combined
    assert "propose-relation" in combined
    assert "save-relations" in combined
    assert "relates_to" in combined
    assert "no edge" in combined.lower()
    assert "hash" in combined.lower()
