"""The entity contract has one immutable, alias-aware registry."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from exomem import commands, entity_types


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
