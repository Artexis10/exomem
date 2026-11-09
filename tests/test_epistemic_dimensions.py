"""Dimensions that slice 5a of `make-epistemic-language-dynamic` adds: the registry, membership and
the `unit.dimension` retrieval filter.

This module began as the before-picture, in the style of `test_epistemic_belief_keys`: every
`# CORE` value never changes, and a `# FLIPS slice5a` value is one that the slice edited on
purpose. The comment on each flipped line names the value it held before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import (
    epistemic_dimensions,
    relation_registry,
    semantic_labels,
    semantic_units,
    structured_filters,
)
from exomem import semantic_language_registry as language_registry

PATH = "Knowledge Base/Notes/Release outlook.md"


def _proposal(**sections) -> dict:
    return {"schema_version": 1, "categories": {}, "kinds": {}, **sections}


def test_a_dimensions_section_in_the_vault_registry() -> None:
    registry = language_registry.load_registry(
        proposal=_proposal(
            dimensions={
                "regulation": {
                    "labels": {"en": "Regulation"},
                    "description": "What the rules require",
                    "members": {"kinds": ["constraint", "requirement"]},
                },
            }
        )
    )

    assert (
        registry.findings == ()
    )  # FLIPS slice5a: was [("unknown_field", "dimensions")] as an error
    assert "regulation" in registry.label_keys(
        semantic_labels.DIMENSIONS
    )  # FLIPS slice5a: was unavailable


def test_the_filter_field_unit_dimension() -> None:
    with pytest.raises(structured_filters.FilterError) as raised:
        structured_filters.compile_filter({"unit.dimension": {"$eq": "currency"}})

    assert raised.value.code == "INVALID_FILTER_FIELD"  # FLIPS slice5a: was "INVALID_FILTER_FIELD"


# ---------------------------------------------------------------------------
# Membership: one function, three ways in
# ---------------------------------------------------------------------------


def _unit(markdown: str, *, language=None, clean: bool = True):
    registry = language or language_registry.core_registry()
    document = semantic_units.parse_semantic_units(markdown, path=PATH, language_registry=registry)
    if clean:
        assert document.errors == ()
    (unit,) = document.units
    return unit


def _relations_with_extensions(tmp_path: Path) -> relation_registry.RelationRegistry:
    path = relation_registry.extension_registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        "schema_version: 1\nextensions:\n"
        "  x.overrules: {parent: supersedes, description: Overrules a decision}\n"
        "  x.leans_on: {parent: relates_to, family: supersession, description: Leans on}\n",
        encoding="utf-8",
    )
    return relation_registry.load_registry(tmp_path)


def _dimensions(unit, relations=None, language=None, edges=()) -> frozenset[str]:
    return epistemic_dimensions.dimensions_of(
        unit,
        language=language or language_registry.core_registry(),
        relations=relations or relation_registry.core_registry(),
        edge_relations=edges,
    )


def test_a_unit_belongs_to_the_dimensions_that_list_its_kind() -> None:
    belief = _unit(
        "# T\n\n## Belief\n- credence_holder: Finance lead\n- credence: likely\n\nIt slips.\n"
    )
    claim = _unit("# T\n\n## Claim\n\nThe launch slips.\n")

    assert "uncertainty" in _dimensions(belief)
    assert "uncertainty" not in _dimensions(claim)


def test_a_unit_belongs_to_the_dimensions_that_list_a_key_it_carries() -> None:
    attributed = _unit("# T\n\n## Claim\n- asserted_by: Acme Hosting\n\nThe service is stable.\n")
    anonymous = _unit("# T\n\n## Claim\n\nThe service is stable.\n")

    assert "attribution" in _dimensions(attributed)
    assert "attribution" not in _dimensions(anonymous)


def test_a_unit_belongs_to_the_dimension_of_an_edge_family_it_ends() -> None:
    claim = _unit("# T\n\n## Claim\n\nThe launch slips.\n")

    assert "currency" not in _dimensions(claim)
    assert "currency" in _dimensions(claim, edges=["supersedes"])
    # The inbound end of a `supports` edge is a member of `argument` just as its source is.
    assert "argument" in _dimensions(claim, edges=["supports"])


def test_a_vault_extension_belongs_through_its_parents_family_never_its_saved_one(
    tmp_path: Path,
) -> None:
    """Catches a grandfathered extension that moves a unit into a dimension its parent is not in."""
    relations = _relations_with_extensions(tmp_path)
    claim = _unit("# T\n\n## Claim\n\nThe launch slips.\n")

    # `x.overrules` rolls up to `supersedes`; `x.leans_on` saved a family but its parent has another.
    assert "currency" in _dimensions(claim, relations, edges=["x.overrules"])
    assert "currency" not in _dimensions(claim, relations, edges=["x.leans_on"])
    assert "currency" not in _dimensions(claim, relations, edges=["x.unregistered"])


def test_one_unit_belongs_to_several_dimensions() -> None:
    decision = _unit("# T\n\n## Decision\n- verdict: confirmed\n- asserted_by: Ops\n\nShip.\n")

    assert _dimensions(decision, edges=["supersedes", "supports"]) == {
        "kind",
        "correction",
        "attribution",
        "currency",
        "argument",
    }


def test_a_vault_dimension_names_its_members_with_no_code_change() -> None:
    language = language_registry.load_registry(
        proposal=_proposal(
            dimensions={
                "regulation": {
                    "labels": {"en": "Regulation"},
                    "description": "What the rules require",
                    "members": {"kinds": ["constraint", "requirement"]},
                },
            }
        )
    )
    constraint = _unit("# T\n\n## Constraint\n\nNo deploys on Friday.\n", language=language)
    claim = _unit("# T\n\n## Claim\n\nThe launch slips.\n", language=language)

    assert "regulation" in _dimensions(constraint, language=language)
    assert "regulation" not in _dimensions(claim, language=language)


def test_the_kind_wildcard_covers_a_kind_the_vault_adds() -> None:
    """Catches a wildcard that stops at the shipped kinds, so a vault kind has no `kind` dimension."""
    language = language_registry.load_registry(
        proposal=_proposal(kinds={"policy": {"description": "A rule the vault sets"}})
    )
    policy = _unit("# T\n\n## Policy\n\nReview twice.\n", language=language)

    assert _dimensions(policy, language=language) == {"kind"}


def test_a_dimension_the_vault_cannot_load_gives_no_membership() -> None:
    """The shipped dimensions keep answering when the vault's own section has an error."""
    language = language_registry.load_registry(
        proposal=_proposal(
            dimensions={
                "regulation": {
                    "labels": {"en": "Regulation"},
                    "description": "What the rules require",
                    "members": {"kinds": ["rule"]},
                },
            }
        )
    )
    constraint = _unit(
        "# T\n\n## Constraint\n\nNo deploys on Friday.\n", language=language, clean=False
    )

    assert _dimensions(constraint, language=language) == {"kind"}
