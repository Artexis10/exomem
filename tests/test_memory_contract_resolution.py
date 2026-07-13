from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import memory_schema, semantic_language_registry, vault


def _contract(
    name: str,
    *,
    scope: dict | None = None,
    validation: str = "warn",
    fields: dict | None = None,
    blocks: dict | None = None,
    kinds: dict | None = None,
    categories: dict | None = None,
    relations: dict | None = None,
    unknown_fields: str = "allow",
    unknown_blocks: str = "allow",
    unknown_kinds: str = "allow",
    unknown_categories: str = "allow",
    unknown_relations: str = "allow",
) -> memory_schema.LoadedMemoryContract:
    parsed = memory_schema.contract_from_dict(
        {
            "schema_version": 1,
            "name": name,
            "scope": scope or {},
            "validation": validation,
            "sample_size": 0,
            "fields": fields or {},
            "blocks": blocks or {},
            "kinds": kinds or {},
            "categories": categories or {},
            "relations": relations or {},
            "unknown_fields": unknown_fields,
            "unknown_blocks": unknown_blocks,
            "unknown_kinds": unknown_kinds,
            "unknown_categories": unknown_categories,
            "unknown_relations": unknown_relations,
        }
    )
    return memory_schema.LoadedMemoryContract(
        contract=parsed,
        path=f"Knowledge Base/_Schema/contracts/{name}.yaml",
        content_hash=(name.encode("utf-8").hex() + "0" * 64)[:64],
    )


def _constraint(
    resolved: memory_schema.ResolvedMemoryContracts,
    identity: tuple[str, str, str],
) -> memory_schema.ResolvedContractConstraint:
    return next(item for item in resolved.constraints if item.identity == identity)


def test_resolution_is_per_rule_and_preserves_unrelated_lower_specificity() -> None:
    contracts = (
        _contract(
            "global",
            fields={
                "owner": {"types": ["string"]},
                "status": {"required": True, "types": ["string"]},
            },
            categories={"config": {"required": True}},
        ),
        _contract(
            "insight",
            scope={"page_type": "insight"},
            fields={"status": {"required": False}},
        ),
        _contract(
            "atlas",
            scope={"project": "atlas"},
            validation="strict",
            fields={"status": {"enum": ["active", "draft"]}},
        ),
        _contract(
            "atlas-insight",
            scope={"project": "atlas", "page_type": "insight"},
            validation="strict",
            fields={"status": {"enum": ["active"]}},
        ),
    )

    resolved = memory_schema.resolve_contracts(
        contracts,
        projects=("companion", "atlas"),
        page_type="insight",
    )

    assert resolved.validation == "strict"
    assert _constraint(resolved, ("fields", "status", "required")).value is False
    assert _constraint(resolved, ("fields", "status", "required")).specificity == (
        "page_type"
    )
    assert _constraint(resolved, ("fields", "status", "types")).value == (
        "string",
    )
    assert _constraint(resolved, ("fields", "status", "enum")).value == (
        "active",
    )
    assert _constraint(resolved, ("fields", "status", "enum")).specificity == (
        "project+page_type"
    )
    assert _constraint(resolved, ("fields", "owner", "types")).specificity == (
        "global"
    )
    assert _constraint(resolved, ("categories", "config", "required")).value is True
    assert resolved.conflicts == ()


def test_equal_specificity_collapses_strengthens_and_intersects_typed_sets() -> None:
    contracts = (
        _contract(
            "one",
            scope={"project": "atlas"},
            fields={
                "value": {
                    "required": False,
                    "types": ["string", "integer"],
                    "enum": ["same", 1],
                }
            },
        ),
        _contract(
            "two",
            scope={"project": "atlas"},
            fields={
                "value": {
                    "required": True,
                    "types": ["string", "number"],
                    "enum": ["same", 1.0],
                }
            },
        ),
    )

    resolved = memory_schema.resolve_contracts(
        tuple(reversed(contracts)), projects=("atlas",), page_type="insight"
    )

    assert _constraint(resolved, ("fields", "value", "required")).value is True
    assert _constraint(resolved, ("fields", "value", "types")).value == (
        "string",
    )
    assert _constraint(resolved, ("fields", "value", "enum")).value == ("same",)
    assert _constraint(resolved, ("fields", "value", "enum")).contracts == (
        "one",
        "two",
    )
    assert resolved.conflicts == ()


