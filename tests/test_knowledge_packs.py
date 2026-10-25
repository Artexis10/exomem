from __future__ import annotations

from pathlib import Path

import pytest
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


def _definition(pack_id: str) -> dict:
    return next(pack for pack in knowledge_packs.BUILTIN_PACKS if pack.id == pack_id).as_dict()


@pytest.mark.parametrize(
    "references,cite,code",
    [
        ({}, "{nowhere}", "INVALID_TEMPLATE"),
        # An import names no mode, so no rendering of the Records command carries it.
        (
            {"start": {"command": "record_memory", "arguments": {"action": "import"}}},
            "{start}",
            "INVALID_REFERENCE",
        ),
    ],
    ids=["cites-an-undefined-reference", "a-shape-cannot-spell-the-reference"],
)
def test_a_pack_reference_that_some_surface_cannot_spell_fails_validation(references, cite, code) -> None:
    """Rendering would raise on the surface that serves it, so the definition fails at load."""
    raw = _definition("personal-records")
    raw["references"] = {**raw["references"], **references}
    raw["agent_instructions"] = f"{raw['agent_instructions']} Also {cite}."

    with pytest.raises(knowledge_packs.PackValidationError) as refused:
        knowledge_packs.validate_pack_dict(raw)

    assert refused.value.code == code
