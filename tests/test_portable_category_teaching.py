"""Portable category teaching contract and bounded surface projections."""

from __future__ import annotations

import json
from pathlib import Path

from exomem import commands, semantic_authoring, semantic_language_registry, semantic_units

CORE_KEYS = tuple(sorted(semantic_language_registry.core_registry().core_categories))


def test_authoring_contract_owns_role_first_portable_category_guidance() -> None:
    contract = semantic_authoring.get_semantic_authoring_contract().as_dict()
    portable = contract["portable_categories"]

    assert contract["version"] > 2
    assert tuple(portable["core_keys"]) == CORE_KEYS
    assert portable["aliases"] == dict(
        semantic_language_registry.core_registry().core_category_aliases
    )
    guidance = json.dumps(portable, ensure_ascii=False).lower()
    for required in (
        "role",
        "domain",
        "exactly one primary category",
        "open",
        "kind",
        "tags",
        "relations",
    ):
        assert required in guidance


def test_contract_examples_are_parseable_nonduplicative_and_generic() -> None:
    portable = semantic_authoring.get_semantic_authoring_contract().as_dict()[
        "portable_categories"
    ]
    role_example = portable["examples"]["role"]
    domain_example = portable["examples"]["domain"]
    rich_example = portable["examples"]["rich"]

    assert "[constraint]" in role_example and "#code" in role_example
    assert "[design]" in domain_example and "#api" in domain_example
    for example, expected in ((role_example, "constraint"), (domain_example, "design")):
        document = semantic_units.parse_semantic_units(example)
        assert len(document.units) == 1
        assert document.units[0].category == expected

    rich = semantic_units.parse_semantic_units(rich_example, validate=False)
    assert len(rich.units) == 1
    assert rich.units[0].kind == "decision"
    assert rich.units[0].anchor
    assert "- tags:" in rich_example
    assert "- relations: supports: [[" in rich_example
    assert "category: decision" not in rich_example.lower()


def test_bootstrap_profiles_teach_full_core_without_vault_leak(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, sentinel in ((first, "Sentinel One"), (second, "Sentinel Two")):
        note = root / "Knowledge Base" / "Notes" / "private.md"
        note.parent.mkdir(parents=True)
        note.write_text(f"# {sentinel}\n", encoding="utf-8")

    compact_left = commands.op_bootstrap(first, profile="compact")["semantic_authoring"]
    compact_right = commands.op_bootstrap(second, profile="compact")["semantic_authoring"]
    full_left = commands.op_bootstrap(first, profile="full")["semantic_authoring"]
    full_right = commands.op_bootstrap(second, profile="full")["semantic_authoring"]

    assert compact_left == compact_right
    assert full_left == full_right
    assert tuple(compact_left["portable_categories"]["core_keys"]) == CORE_KEYS
    compact_text = json.dumps(compact_left["portable_categories"], ensure_ascii=False).lower()
    assert "open" in compact_text and "role" in compact_text and "domain" in compact_text
    assert compact_left["portable_categories"]["examples"]["role"]
    assert "rich" not in compact_left["portable_categories"]["examples"]
    assert full_left["portable_categories"]["examples"]["rich"]
    full_text = json.dumps(full_left["portable_categories"], ensure_ascii=False).lower()
    assert "relations" in full_text
    assert "Sentinel One" not in json.dumps(full_left)
    assert "Sentinel Two" not in json.dumps(full_right)


def test_write_tool_guidance_is_bounded_and_routes_to_full_bootstrap() -> None:
    contract = semantic_authoring.get_semantic_authoring_contract()
    portable = contract.as_dict()["portable_categories"]
    rendered = semantic_authoring.render_tool_guidance("remember")

    assert semantic_authoring.contract_identity(contract) in rendered
    assert portable["short_selection_rule"] in rendered
    assert portable["examples"]["role"] in rendered
    assert 'bootstrap(profile="full")' in rendered
    assert ", ".join(portable["core_keys"]) not in rendered
