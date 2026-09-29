"""The entity contract has one immutable, alias-aware registry."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from exomem import commands, entity_candidates, entity_types, link


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
    assert "active entity registry" in guidance
    assert "_Schema/entity-types.yaml" in guidance
    assert "ENTITY_TYPE_UNKNOWN" in guidance
    assert "INVALID_LINK (bad entity_type" not in guidance
    assert "One of person" not in guidance

    for command_name in ("link", "connect_memory"):
        registry = commands.COMMANDS if command_name == "link" else commands.PRODUCT_COMMANDS
        command = next(
            command for command in registry if command.name == command_name
        )
        entity_param = next(param for param in command.params if param.name == "entity_type")
        assert entity_param.choices == ()


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
    assert entity_types.EntityTypeId is str

    for command_name, registry in (
        ("link", commands.COMMANDS),
        ("connect_memory", commands.PRODUCT_COMMANDS),
    ):
        command = next(command for command in registry if command.name == command_name)
        parameter = next(param for param in command.params if param.name == "entity_type")
        assert parameter.choices == ()


def test_vault_type_declares_bounded_facets(tmp_path: Path) -> None:
    """A vault type is no longer limited to the ten core writer fields: the
    registry's `facets` section declares bounded facets (name, single or multi,
    text/wikilink/date, at most 16 per type) for a core or vault-defined type.
    The core writer fields keep their exact mapping and cannot be redeclared."""
    writer_fields = tuple(
        inspect.signature(link._entity_writer_optional_values).parameters
    )
    assert entity_types.ENTITY_WRITER_OPTIONAL_FRONTMATTER == writer_fields
    assert entity_types.SUPPORTED_OPTIONAL_FRONTMATTER == frozenset(writer_fields)

    proposal = _proposal({"site": _extension(folder="Sites", label="Site", aliases=[], parent=None)})
    proposal["facets"] = {
        "site": {
            "operator": {"cardinality": "single", "value": "wikilink"},
            "established": {"cardinality": "single", "value": "date"},
            "soils": {"cardinality": "multi", "value": "text"},
        },
        "organization": {"roles": {"cardinality": "multi", "value": "text"}},
    }
    _write_registry(tmp_path, proposal)

    registry = entity_types.load_entity_types(tmp_path)

    assert registry.findings == ()
    assert [
        (facet.name, facet.cardinality, facet.value) for facet in registry.facets_for("site")
    ] == [
        ("operator", "single", "wikilink"),
        ("established", "single", "date"),
        ("soils", "multi", "text"),
    ]
    assert [facet.name for facet in registry.facets_for("organization")] == ["roles"]
    assert registry.facets_for("person") == ()

    too_many = {f"facet_{index}": {"value": "text"} for index in range(17)}
    for declaration, code in (
        ({"site": too_many}, "too_many_facets"),
        ({"site": {"affiliation": {"value": "text"}}}, "reserved_facet_name"),
        ({"site": {"entity_type": {"value": "text"}}}, "reserved_facet_name"),
        ({"site": {"Operator": {"value": "text"}}}, "invalid_facet_name"),
        ({"site": {"operator": {"value": "number"}}}, "invalid_facet_value"),
        ({"site": {"operator": {"cardinality": "many"}}}, "invalid_facet_cardinality"),
        ({"site": {"operator": {"value": "text", "unit": "kg"}}}, "unknown_field"),
        ({"venue": {"operator": {"value": "text"}}}, "unknown_facet_type"),
    ):
        invalid = entity_types.load_entity_types(proposal={**proposal, "facets": declaration})
        assert code in {item["code"] for item in invalid.findings}, declaration
        # A declaration with any finding contributes no facets at all.
        assert invalid.facets_for("site") == ()
        assert "site" in invalid.extensions

    distinct_values = {
        field: [f"{field}-value"] if field == "used_in" else f"{field}-value"
        for field in writer_fields
    }
    rendered = link._render_entity(
        entity_type="site",
        name="Aster Hall",
        summary="A synthetic site used to pin core and declared entity frontmatter.",
        why_in_kb=None,
        date_iso="2026-08-22",
        tags=[],
        connections=[],
        exomem_id="00000000-0000-4000-8000-000000000001",
        definition=entity_types.EntityTypeDefinition(
            id="site",
            folder="Sites",
            label="Site",
            aliases=(),
            capture_guidance="A stable synthetic site identity.",
            optional_frontmatter=writer_fields,
            core=False,
        ),
        facets=[
            ("operator", ["[[Knowledge Base/Entities/Organizations/Aster Co]]"], False),
            ("established", ["2019-04-01"], False),
            ("soils", ["loam", "clay: heavy"], True),
        ],
        **distinct_values,
    )
    frontmatter = yaml.safe_load(rendered.split("---", 2)[1])

    for field, value in distinct_values.items():
        assert frontmatter[field] == value
    assert frontmatter["operator"] == "[[Knowledge Base/Entities/Organizations/Aster Co]]"
    assert frontmatter["established"] == "2019-04-01"
    assert frontmatter["soils"] == ["loam", "clay: heavy"]


def test_parent_may_name_a_parentless_vault_type(tmp_path: Path) -> None:
    """A vault-rooted family: `milking-machine` under a vault `equipment` root.
    Families stay one level deep, so a child's parent must itself be a root."""
    _write_registry(
        tmp_path,
        _proposal(
            {
                "equipment": _extension(
                    folder="Equipment", label="Equipment", aliases=[], parent=None
                ),
                "milking-machine": _extension(
                    folder="Milking Machines",
                    label="Milking Machine",
                    aliases=[],
                    parent="equipment",
                ),
            }
        ),
    )

    registry = entity_types.load_entity_types(tmp_path)

    assert registry.findings == ()
    assert registry.family_of("milking-machine") == "equipment"
    assert registry.family_of("equipment") == "equipment"
    assert registry.families["equipment"] == ("equipment", "milking-machine")
    assert registry.matches_family("milking-machine", "equipment")