def test_finite_allowed_sets_intersect_and_required_elements_union() -> None:
    contracts = (
        _contract(
            "one",
            scope={"project": "atlas"},
            categories={
                "config": {"required": True},
                "rule": {"required": False},
            },
            unknown_categories="forbid",
        ),
        _contract(
            "two",
            scope={"project": "atlas"},
            categories={
                "config": {"required": False},
                "term": {"required": True},
            },
            unknown_categories="forbid",
        ),
    )

    resolved = memory_schema.resolve_contracts(
        contracts, projects=("atlas",), page_type="insight"
    )

    assert _constraint(resolved, ("categories", "*", "allowed")).value == (
        "config",
    )
    assert _constraint(resolved, ("categories", "config", "required")).value is True
    assert _constraint(resolved, ("categories", "term", "required")).value is True
    conflicts = [item.as_dict() for item in resolved.conflicts]
    assert any(
        item["code"] == "CONTRACT_RULE_CONFLICT"
        and item["resolved_rule"] == ["categories", "term", "required"]
        for item in conflicts
    )


def test_empty_intersections_scalar_modes_and_required_exclusion_conflict() -> None:
    contracts = (
        _contract(
            "global-required",
            categories={"rule": {"required": True}},
        ),
        _contract(
            "one",
            scope={"project": "atlas"},
            validation="strict",
            fields={"status": {"types": ["string"]}},
            categories={"config": {"required": False}},
            unknown_categories="forbid",
        ),
        _contract(
            "two",
            scope={"project": "atlas"},
            validation="warn",
            fields={"status": {"types": ["integer"]}},
            categories={"term": {"required": False}},
            unknown_categories="forbid",
        ),
        _contract(
            "three",
            scope={"project": "atlas"},
            categories={"config": {"required": False}},
            unknown_categories="allow",
        ),
    )

    resolved = memory_schema.resolve_contracts(
        contracts, projects=("atlas",), page_type="insight"
    )
    by_code = {item.code for item in resolved.conflicts}

    assert resolved.validation is None
    assert "CONTRACT_VALIDATION_CONFLICT" in by_code
    assert "CONTRACT_RULE_CONFLICT" in by_code
    assert any(
        item.resolved_rule == ("fields", "status", "types")
        for item in resolved.conflicts
    )
    assert any(
        item.resolved_rule == ("categories", "*", "allowed")
        for item in resolved.conflicts
    )
    excluded = memory_schema.resolve_contracts(
        (
            _contract(
                "required-rule",
                categories={"rule": {"required": True}},
            ),
            _contract(
                "config-only",
                scope={"project": "atlas"},
                categories={"config": {"required": False}},
                unknown_categories="forbid",
            ),
        ),
        projects=("atlas",),
        page_type="insight",
    )
    assert any(
        item.resolved_rule == ("categories", "rule", "required")
        for item in excluded.conflicts
    )


def test_resolution_order_is_independent_of_contract_project_and_mapping_order() -> None:
    forward = (
        _contract(
            "alpha",
            scope={"project": "atlas"},
            fields={"z": {"required": True}, "a": {"required": False}},
        ),
        _contract(
            "beta",
            scope={"project": "companion"},
            categories={"rule": {"required": True}},
        ),
    )

    first = memory_schema.resolve_contracts(
        forward, projects=("atlas", "companion"), page_type="insight"
    ).as_dict()
    second = memory_schema.resolve_contracts(
        tuple(reversed(forward)),
        projects=("companion", "atlas", "atlas"),
        page_type="insight",
    ).as_dict()

    assert first == second
    assert first["matched_contracts"] == [
        {
            "name": "alpha",
            "path": "Knowledge Base/_Schema/contracts/alpha.yaml",
        },
        {
            "name": "beta",
            "path": "Knowledge Base/_Schema/contracts/beta.yaml",
        },
    ]


def test_category_and_kind_aliases_collapse_with_all_attached_project_scope() -> None:
    registry = semantic_language_registry.load_registry(
        proposal={
            "schema_version": 1,
            "categories": {
                "config": {
                    "description": "Configuration",
                    "aliases": ["cfg-fact"],
                    "scope": {"projects": ["companion"]},
                }
            },
            "kinds": {
                "protocol": {
                    "description": "Protocol",
                    "aliases": ["custom-procedure"],
                    "scope": {"projects": ["companion"]},
                }
            },
        }
    )
    contract = _contract(
        "aliases",
        categories={
            "cfg-fact": {"required": False},
            "config": {"required": True},
        },
        kinds={
            "custom-procedure": {"required": False},
            "protocol": {"required": True},
        },
    )

    resolved = memory_schema.resolve_contracts(
        (contract,),
        projects=("atlas", "companion"),
        page_type="insight",
        language_registry=registry,
    )

    category = _constraint(resolved, ("categories", "config", "required"))
    kind = _constraint(resolved, ("kinds", "protocol", "required"))
    assert category.value is True
    assert {item["raw_element"] for item in category.as_dict()["provenance"]} == {
        "config",
        "cfg-fact",
    }
    assert kind.value is True
    assert {item["raw_element"] for item in kind.as_dict()["provenance"]} == {
        "custom-procedure",
        "protocol",
    }
    assert resolved.conflicts == ()


