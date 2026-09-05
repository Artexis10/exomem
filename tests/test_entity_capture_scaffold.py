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
    assert 'connect_memory(operation="resolve-entity"' in text
    assert "edit_memory" in text
    assert 'connect_memory(operation="create-entity")' in text
    assert "single incidental mention" in text


def test_main_scaffold_treats_entities_as_stepping_stones_without_frozen_list() -> None:
    entrypoint = (SCHEMA / "SKILL.md").read_text(encoding="utf-8")
    assert "references/engagement.md" in entrypoint
    text = (SCHEMA / "references" / "engagement.md").read_text(encoding="utf-8")
    writing = (SCHEMA / "references" / "writing.md").read_text(encoding="utf-8")
    assert 'connect_memory(operation="resolve-entity"' in writing

    assert "durable recurring entity" in text
    assert "active entity registry" in text
    assert "selected knowledge packs" in text
    assert 'connect_memory(operation="resolve-entity"' in text
    assert 'connect_memory(operation="create-entity")' in text
    assert "single incidental mention" in text
    assert "person, organization, concept, library, decision" not in text


def test_people_pages_document_aliases_and_about_entity() -> None:
    entrypoint = (SCHEMA / "SKILL.md").read_text(encoding="utf-8")
    assert "references/recall.md" in entrypoint
    skill = (SCHEMA / "references" / "recall.md").read_text(encoding="utf-8")
    page_types = (SCHEMA / "references" / "page-types.md").read_text(encoding="utf-8")

    assert "### Referents" in skill
    assert "unresolved" in skill and "never guess" in skill
    people = page_types.split("### People", 1)[1].split("### ", 1)[0]
    assert "aliases" in people
    assert "about_entity" in people