def _extension(
    *,
    folder: str = "Places",
    label: str = "Place",
    aliases: list[str] | None = None,
    cue_nouns: list[str] | None = None,
    parent: str | None = "concept",
    status: str = "active",
) -> dict:
    value: dict = {
        "folder": folder,
        "label": label,
        "aliases": aliases if aliases is not None else ["location"],
        "capture_guidance": "A stable place identity used across notes.",
        "status": status,
    }
    if cue_nouns is not None:
        value["cue_nouns"] = cue_nouns
    if parent is not None:
        value["parent"] = parent
    return value


def _proposal(entity_types_map: dict[str, dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "entity_types": entity_types_map if entity_types_map is not None else {"place": _extension()},
    }


def _write_registry(vault: Path, proposal: dict) -> Path:
    path = vault / "Knowledge Base" / "_Schema" / "entity-types.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(proposal, sort_keys=False), encoding="utf-8")
    return path


def test_extension_type_loads_beside_core(tmp_path: Path) -> None:
    _write_registry(tmp_path, _proposal())

    registry = entity_types.load_entity_types(tmp_path)

    assert registry.active_ids == (*entity_types.ENTITY_TYPE_IDS, "place")
    assert registry.extensions["place"].folder == "Places"
    for value in ("place", "Place", "Places", "location"):
        assert registry.resolve(value).id == "place"


def test_extension_id_folder_alias_collisions_with_core_are_findings_not_exceptions(
    tmp_path: Path,
) -> None:
    proposal = _proposal(
        {
            "person": _extension(folder="Guests", label="Guest", aliases=[]),
            "crew": _extension(folder="People", label="Crew", aliases=[]),
            "human": _extension(folder="Humans", label="Human Kind", aliases=[]),
            "collective": _extension(
                folder="Collectives", label="Collective", aliases=["company"]
            ),
            "place": _extension(),
        }
    )
    _write_registry(tmp_path, proposal)

    registry = entity_types.load_entity_types(tmp_path)

    assert set(registry.extensions) == {"place"}
    assert len(registry.findings) == 5
    assert {item["code"] for item in registry.findings} == {"collision"}
    assert registry.resolve("location").id == "place"


def test_invalid_folder_segment_is_a_finding(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        _proposal({"place": _extension(folder="../Places")}),
    )

    registry = entity_types.load_entity_types(tmp_path)

    assert registry.extensions == {}
    assert any(
        item["code"] == "invalid_folder" and item["path"] == "entity_types.place.folder"
        for item in registry.findings
    )


def test_deprecated_extension_is_excluded_from_active_ids(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        _proposal({"place": _extension(status="deprecated")}),
    )

    registry = entity_types.load_entity_types(tmp_path)

    assert "place" in registry.extensions
    assert "place" not in registry.active_ids
    assert registry.resolve("place") is None


def test_parent_must_name_a_core_type(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        _proposal(
            {
                "place": _extension(),
                "venue": _extension(
                    folder="Venues", label="Venue", aliases=[], parent="place"
                ),
            }
        ),
    )

    registry = entity_types.load_entity_types(tmp_path)

    assert set(registry.extensions) == {"place"}
    assert any(
        item["code"] == "invalid_parent" and item["entity_type"] == "venue"
        for item in registry.findings
    )


def test_loader_is_cached_by_extension_hash(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, _proposal())

    first = entity_types.load_entity_types(tmp_path)
    second = entity_types.load_entity_types(tmp_path)
    assert first is second

    changed = _proposal()
    changed["entity_types"]["place"]["aliases"] = ["location", "site"]
    path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
    third = entity_types.load_entity_types(tmp_path)
    assert third is not second
    assert third.extension_hash != second.extension_hash


def test_save_registry_refuses_observed_deletion_and_stale_hash(tmp_path: Path) -> None:
    created = entity_types.save_registry(tmp_path, _proposal(), expected_hash=None, observed_ids=())

    with pytest.raises(ValueError, match="OBSERVED_ENTITY_TYPE_DELETION"):
        entity_types.save_registry(
            tmp_path,
            _proposal({}),
            expected_hash=created["content_hash"],
            observed_ids=("place",),
        )
    with pytest.raises(ValueError, match="STALE_ENTITY_TYPE_REGISTRY"):
        entity_types.save_registry(
            tmp_path,
            _proposal(),
            expected_hash="stale",
            observed_ids=("place",),
        )


def test_save_registry_ignores_unregistered_authored_types(tmp_path: Path) -> None:
    for name in ("Aster Hall", "Beryl Room"):
        _entity_page(
            tmp_path,
            f"Knowledge Base/Entities/Places/{name}.md",
            title=name,
        )
        page = tmp_path / "Knowledge Base" / "Entities" / "Places" / f"{name}.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "entity_type: person", "entity_type: place"
            ),
            encoding="utf-8",
        )

    venue = _proposal(
        {
            "venue": _extension(
                folder="Venues", label="Venue", aliases=[], parent="concept"
            )
        }
    )
    created = commands.op_schema_memory(
        tmp_path,
        operation="save-entity-types",
        proposal=venue,
        why="Register the synthetic venue type.",
    )

    _entity_page(
        tmp_path,
        "Knowledge Base/Entities/Venues/Cedar Room.md",
        title="Cedar Room",
    )
    venue_page = (
        tmp_path / "Knowledge Base" / "Entities" / "Venues" / "Cedar Room.md"
    )
    venue_page.write_text(
        venue_page.read_text(encoding="utf-8").replace(
            "entity_type: person", "entity_type: venue"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="OBSERVED_ENTITY_TYPE_DELETION"):
        commands.op_schema_memory(
            tmp_path,
            operation="save-entity-types",
            proposal=_proposal({}),
            why="Exercise observed extension deletion protection.",
            expected_hash=created["saved"]["content_hash"],
        )


def test_resolve_accepts_normalized_extension_folder(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        _proposal(
            {
                "place": _extension(
                    folder="My Places", label="Place", aliases=[]
                )
            }
        ),
    )

    registry = entity_types.load_entity_types(tmp_path)

    assert registry.resolve("my-places").id == "place"


def test_unsupported_optional_frontmatter_is_a_finding(tmp_path: Path) -> None:
    extension = _extension()
    extension["optional_frontmatter"] = ["domain", "opening_hours"]
    _write_registry(tmp_path, _proposal({"place": extension}))

    registry = entity_types.load_entity_types(tmp_path)

    assert registry.extensions == {}
    assert any(
        finding["code"] == "unsupported_optional_frontmatter"
        and finding["path"] == "entity_types.place.optional_frontmatter"
        and "opening_hours" in finding["detail"]
        for finding in registry.findings
    )


def test_schema_memory_saves_entity_types_only_with_why(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="WHY_REQUIRED"):
        commands.op_schema_memory(
            tmp_path,
            operation="save-entity-types",
            proposal=_proposal(),
        )

    result = commands.op_schema_memory(
        tmp_path,
        operation="save-entity-types",
        proposal=_proposal(),
        why="Add a stable synthetic place identity.",
    )

    assert result["valid"] is True
    assert result["why"] == "Add a stable synthetic place identity."
    assert entity_types.load_entity_types(tmp_path).active_ids[-1] == "place"


def test_cue_nouns_default_to_aliases(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        _proposal({"place": _extension(aliases=["location", "site"], cue_nouns=None)}),
    )

    definition = entity_types.load_entity_types(tmp_path).extensions["place"]

    assert definition.cue_nouns == ("location", "site")
