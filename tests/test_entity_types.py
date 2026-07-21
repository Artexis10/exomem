"""The entity contract has one immutable, alias-aware registry."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import get_args

import pytest

from exomem import commands, entity_candidates, entity_types


def test_core_entity_registry_is_complete_unique_and_immutable() -> None:
    definitions = entity_types.ENTITY_TYPE_REGISTRY

    assert tuple(item.id for item in definitions) == (
        "person",
        "organization",
        "concept",
        "library",
        "decision",
    )
    assert len({item.id for item in definitions}) == len(definitions)
    assert len({item.folder for item in definitions}) == len(definitions)
    assert len({item.label for item in definitions}) == len(definitions)
    assert len(
        {alias for item in definitions for alias in item.aliases}
    ) == sum(len(item.aliases) for item in definitions)
    assert entity_types.ENTITY_TYPES_BY_ID["organization"].folder == "Organizations"

    with pytest.raises(TypeError):
        entity_types.ENTITY_TYPES_BY_ID["vendor"] = definitions[0]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        definitions[0].folder = "Humans"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("person", "person"),
        ("people", "person"),
        ("Organization", "organization"),
        ("organisations", "organization"),
        ("software-library", "library"),
    ],
)
def test_entity_type_aliases_resolve_to_stable_ids(value: str, expected: str) -> None:
    assert entity_types.resolve_entity_type(value).id == expected


def test_unknown_entity_type_does_not_resolve() -> None:
    assert entity_types.resolve_entity_type("vendor") is None


def test_public_entity_writer_guidance_covers_every_registered_kind() -> None:
    guidance = commands.op_link.__doc__ or ""

    assert "stable entity registry returned" in guidance
    assert "bootstrap.entity_registry" in guidance
    assert "One of person" not in guidance

    for command_name in ("link", "connect_memory"):
        registry = commands.COMMANDS if command_name == "link" else commands.PRODUCT_COMMANDS
        command = next(
            command for command in registry if command.name == command_name
        )
        entity_param = next(param for param in command.params if param.name == "entity_type")
        assert entity_param.choices == entity_types.ENTITY_TYPE_IDS


def _entity_page(
    root, relative: str, *, title: str, aliases: list[str] | None = None, status: str = "active"
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    alias_line = f"aliases: {aliases!r}\n" if aliases else ""
    path.write_text(
        "---\n"
        "type: entity\n"
        "entity_type: person\n"
        f"title: {title}\n"
        f"status: {status}\n"
        f"{alias_line}"
        "---\n\n"
        f"# {title}\n",
        encoding="utf-8",
    )


def test_entity_candidate_resolution_is_alias_aware_active_and_bounded(tmp_path) -> None:
    _entity_page(
        tmp_path,
        "Knowledge Base/Entities/People/Olivia Khwaja.md",
        title="Olivia Khwaja",
        aliases=["Olivia K"],
    )
    _entity_page(
        tmp_path,
        "Knowledge Base/Entities/People/Archived Olivia.md",
        title="Archived Olivia",
        aliases=["Olivia K"],
        status="archived",
    )

    result = entity_candidates.resolve_entity_candidate(tmp_path, name="olivia k")

    assert result["status"] == "match"
    assert result["candidates"][0]["path"].endswith("Olivia Khwaja.md")
    assert result["candidates"][0]["matched_by"] == "alias"
    routed = commands.op_connect_memory(
        tmp_path,
        operation="resolve-entity",
        name="Olivia K",
        entity_type="person",
    )
    assert routed == result


def test_entity_candidate_resolution_returns_ambiguity_without_mutation(tmp_path) -> None:
    for filename, title in (("One.md", "Olivia One"), ("Two.md", "Olivia Two")):
        _entity_page(
            tmp_path,
            f"Knowledge Base/Entities/People/{filename}",
            title=title,
            aliases=["Olivia"],
        )

    result = entity_candidates.resolve_entity_candidate(
        tmp_path, name="Olivia", limit=1
    )

    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 1
    assert result["omitted_candidate_count"] == 1
    assert sorted(path.name for path in tmp_path.rglob("*.md")) == ["One.md", "Two.md"]


def test_public_entity_type_schema_and_cli_choices_come_from_registry() -> None:
    assert get_args(entity_types.EntityTypeId) == entity_types.ENTITY_TYPE_IDS

    for command_name, registry in (
        ("link", commands.COMMANDS),
        ("connect_memory", commands.PRODUCT_COMMANDS),
    ):
        command = next(command for command in registry if command.name == command_name)
        parameter = next(param for param in command.params if param.name == "entity_type")
        assert parameter.choices == entity_types.ENTITY_TYPE_IDS