def test_registry_scope_failure_is_a_resolution_conflict() -> None:
    registry = semantic_language_registry.load_registry(
        proposal={
            "schema_version": 1,
            "categories": {
                "config": {
                    "description": "Configuration",
                    "scope": {"projects": ["other"]},
                }
            },
            "kinds": {},
        }
    )

    resolved = memory_schema.resolve_contracts(
        (_contract("scoped", categories={"config": {"required": True}}),),
        projects=("atlas", "companion"),
        page_type="insight",
        language_registry=registry,
    )

    assert resolved.conflicts[0].code == "CONTRACT_RULE_CONFLICT"
    assert resolved.conflicts[0].resolved_rule == (
        "categories",
        "config",
        "required",
    )
    assert "scope" in resolved.conflicts[0].detail


def test_registry_scope_failure_is_reported_for_an_empty_element_rule() -> None:
    registry = semantic_language_registry.load_registry(
        proposal={
            "schema_version": 1,
            "categories": {
                "config": {
                    "description": "Configuration",
                    "scope": {"projects": ["other"]},
                }
            },
            "kinds": {},
        }
    )

    resolved = memory_schema.resolve_contracts(
        (_contract("scoped", categories={"config": {}}),),
        projects=("atlas",),
        page_type="insight",
        language_registry=registry,
    )

    assert resolved.conflicts[0].code == "CONTRACT_RULE_CONFLICT"
    assert resolved.conflicts[0].resolved_rule == (
        "categories",
        "config",
        "declaration",
    )
    assert "scope" in resolved.conflicts[0].detail


def test_load_saved_contracts_is_direct_sorted_hashed_and_symlink_free(
    tmp_path: Path,
) -> None:
    contracts_dir = tmp_path / "Knowledge Base/_Schema/contracts"
    contracts_dir.mkdir(parents=True)
    nested = contracts_dir / "nested"
    nested.mkdir()
    template = (
        "schema_version: 1\n"
        "name: {name}\n"
        "scope: {{}}\n"
        "sample_size: 0\n"
        "fields: {{}}\n"
        "blocks: {{}}\n"
        "relations: {{}}\n"
    )
    zulu = contracts_dir / "zulu.yaml"
    alpha = contracts_dir / "alpha.yaml"
    zulu.write_text(template.format(name="zulu"), encoding="utf-8")
    alpha.write_text(template.format(name="alpha"), encoding="utf-8")
    (nested / "ignored.yaml").write_text(
        template.format(name="ignored"), encoding="utf-8"
    )
    (contracts_dir / "ignored.yml").write_text("broken: [", encoding="utf-8")

    loaded = memory_schema.load_saved_contracts(tmp_path)

    assert [item.contract.name for item in loaded] == ["alpha", "zulu"]
    assert [item.path for item in loaded] == [
        "Knowledge Base/_Schema/contracts/alpha.yaml",
        "Knowledge Base/_Schema/contracts/zulu.yaml",
    ]
    assert loaded[0].content_hash == vault.content_hash(
        alpha.read_text(encoding="utf-8")
    )


def test_load_saved_contracts_rejects_a_symlinked_yaml_file(tmp_path: Path) -> None:
    contracts_dir = tmp_path / "Knowledge Base/_Schema/contracts"
    contracts_dir.mkdir(parents=True)
    target = tmp_path / "real.yaml"
    target.write_text("schema_version: 1\nname: linked\n", encoding="utf-8")
    (contracts_dir / "linked.yaml").symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        memory_schema.load_saved_contracts(tmp_path)


def test_load_saved_contracts_rejects_a_symlinked_contracts_directory(
    tmp_path: Path,
) -> None:
    real_directory = tmp_path / "real-contracts"
    real_directory.mkdir()
    schema_directory = tmp_path / "Knowledge Base/_Schema"
    schema_directory.mkdir(parents=True)
    (schema_directory / "contracts").symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        memory_schema.load_saved_contracts(tmp_path)


