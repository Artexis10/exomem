from __future__ import annotations

from pathlib import Path

import yaml

from exomem import entity_types, indexes


def _proposal(type_id: str, folder: str, label: str) -> dict:
    return {
        "schema_version": 1,
        "entity_types": {
            type_id: {
                "folder": folder,
                "label": label,
                "aliases": [],
                "capture_guidance": f"A stable synthetic {label.lower()} identity.",
                "parent": "concept",
            }
        },
    }


def _write_registry(vault: Path, proposal: dict) -> None:
    path = vault / "Knowledge Base" / "_Schema" / "entity-types.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(proposal, sort_keys=False), encoding="utf-8")


def _write_entity(vault: Path, folder: str, type_id: str, name: str) -> None:
    path = vault / "Knowledge Base" / "Entities" / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: entity\ntitle: {name}\nentity_type: {type_id}\n"
        "status: active\n---\n"
        f"# {name}\n",
        encoding="utf-8",
    )


def test_entity_index_includes_extension_folders_and_rebuilds_on_registry_change(
    tmp_path: Path,
) -> None:
    _write_registry(tmp_path, _proposal("place", "Places", "Place"))
    _write_entity(tmp_path, "Places", "place", "Aster Hall")
    first_registry = entity_types.load_entity_types(tmp_path)
    first_counts = indexes._count_entities(
        tmp_path / "Knowledge Base" / "Entities", registry=first_registry
    )
    first_index = indexes._refresh_entities_subindex_text(
        "# Entities\n\n## By type\n",
        counts_by_type=first_counts,
        registry=first_registry,
    )
    entities_index = tmp_path / "Knowledge Base" / "Entities" / "index.md"
    entities_index.write_text("# Entities\n\n## By type\n", encoding="utf-8")
    planned, _top = indexes.compute_subindex_writes(tmp_path)
    planned_entities_index = next(
        write for write in planned if write.path == entities_index
    )

    assert first_counts["place"] == 1
    assert "Entities/Places/|Places" in first_index
    assert "Entities/Places/|Places" in planned_entities_index.content

    _write_registry(tmp_path, _proposal("venue", "Venues", "Venue"))
    _write_entity(tmp_path, "Venues", "venue", "Beryl Room")
    second_registry = entity_types.load_entity_types(tmp_path)
    second_counts = indexes._count_entities(
        tmp_path / "Knowledge Base" / "Entities", registry=second_registry
    )
    second_index = indexes._refresh_entities_subindex_text(
        first_index,
        counts_by_type=second_counts,
        registry=second_registry,
    )

    assert second_registry.extension_hash != first_registry.extension_hash
    assert "place" not in second_counts
    assert second_counts["venue"] == 1
    assert "Entities/Venues/|Venues" in second_index
