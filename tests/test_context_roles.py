"""Task 3.1 — the versioned context-role registry and deterministic selection.

Roles are retrieval LENSES, not code and not a model's opinion: the vocabulary
ships as a reviewed YAML registry, a vault may add to it or narrow its defaults
but never remove a shipped role, and a broken override falls back to the shipped
registry with the failure visible in the packet. Selection is a pure function of
the resolved anchor kinds and the turn's cues.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from exomem import context_roles

SHIPPED_VOCABULARY = (
    "identity",
    "preferences",
    "constraints",
    "resources",
    "current_state",
    "recent_change",
    "active_plans",
    "methods",
    "precedents",
    "people",
    "location",
    "baseline",
    "evidence",
    "open_questions",
)


def _override(vault: Path, payload: object) -> None:
    path = context_roles.override_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    context_roles.clear_cache()


# --------------------------------------------------------------------------- #
# The shipped registry
# --------------------------------------------------------------------------- #


def test_shipped_registry_loads_with_the_declared_vocabulary() -> None:
    registry = context_roles.load_roles()

    assert registry.source == "shipped"
    assert registry.findings == ()
    assert tuple(registry.roles) == SHIPPED_VOCABULARY
    assert len(registry.roles) == 14
    for role in registry.roles.values():
        assert role.description
        assert role.lane in context_roles.LANES
        assert isinstance(role.categories, frozenset)
        assert all(kind in context_roles.ANCHOR_KINDS for kind in role.anchor_defaults)


def test_scaffold_and_plugin_copies_are_byte_identical() -> None:
    repo = Path(__file__).resolve().parents[1]
    scaffold = repo / "src" / "exomem" / "_scaffold" / "_Schema" / "context-roles.yaml"
    plugin = repo / "plugins" / "claude-code" / "skills" / "exomem" / "context-roles.yaml"

    assert scaffold.is_file()
    assert plugin.is_file()
    assert scaffold.read_bytes() == plugin.read_bytes()


def test_registry_hash_is_stable_and_changes_with_the_registry(vault: Path) -> None:
    shipped = context_roles.load_roles(vault)
    again = context_roles.load_roles(vault)
    assert shipped.roles_hash == again.roles_hash

    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"resources": {"add_cues": ["haul"]}},
        },
    )
    changed = context_roles.load_roles(vault)

    assert changed.roles_hash != shipped.roles_hash
    assert changed.source == "vault"


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #


def test_override_adds_a_role_without_removing_one(vault: Path) -> None:
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {
                "logistics": {
                    "description": "Freight and depot movement",
                    "lane": "units",
                    "categories": ["fact"],
                    "anchor_defaults": ["resource"],
                    "cues": ["freight"],
                }
            },
        },
    )
    registry = context_roles.load_roles(vault)

    assert "logistics" in registry.roles
    assert "people" in registry.roles
    assert registry.findings == ()


def test_override_may_narrow_defaults_but_not_remove_a_shipped_role(vault: Path) -> None:
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {
                "people": {"anchor_defaults": []},
                "resources": {"remove": True},
            },
        },
    )
    registry = context_roles.load_roles(vault)

    assert registry.roles["people"].anchor_defaults == frozenset()
    assert "resources" in registry.roles
    assert any(finding["code"] == "role_removal_refused" for finding in registry.findings)


def test_override_cannot_rename_a_shipped_role(vault: Path) -> None:
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"people": {"id": "humans"}},
        },
    )
    registry = context_roles.load_roles(vault)

    assert "people" in registry.roles
    assert "humans" not in registry.roles
    assert any(finding["code"] == "unknown_field" for finding in registry.findings)


def test_broken_override_falls_back_to_the_shipped_registry(vault: Path) -> None:
    _override(vault, "roles: [this: is not: valid yaml")
    registry = context_roles.load_roles(vault)

    assert registry.source == "shipped"
    assert tuple(registry.roles) == SHIPPED_VOCABULARY
    assert any(finding["code"] == "invalid_yaml" for finding in registry.findings)


def test_no_server_component_mutates_the_registry() -> None:
    """Review-gated evolution: the loader exposes no writer."""
    assert not [name for name in dir(context_roles) if name.startswith("save")]
    assert not hasattr(context_roles, "write_roles")


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def test_selection_is_anchor_defaults_union_turn_cues_in_registry_order() -> None:
    from exomem import working_set_resolve

    registry = context_roles.load_roles()
    analysis = working_set_resolve.analyze_turn("I'm planning to tow the cargo sled — how do I?")
    selected = context_roles.select_roles(registry, anchor_kinds=("resource",), analysis=analysis)

    ids = [item["id"] for item in selected]
    assert "resources" in ids
    assert "current_state" in ids
    assert "constraints" in ids
    assert "active_plans" in ids
    assert "methods" in ids
    sources = {item["id"]: item["source"] for item in selected}
    assert sources["resources"] == "anchor_default"
    assert sources["active_plans"] == "turn_cue"
    assert ids == sorted(ids, key=lambda name: tuple(registry.roles).index(name))


def test_selection_is_bounded_to_six_roles() -> None:
    from exomem import working_set_resolve

    registry = context_roles.load_roles()
    analysis = working_set_resolve.analyze_turn(
        "I'm planning to, how do I, currently, prefer, last time, why does, still, can i?"
    )
    selected = context_roles.select_roles(
        registry,
        anchor_kinds=("resource", "entity", "hub", "plan", "collection", "project"),
        analysis=analysis,
    )

    assert len(selected) <= context_roles.MAX_SELECTED_ROLES == 6


def test_same_turn_same_roles() -> None:
    from exomem import working_set_resolve

    registry = context_roles.load_roles()
    analysis = working_set_resolve.analyze_turn("I'm burning through AI usage again")
    first = context_roles.select_roles(registry, anchor_kinds=("resource",), analysis=analysis)
    second = context_roles.select_roles(registry, anchor_kinds=("resource",), analysis=analysis)

    assert first == second


def test_selection_without_an_anchor_is_empty() -> None:
    from exomem import working_set_resolve

    registry = context_roles.load_roles()
    analysis = working_set_resolve.analyze_turn("how do I do this")

    assert context_roles.select_roles(registry, anchor_kinds=(), analysis=analysis) == ()


def test_scaffold_no_leak_gate_passes_for_the_new_registry() -> None:
    """The shipped registry must stay generic — no personal or brand tokens."""
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_scaffold_no_leak.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


@pytest.mark.parametrize("field", ["lane", "categories"])
def test_unknown_lane_or_category_shape_is_reported(vault: Path, field: str) -> None:
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"resources": {field: 17}},
        },
    )
    registry = context_roles.load_roles(vault)

    assert registry.findings
    assert "resources" in registry.roles