def test_load_saved_contracts_rejects_a_non_regular_yaml_entry(tmp_path: Path) -> None:
    contracts_dir = tmp_path / "Knowledge Base/_Schema/contracts"
    contracts_dir.mkdir(parents=True)
    (contracts_dir / "bad.yaml").mkdir()

    with pytest.raises(ValueError, match="regular file"):
        memory_schema.load_saved_contracts(tmp_path)


def test_load_saved_contracts_rejects_a_non_directory_contracts_path(
    tmp_path: Path,
) -> None:
    schema_dir = tmp_path / "Knowledge Base/_Schema"
    schema_dir.mkdir(parents=True)
    (schema_dir / "contracts").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="directory"):
        memory_schema.load_saved_contracts(tmp_path)


@pytest.mark.parametrize(
    ("filename", "content", "match"),
    [
        (
            "wrong.yaml",
            "schema_version: 1\nname: actual\nscope: {}\nsample_size: 0\n",
            "filename",
        ),
        (
            "duplicate.yaml",
            "schema_version: 1\nname: duplicate\nname: duplicate\n",
            "duplicate",
        ),
        ("broken.yaml", "schema_version: [\n", "parse"),
    ],
)
def test_load_saved_contracts_rejects_every_bad_direct_yaml(
    tmp_path: Path, filename: str, content: str, match: str
) -> None:
    directory = tmp_path / "Knowledge Base/_Schema/contracts"
    directory.mkdir(parents=True)
    (directory / filename).write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=match):
        memory_schema.load_saved_contracts(tmp_path)


def test_pure_resolver_rejects_duplicate_logical_names_at_different_paths() -> None:
    first = _contract("duplicate")
    second = memory_schema.LoadedMemoryContract(
        contract=first.contract,
        path="Knowledge Base/_Schema/contracts/elsewhere.yaml",
        content_hash="f" * 64,
    )

    with pytest.raises(ValueError, match="duplicate"):
        memory_schema.resolve_contracts(
            (first, second), projects=("atlas",), page_type="insight"
        )


def test_identical_empty_finite_allowed_sets_collapse_without_conflict() -> None:
    resolved = memory_schema.resolve_contracts(
        (
            _contract("one", unknown_categories="forbid"),
            _contract("two", unknown_categories="forbid"),
        ),
        projects=("atlas",),
        page_type="insight",
    )

    assert _constraint(resolved, ("categories", "*", "allowed")).value == ()
    assert resolved.conflicts == ()


def test_resolved_field_types_and_enum_must_have_a_compatible_value() -> None:
    resolved = memory_schema.resolve_contracts(
        (
            _contract("types", fields={"status": {"types": ["string"]}}),
            _contract("enum", fields={"status": {"enum": [1]}}),
        ),
        projects=("atlas",),
        page_type="insight",
    )

    assert any(
        item.resolved_rule == ("fields", "status", "enum")
        and "types" in item.detail
        for item in resolved.conflicts
    )


def test_resolved_enum_as_dict_preserves_date_and_string_type_identity() -> None:
    resolved = memory_schema.resolve_contracts(
        (
            _contract(
                "typed-dates",
                fields={
                    "published": {
                        "enum": [dt.date(2026, 1, 1), "2026-01-01"],
                    }
                },
            ),
        ),
        projects=("atlas",),
        page_type="insight",
    )

    constraint = next(
        item
        for item in resolved.as_dict()["constraints"]
        if item["resolved_rule"] == ["fields", "published", "enum"]
    )
    assert constraint["value"] == [dt.date(2026, 1, 1), "2026-01-01"]
    assert type(constraint["value"][0]) is dt.date
    assert type(constraint["value"][1]) is str


def test_resolve_saved_contracts_loads_language_registry_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "Knowledge Base/_Schema/contracts"
    directory.mkdir(parents=True)
    memory_schema.save_contract(
        tmp_path,
        _contract("global", categories={"config": {"required": True}})
        .contract.as_dict(),
    )
    calls = 0
    original = memory_schema.semantic_language_registry.load_registry

    def counted_load(root: Path):
        nonlocal calls
        calls += 1
        return original(root)

    monkeypatch.setattr(
        memory_schema.semantic_language_registry, "load_registry", counted_load
    )

    result = memory_schema.resolve_saved_contracts(
        tmp_path, projects=("atlas",), page_type="insight"
    )

    assert calls == 1
    assert _constraint(result, ("categories", "config", "required")).value is True
