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
    epistemic_graph,
    relation_registry,
    semantic_labels,
    semantic_units,
    structured_filters,
)
from exomem import find as find_module
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
    plan = structured_filters.compile_filter({"unit.dimension": {"$eq": "currency"}})

    assert plan.has_unit_predicate  # FLIPS slice5a: was INVALID_FILTER_FIELD, an unknown field


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


# ---------------------------------------------------------------------------
# The `unit.dimension` retrieval filter
# ---------------------------------------------------------------------------

RULES = "Knowledge Base/Notes/Release rules.md"
OLD = "Knowledge Base/Notes/Old outlook.md"
NEW = "Knowledge Base/Notes/New outlook.md"
REGULATION = (
    "schema_version: 1\ndimensions:\n"
    "  regulation:\n    labels: {en: Regulation}\n"
    "    description: What the rules require\n    members: {kinds: [constraint, requirement]}\n"
    "  statements:\n    labels: {en: Statements}\n"
    "    description: What is plainly claimed\n    members: {kinds: [claim]}\n"
)


def _write(vault: Path, rel: str, body: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _outlook_vault(tmp_path: Path, *, graph: bool = False) -> Path:
    vault = tmp_path / "vault"
    page = "---\ntype: insight\n---\n# {title}\n\n{body}\n"
    _write(
        vault,
        RULES,
        page.format(
            title="Release rules",
            body="## Constraint\n\nNo deploys on Friday.\n\n## Requirement\n\nTwo reviewers.\n\n"
            "## Claim\n\nThe launch slips.",
        ),
    )
    _write(
        vault, OLD, page.format(title="Old outlook", body="## Claim\n\nThe launch is in January.")
    )
    _write(
        vault,
        NEW,
        page.format(
            title="New outlook",
            body="## Claim\n\nThe launch is in March.\n\n## Relations\n\n"
            "- supersedes [[Knowledge Base/Notes/Old outlook]]",
        ),
    )
    _write(vault, language_registry.registry_path(vault).relative_to(vault).as_posix(), REGULATION)
    if graph:
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    find_module.clear_cache()
    return vault


def _recall_units(vault: Path, filters: dict, **kw) -> list[tuple[str, str]]:
    hits = find_module.find(
        vault,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="unit",
        filters=filters,
        limit=20,
        **kw,
    )
    return [(Path(hit.parent_path).stem, hit.kind) for hit in hits]


def test_recall_filters_units_by_a_dimension_a_vault_added(tmp_path: Path) -> None:
    vault = _outlook_vault(tmp_path)

    by_key = _recall_units(vault, {"unit.dimension": {"$eq": "regulation"}})
    by_label = _recall_units(vault, {"unit.dimension": {"$eq": "Regulation"}})
    assert sorted(by_key) == [("Release rules", "constraint"), ("Release rules", "requirement")]
    assert by_label == by_key

    excluded = _recall_units(vault, {"unit.dimension": {"$ne": "regulation"}})
    assert ("Release rules", "constraint") not in excluded
    assert ("Release rules", "claim") in excluded

    either = _recall_units(vault, {"unit.dimension": {"$in": ["regulation", "statements"]}})
    assert len(either) == 5


def test_recall_names_the_known_dimensions_when_the_filter_names_none(tmp_path: Path) -> None:
    """Catches a typo that answers with no units, which reads as 'nothing is in that dimension'."""
    vault = _outlook_vault(tmp_path)

    with pytest.raises(structured_filters.FilterError) as raised:
        _recall_units(vault, {"unit.dimension": {"$eq": "regulations"}})

    assert raised.value.code == "INVALID_FILTER_VALUE"
    assert "regulation" in raised.value.expected
    assert "currency" in raised.value.expected


def test_recall_filters_units_by_the_edge_family_of_their_page(tmp_path: Path) -> None:
    vault = _outlook_vault(tmp_path, graph=True)

    current = _recall_units(vault, {"unit.dimension": {"$eq": "currency"}})
    assert sorted(current) == [("New outlook", "claim"), ("Old outlook", "claim")]

    others = _recall_units(vault, {"unit.dimension": {"$ne": "currency"}})
    assert ("Release rules", "claim") in others
    assert ("New outlook", "claim") not in others


def test_a_dimension_that_needs_edges_never_answers_empty_from_a_missing_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a reader that treats an unbuilt sidecar as 'no edges', so every unit looks unreplaced."""
    monkeypatch.setattr(epistemic_graph, "schedule_background_rebuild", lambda *a, **k: None)
    vault = _outlook_vault(tmp_path)

    with pytest.raises(find_module.RetrievalIndexWarming):
        _recall_units(vault, {"unit.dimension": {"$eq": "currency"}})

    # A dimension that names only kinds and keys needs no graph, so it still answers.
    assert _recall_units(vault, {"unit.dimension": {"$eq": "regulation"}})


def test_an_edge_to_a_page_the_caller_cannot_see_gives_no_membership(tmp_path: Path) -> None:
    """Catches a restricted reader learning that a withheld page replaced a visible one."""
    vault = _outlook_vault(tmp_path, graph=True)

    visible = _recall_units(
        vault,
        {"unit.dimension": {"$eq": "currency"}},
        admit_path=lambda path: path != OLD,
    )

    assert visible == []


@pytest.mark.parametrize("operator", ["$contains", "$all", "$gt", "$exists"])
def test_a_dimension_filter_takes_only_member_operators(operator: str) -> None:
    operand: object = True if operator == "$exists" else ["currency"] if operator == "$all" else "a"

    with pytest.raises(structured_filters.FilterError) as raised:
        structured_filters.compile_filter({"unit.dimension": {operator: operand}})

    assert raised.value.code == "INVALID_FILTER_OPERATOR"


def test_a_dimension_filter_takes_only_names() -> None:
    with pytest.raises(structured_filters.FilterError) as raised:
        structured_filters.compile_filter({"unit.dimension": {"$eq": 3}})

    assert raised.value.code == "INVALID_FILTER_VALUE"


def test_a_reader_with_no_dimension_source_refuses_instead_of_matching_nothing() -> None:
    plan = structured_filters.compile_filter({"unit.dimension": {"$eq": "currency"}})
    page = {"path": RULES, "frontmatter": {}, "file_type": "markdown"}
    unit = {"kind": "claim", "row_keys": (), "instance": None}

    with pytest.raises(structured_filters.FilterError) as raised:
        structured_filters.evaluate_filter(plan, page=page, unit=unit)

    assert raised.value.code == "UNSUPPORTED_FILTER_FIELD"


def test_a_served_reader_refuses_the_dimension_filter_by_field() -> None:
    """Membership is read from rows and edges that no catalogue column holds, so a served reader
    names the field rather than scanning the corpus."""
    plan = structured_filters.compile_filter({"unit.dimension": {"$eq": "currency"}})

    eligibility = structured_filters.plan_index_eligibility(plan)

    assert "unit.dimension" in eligibility.unsupported_fields
