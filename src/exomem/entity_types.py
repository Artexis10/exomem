"""Immutable registry for Exomem's stable entity kinds.

Entity IDs are the durable storage/API tokens.  Labels, folders, aliases, and
agent capture guidance live beside them so dependent surfaces do not grow their
own validity lists.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class EntityTypeDefinition:
    """One supported entity kind and its durable routing metadata."""

    id: str
    folder: str
    label: str
    aliases: tuple[str, ...]
    capture_guidance: str
    optional_frontmatter: tuple[str, ...] = ()


ENTITY_TYPE_REGISTRY: tuple[EntityTypeDefinition, ...] = (
    EntityTypeDefinition(
        id="person",
        folder="People",
        label="Person",
        aliases=("people", "individual", "individuals", "human", "humans"),
        capture_guidance="A stable person identity with reusable facts, history, or relations.",
        optional_frontmatter=("affiliation", "relationship"),
    ),
    EntityTypeDefinition(
        id="organization",
        folder="Organizations",
        label="Organization",
        aliases=(
            "organizations",
            "organisation",
            "organisations",
            "company",
            "companies",
            "institution",
            "institutions",
        ),
        capture_guidance=(
            "A stable organization identity with reusable facts, history, or relations."
        ),
    ),
    EntityTypeDefinition(
        id="concept",
        folder="Concepts",
        label="Concept",
        aliases=("concepts", "idea", "ideas"),
        capture_guidance="A reusable concept that anchors conclusions across sources.",
        optional_frontmatter=("domain",),
    ),
    EntityTypeDefinition(
        id="library",
        folder="Libraries",
        label="Library",
        aliases=(
            "libraries",
            "software-library",
            "software-libraries",
            "package",
            "packages",
        ),
        capture_guidance="A reusable software library or package with durable project context.",
        optional_frontmatter=("language", "repo", "license", "used_in"),
    ),
    EntityTypeDefinition(
        id="decision",
        folder="Decisions",
        label="Decision",
        aliases=("decisions", "adr", "adrs"),
        capture_guidance="A durable decision whose identity is useful as a graph node.",
        optional_frontmatter=("decided", "project", "decision_status"),
    ),
)


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _build_indexes() -> tuple[
    Mapping[str, EntityTypeDefinition],
    Mapping[str, EntityTypeDefinition],
    Mapping[str, EntityTypeDefinition],
]:
    by_id: dict[str, EntityTypeDefinition] = {}
    by_folder: dict[str, EntityTypeDefinition] = {}
    by_alias: dict[str, EntityTypeDefinition] = {}
    for definition in ENTITY_TYPE_REGISTRY:
        if definition.id != _normalized(definition.id):
            raise ValueError(f"entity type id must be normalized: {definition.id!r}")
        if definition.id in by_id:
            raise ValueError(f"duplicate entity type id: {definition.id!r}")
        folder_key = definition.folder.casefold()
        if folder_key in by_folder:
            raise ValueError(f"duplicate entity folder: {definition.folder!r}")
        by_id[definition.id] = definition
        by_folder[folder_key] = definition
        for raw_alias in definition.aliases:
            alias = _normalized(raw_alias)
            if not alias or alias in by_id or alias in by_alias:
                raise ValueError(f"duplicate or invalid entity alias: {raw_alias!r}")
            by_alias[alias] = definition
    return (
        MappingProxyType(by_id),
        MappingProxyType(by_folder),
        MappingProxyType(by_alias),
    )


ENTITY_TYPES_BY_ID, ENTITY_TYPES_BY_FOLDER, ENTITY_TYPES_BY_ALIAS = _build_indexes()
ENTITY_TYPE_IDS: tuple[str, ...] = tuple(ENTITY_TYPES_BY_ID)
ENTITY_TYPE_TO_FOLDER: Mapping[str, str] = MappingProxyType(
    {key: definition.folder for key, definition in ENTITY_TYPES_BY_ID.items()}
)


def resolve_entity_type(value: str) -> EntityTypeDefinition | None:
    """Resolve a stable ID, display label, folder, or declared alias."""
    normalized = _normalized(value)
    direct = ENTITY_TYPES_BY_ID.get(normalized) or ENTITY_TYPES_BY_ALIAS.get(normalized)
    if direct is not None:
        return direct
    for definition in ENTITY_TYPE_REGISTRY:
        if normalized in {_normalized(definition.label), _normalized(definition.folder)}:
            return definition
    return None
