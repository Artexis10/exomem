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


# --------------------------------------------------------------------------- #
# Task 3.2 — `evidence_cues` / `evidence_categories`
# --------------------------------------------------------------------------- #

#: The eight roles the deleted `CUE_PATTERNS`/`_CUE_CATEGORIES` table
#: covered, and the roles that ship with neither field.
EVIDENCE_BEARING_ROLES = (
    "preferences",
    "constraints",
    "current_state",
    "recent_change",
    "active_plans",
    "methods",
    "precedents",
    "open_questions",
)
NEITHER_FIELD_ROLES = ("identity", "resources", "people", "location", "baseline", "evidence")


def test_shipped_evidence_bearing_roles_carry_both_fields() -> None:
    registry = context_roles.load_roles()
    for role_id in EVIDENCE_BEARING_ROLES:
        role = registry.roles[role_id]
        assert role.evidence_cues, role_id
        assert role.evidence_categories, role_id


def test_shipped_roles_with_neither_field_ship_empty() -> None:
    registry = context_roles.load_roles()
    for role_id in NEITHER_FIELD_ROLES:
        role = registry.roles[role_id]
        assert role.evidence_cues == ()
        assert role.evidence_categories == frozenset()


def test_evidence_categories_join_by_union_not_replacement(vault: Path) -> None:
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"constraints": {"evidence_categories": ["assumption"]}},
        },
    )
    registry = context_roles.load_roles(vault)

    assert registry.roles["constraints"].evidence_categories == frozenset(
        {"constraint", "requirement", "assumption"}
    )


def test_evidence_cues_join_by_union_not_replacement(vault: Path) -> None:
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"active_plans": {"evidence_cues": ["ich plane"]}},
        },
    )
    registry = context_roles.load_roles(vault)
    cues = registry.roles["active_plans"].evidence_cues

    assert "ich plane" in cues
    assert "i'm planning" in cues  # shipped evidence cues survive the union


def test_an_unregistered_evidence_category_is_dropped_with_a_finding(vault: Path) -> None:
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"constraints": {"evidence_categories": ["not_a_real_category"]}},
        },
    )
    registry = context_roles.load_roles(vault)

    assert "not_a_real_category" not in registry.roles["constraints"].evidence_categories
    assert any(finding["code"] == "invalid_evidence_category" for finding in registry.findings)


def test_a_new_role_may_declare_evidence_cues_and_categories_directly(vault: Path) -> None:
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
                    "evidence_cues": ["in transit"],
                    "evidence_categories": ["fact"],
                }
            },
        },
    )
    registry = context_roles.load_roles(vault)

    assert registry.roles["logistics"].evidence_cues == ("in transit",)
    assert registry.roles["logistics"].evidence_categories == frozenset({"fact"})
    assert registry.findings == ()


# --------------------------------------------------------------------------- #
# Task 3.2 — caps, with findings (design.md decision 6)
# --------------------------------------------------------------------------- #


def test_evidence_cues_cap_is_applied_with_a_finding(vault: Path) -> None:
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {
                "constraints": {"evidence_cues": [f"cue number {i}" for i in range(60)]}
            },
        },
    )
    registry = context_roles.load_roles(vault)

    assert len(registry.roles["constraints"].evidence_cues) == context_roles.MAX_CUES_PER_ROLE
    assert any(finding["code"] == "cap_exceeded" for finding in registry.findings)


def test_cues_cap_is_applied_with_a_finding(vault: Path) -> None:
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"resources": {"cues": [f"haul thing {i}" for i in range(60)]}},
        },
    )
    registry = context_roles.load_roles(vault)

    assert len(registry.roles["resources"].cues) == context_roles.MAX_CUES_PER_ROLE
    assert any(finding["code"] == "cap_exceeded" for finding in registry.findings)


def test_evidence_categories_cap_is_applied_with_a_finding(vault: Path) -> None:
    extra = [
        "fact", "insight", "constraint", "requirement", "assumption", "risk", "problem",
        "question", "action", "technique", "preference", "design", "config", "decision",
    ]
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"constraints": {"evidence_categories": extra}},
        },
    )
    registry = context_roles.load_roles(vault)

    assert (
        len(registry.roles["constraints"].evidence_categories)
        == context_roles.MAX_EVIDENCE_CATEGORIES_PER_ROLE
    )
    assert any(finding["code"] == "cap_exceeded" for finding in registry.findings)


def test_evidence_cue_over_the_character_limit_is_dropped_with_a_finding(vault: Path) -> None:
    long_cue = "x" * (context_roles.MAX_CUE_CHARS + 1)
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"constraints": {"evidence_cues": [long_cue]}},
        },
    )
    registry = context_roles.load_roles(vault)

    assert long_cue not in registry.roles["constraints"].evidence_cues
    assert any(finding["code"] == "entry_too_long" for finding in registry.findings)


def test_a_short_evidence_cue_is_kept_for_selection_but_flagged(vault: Path) -> None:
    """Decision 1: a cue failing the evidence bounds still SELECTS its role
    (every cue matches as a substring); only evidence is stricter, so the
    cue is kept, not dropped, and reported instead."""
    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"constraints": {"evidence_cues": ["an"]}},
        },
    )
    registry = context_roles.load_roles(vault)

    assert "an" in registry.roles["constraints"].evidence_cues
    assert any(finding["code"] == "evidence_cue_too_weak" for finding in registry.findings)


def test_too_many_roles_in_one_override_is_capped_with_a_finding(vault: Path) -> None:
    roles = {
        f"extra_role_{i}": {
            "description": "test",
            "lane": "units",
            "categories": ["fact"],
            "anchor_defaults": ["resource"],
            "cues": [f"extra cue {i}"],
        }
        for i in range(40)
    }
    _override(
        vault, {"schema_version": context_roles.SCHEMA_VERSION, "roles": roles}
    )
    registry = context_roles.load_roles(vault)

    new_roles = [role for role in registry.roles.values() if not role.shipped]
    assert len(new_roles) == context_roles.MAX_ROLES_PER_OVERRIDE
    assert any(finding["code"] == "cap_exceeded" for finding in registry.findings)


def test_oversized_role_override_is_refused_before_parsing(vault: Path) -> None:
    path = context_roles.override_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("schema_version: 1\n" + ("#" * (context_roles.MAX_FILE_BYTES + 1)), encoding="utf-8")
    context_roles.clear_cache()

    registry = context_roles.load_roles(vault)

    assert registry.source == "shipped"
    assert any(finding["code"] == "file_too_large" for finding in registry.findings)


# --------------------------------------------------------------------------- #
# Task 3.2 — selection reads both `cues` and `evidence_cues`
# --------------------------------------------------------------------------- #


def test_selection_matches_evidence_cues_too(vault: Path) -> None:
    from exomem import working_set_resolve

    _override(
        vault,
        {
            "schema_version": context_roles.SCHEMA_VERSION,
            "roles": {"active_plans": {"evidence_cues": ["ich plane"]}},
        },
    )
    registry = context_roles.load_roles(vault)
    analysis = working_set_resolve.analyze_turn("ich plane etwas")
    selected = context_roles.select_roles(registry, anchor_kinds=("hub",), analysis=analysis)

    assert any(item["id"] == "active_plans" and item["source"] == "turn_cue" for item in selected)
