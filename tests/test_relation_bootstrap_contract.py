from __future__ import annotations

import json
from pathlib import Path

import yaml

from exomem import commands, relation_registry


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
        result = commands.op_bootstrap(vault, profile=profile)
        relation = result["relation_vocabulary"]

        assert relation["contract_version"]
        assert relation["core_version"]
        assert len(relation["core_vocabulary"]) == 28
        assert relation["extension_count"] == 40
        assert relation["extension_hash"] == relation_registry.load_registry(
            vault
        ).extension_hash
        assert relation["inventory_route"] == {
            "tool": "connect_memory",
            "args": {"operation": "resolve-relation"},
        }
        assert "extensions" not in relation


def test_compact_bootstrap_teaches_the_complete_truthful_relation_loop(
    tmp_path: Path,
) -> None:
    result = commands.op_bootstrap(tmp_path / "vault", profile="compact")
    relation = result["relation_vocabulary"]
    rendered = json.dumps(relation, sort_keys=True)

    for token in (
        "resolve-relation",
        "propose-relation",
        "save-relations",
        "relates_to",
        "no edge",
        "specific truthful",
        "new canonical key",
        "deprecat",
    ):
        assert token in rendered


def test_compact_bootstrap_does_not_inline_unbounded_extension_definitions(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_registry(vault, 200)

    compact = commands.op_bootstrap(vault, profile="compact")
    encoded = json.dumps(compact, ensure_ascii=False).encode("utf-8")

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
