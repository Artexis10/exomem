"""Shipped guidance keeps entity capture conservative and registry-driven."""

from __future__ import annotations

from pathlib import Path

SCHEMA = Path(__file__).parents[1] / "src" / "exomem" / "_scaffold" / "_Schema"


def test_capture_workflow_checks_existing_entity_before_create() -> None:
    text = (SCHEMA / "workflow-skills" / "exomem-capture" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "active entity registry" in text
    assert "selected knowledge packs" in text
    assert "exact name and aliases" in text
    assert "edit_memory" in text
    assert 'connect_memory(operation="create-entity")' in text
    assert "single incidental mention" in text


def test_main_scaffold_treats_entities_as_stepping_stones_without_frozen_list() -> None:
    text = (SCHEMA / "SKILL.md").read_text(encoding="utf-8")

    assert "durable recurring entity" in text
    assert "active entity registry" in text
    assert "selected knowledge packs" in text
    assert "exact name and aliases" in text
    assert 'connect_memory(operation="create-entity")' in text
    assert "single incidental mention" in text
    assert "person, organization, concept, library, decision" not in text
