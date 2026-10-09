"""Built-in collection types as declarations (move-structured-collections-to-sqlite P1a.4).

Records and Planning ship as package data, resolve through one registry, and
carry the only wire maps (legacy property, receipt and error names). Their
wire maps are pinned against the code that emits those names today, so the
declarations cannot drift from the file-mode wire. Planning's hierarchy is the
named validator ``planning.hierarchy.v1``. Declared-type authoring is P4.
All data is invented.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml
from record_fixtures import LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT

from exomem import collection_profiles, mutation_terminal, planning, plan_memory
from exomem import structured_collections as collections
from exomem.collection_store import types

PLANNING_COLLECTION_PATH = "Knowledge Base/Planning/Work/_collection.md"
PLANNING_COLLECTION_ID = "2db90f18-70df-4e41-986e-2d7d7db1caca"


def _planning_manifest_text(fields: dict[str, dict[str, object]]) -> str:
    frontmatter = {
        "type": "collection",
        "exomem_id": PLANNING_COLLECTION_ID,
        "title": "Planning work",
        "semantic_profile": "planning",
        "collection_version": 1,
        "schema_version": 1,
        "lifecycle": "active",
        "storage": {"strategy": "markdown-items", "source": "Items", "format_version": 1},
        "item_schema": {"natural_key": ["title"], "fields": fields},
    }
    return "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n"


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _planning_manifest(tmp_path: Path, text: str) -> collections.CollectionManifest:
    return collections.parse_manifest_bytes(tmp_path, PLANNING_COLLECTION_PATH, text.encode())


def test_the_registry_holds_exactly_the_two_built_ins() -> None:
    registry = types.builtin_types()
    assert set(registry) == {"records", "planning"}
    assert all(declared.builtin for declared in registry.values())


def test_records_is_an_extensible_observed_ledger() -> None:
    records = types.builtin_types()["records"]
    assert (records.kind, records.placement, records.item_type) == ("observed", "Records", "record")
    assert records.extensible is True
    assert records.fields == {}
    assert records.default_audience == "policy"
    assert records.validators == ()


def test_planning_is_an_intended_type_with_its_hierarchy_validator() -> None:
    planning_type = types.builtin_types()["planning"]
    assert (planning_type.kind, planning_type.placement, planning_type.item_type) == (
        "intended",
        "Planning",
        "plan",
    )
    assert planning_type.default_audience == "policy"
    assert planning_type.validators == ("planning.hierarchy.v1",)


@pytest.mark.parametrize("name", ["records", "planning"])
def test_wire_maps_match_the_names_emitted_today(name: str) -> None:
    wire = types.builtin_types()[name].wire
    profile = collection_profiles.PROFILES[name]
    assert wire["item_id_property"] == profile.item_id_property
    assert wire["reference_namespace"] == profile.reference_namespace
    assert wire["manifest_audit_property"] == profile.manifest_audit_property
    assert wire["item_audit_marker"] == profile.item_audit_marker
    assert wire["activity_prefix"] == profile.activity_prefix
    assert types.builtin_types()[name].item_type == profile.item_type
    assert types.builtin_types()[name].placement == profile.placement_layer
    marker = {
        "records": mutation_terminal._RECORD_RECEIPT_MARKER,  # noqa: SLF001
        "planning": mutation_terminal._PLAN_RECEIPT_MARKER,  # noqa: SLF001
    }[name]
    assert wire["receipt_marker"] == marker
    assert wire["receipt_property"] == {"records": "_record_receipt", "planning": "_plan_receipt"}[
        name
    ]


def test_planning_error_code_map_reproduces_the_facade() -> None:
    codes = types.builtin_types()["planning"].wire["error_codes"]
    assert codes, "Planning renames engine codes on its wire"
    for engine_code, public in codes.items():
        code, _, guard = engine_code.partition(".")
        reason = f"{guard} guard is stale" if guard else "invented reason"
        error = collections.CollectionError(code, reason)
        assert plan_memory._public_error_code(error) == public, engine_code  # noqa: SLF001
    # Nothing the facade renames today is missing from the map.
    for code in (
        "RECORD_NOT_FOUND",
        "AMBIGUOUS_RECORD",
        "RECORD_ID_CONFLICT",
        "INVALID_RECORD_CONTINUATION",
        "STALE_RECORD_SNAPSHOT",
        "RECORD_RESPONSE_TOO_LARGE",
    ):
        assert code in codes
    assert {"STALE_RECORD.item", "STALE_RECORD.container"} <= set(codes)
    assert types.builtin_types()["records"].wire["error_codes"] == {}


def test_planning_core_fields_are_what_the_planning_profile_requires(tmp_path: Path) -> None:
    fields = {
        name: {"type": spec.type, **({"required": True} if spec.required else {})}
        for name, spec in types.builtin_types()["planning"].fields.items()
    }
    manifest = _planning_manifest(tmp_path, _planning_manifest_text(fields))
    assert planning.require_planning_profile(manifest) is manifest
    # Dropping any declared core field breaks the profile, so none is decorative.
    for name in fields:
        reduced = {key: value for key, value in fields.items() if key != name}
        with pytest.raises(collections.CollectionError):
            planning.require_planning_profile(
                _planning_manifest(tmp_path, _planning_manifest_text(reduced))
            )


def test_planning_views_are_the_six_scaffolded_horizons(tmp_path: Path) -> None:
    fields = {
        name: {"type": spec.type, **({"required": True} if spec.required else {})}
        for name, spec in types.builtin_types()["planning"].fields.items()
    }
    text = _planning_manifest_text(fields)
    scaffolded = planning._with_default_scaffold(  # noqa: SLF001
        text, _planning_manifest(tmp_path, text), vault_root=None
    )
    expected = [
        {"name": name, "filters": _plain(view["query"]["filters"])}
        for name, view in _planning_manifest(tmp_path, scaffolded).views.items()
    ]
    declared = _plain(types.builtin_types()["planning"].views)
    assert declared == expected
    assert [view["name"] for view in declared] == [
        "inbox",
        "week",
        "month",
        "quarter",
        "year",
        "multi-year",
    ]


def test_semantic_profile_manifests_resolve_to_the_built_in_types(tmp_path: Path) -> None:
    ledger = collections.parse_manifest_bytes(
        tmp_path, LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT.encode()
    )
    assert types.type_for_manifest(ledger).name == "records"
    fields = {"title": {"type": "string", "required": True}}
    plan = _planning_manifest(tmp_path, _planning_manifest_text(fields))
    assert types.type_for_manifest(plan).name == "planning"


def test_an_unknown_profile_never_becomes_records() -> None:
    with pytest.raises(types.CollectionTypeError) as refused:
        types.type_for_profile("dataset")
    assert refused.value.code == "UNKNOWN_COLLECTION_TYPE"


def test_named_validators_are_a_closed_registry() -> None:
    assert types.named_validator("planning.hierarchy.v1").name == "planning.hierarchy.v1"
    with pytest.raises(types.CollectionTypeError) as refused:
        types.named_validator("references.acyclic.v9")
    assert refused.value.code == "UNKNOWN_NAMED_VALIDATOR"


def _plan_ref(key: str) -> str:
    return f"exomem://plan/{PLANNING_COLLECTION_ID}/{key}"


def test_the_hierarchy_validator_accepts_a_typed_tree_and_refuses_a_cycle(
    tmp_path: Path,
) -> None:
    fields = {
        "title": {"type": "string", "required": True},
        "kind": {"type": "string"},
        "lifecycle": {"type": "string"},
        "commitment": {"type": "string"},
        "parent": {"type": "link"},
    }
    manifest = _planning_manifest(tmp_path, _planning_manifest_text(fields))
    validator = types.named_validator("planning.hierarchy.v1")
    base = {"lifecycle": "active", "commitment": "uncommitted"}
    outcome = "11111111-1111-4111-8111-111111111111"
    initiative = "22222222-2222-4222-8222-222222222222"
    work = "33333333-3333-4333-8333-333333333333"
    tree = {
        outcome: {**base, "kind": "outcome"},
        initiative: {**base, "kind": "initiative", "parent": _plan_ref(outcome)},
        work: {**base, "kind": "work-item", "parent": _plan_ref(initiative)},
    }
    validator.validate(manifest, tree)

    cyclic = {
        initiative: {**base, "kind": "initiative", "parent": _plan_ref(work)},
        work: {**base, "kind": "work-item", "parent": _plan_ref(initiative)},
    }
    with pytest.raises(collections.CollectionError, match="INVALID_PLAN_RELATION"):
        validator.validate(manifest, cyclic)
    wrong_parent = {
        outcome: {**base, "kind": "outcome"},
        work: {**base, "kind": "work-item", "parent": _plan_ref(outcome)},
    }
    with pytest.raises(collections.CollectionError, match="INVALID_PLAN_RELATION"):
        validator.validate(manifest, wrong_parent)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"colour": "blue"}, "UNKNOWN_DECLARATION_KEY"),
        ({"kind": "aspirational"}, "UNKNOWN_COLLECTION_KIND"),
        ({"validators": ["invented.validator.v1"]}, "UNKNOWN_NAMED_VALIDATOR"),
        ({"wire": {"item_id_property": "recipe_id"}}, "BUILTIN_ONLY_WIRE"),
    ],
)
def test_declarations_are_closed(change: dict[str, object], code: str) -> None:
    declaration = {
        "name": "recipes",
        "version": 1,
        "title": "Recipes",
        "item_type": "recipe",
        "kind": "procedural",
        "placement": "Recipes",
        "fields": {"title": {"type": "string", "required": True}},
        "default_audience": "owner",
        **change,
    }
    with pytest.raises(types.CollectionTypeError) as refused:
        types.parse_declaration(declaration, builtin=False)
    assert refused.value.code == code


def test_package_declarations_are_generic_package_data() -> None:
    for name in ("records", "planning"):
        text = types.declaration_text(name)
        assert re.search(r"(?m)^name: " + name + "$", text)
