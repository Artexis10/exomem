from __future__ import annotations

from pathlib import Path

import yaml

from exomem import knowledge_packs


def test_default_entity_types_accept_vault_defined_ids(tmp_path: Path) -> None:
    path = tmp_path / "Knowledge Base" / "_Schema" / "entity-types.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entity_types": {
                    "place": {
                        "folder": "Places",
                        "label": "Place",
                        "aliases": ["location"],
                        "capture_guidance": "A stable place identity.",
                        "parent": "concept",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    raw = knowledge_packs.list_builtin_packs()[0].copy()
    raw["default_entity_types"] = [*raw["default_entity_types"], "place"]

    pack = knowledge_packs.validate_pack_dict(raw, vault_root=tmp_path)

    assert "place" in pack.default_entity_types
