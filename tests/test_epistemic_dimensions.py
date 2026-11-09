"""Epistemic dimensions: membership, the `unit.dimension` retrieval filter on the offline and the
served reader, and the per-dimension report row.

Each test names the wrong answer it catches. Membership is at unit grain: an edge counts at the
unit that carries its row or that its anchored link names, and an edge with a whole page as an end
counts at every unit of that page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import (
    epistemic_dimensions,
    epistemic_graph,
    memory_schema,
    relation_registry,
    semantic_units,
    structured_filters,
)
from exomem import find as find_module
from exomem import semantic_language_registry as language_registry

PATH = "Knowledge Base/Notes/Release outlook.md"


def _proposal(**sections) -> dict:
    return {"schema_version": 1, "categories": {}, "kinds": {}, **sections}


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


def test_another_instances_kind_joins_no_dimension_by_its_key() -> None:
    """Catches membership matching a kind key that another vocabulary instance defines, while
    `unit.kind` treats that entry as distinct: `policy` there is not this vault's `policy`."""
    language = language_registry.load_registry(
        proposal=_proposal(
            kinds={"policy": {"description": "A rule the vault sets"}},
            dimensions={
                "regulation": {
                    "labels": {"en": "Regulation"},
                    "description": "What the rules require",
                    "members": {"kinds": ["policy"]},
                },
            },
        )
    )
    lookups = epistemic_dimensions.membership(language, relation_registry.core_registry())
    plan = structured_filters.compile_filter(
        {"unit.dimension": {"$eq": "regulation"}}
    ).with_dimensions(epistemic_dimensions.UnitDimensions.from_edges(lookups, ()))
    kind_plan = structured_filters.compile_filter({"unit.kind": {"$eq": "policy"}})
    page = {"path": PATH}
    own = {"kind": "policy", "instance": plan.unit_instance, "row_keys": ()}
    foreign = {"kind": "policy", "instance": "partner-vocabulary", "row_keys": ()}

    assert structured_filters.evaluate_filter(plan, page=page, unit=own)
    assert not structured_filters.evaluate_filter(plan, page=page, unit=foreign)
    assert not structured_filters.evaluate_filter(kind_plan, page=page, unit=foreign)


# ---------------------------------------------------------------------------
# The `unit.dimension` retrieval filter
# ---------------------------------------------------------------------------

RULES = "Knowledge Base/Notes/Release rules.md"
OLD = "Knowledge Base/Notes/Old outlook.md"
NEW = "Knowledge Base/Notes/New outlook.md"
FORECAST = "Knowledge Base/Notes/Forecast.md"
DECISIONS = "Knowledge Base/Notes/Decision log.md"
REVIEW = "Knowledge Base/Notes/Review.md"
REGULATION = (
    "schema_version: 1\ndimensions:\n"
    "  regulation:\n    labels: {en: Regulation}\n"
    "    description: What the rules require\n    members: {kinds: [constraint, requirement]}\n"
    "  statements:\n    labels: {en: Statements}\n"
    "    description: What is plainly claimed\n    members: {kinds: [claim]}\n"
)
PAGE = "---\ntype: insight\n---\n# {title}\n\n{body}\n"


def _write(vault: Path, rel: str, body: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _outlook_vault(tmp_path: Path, *, graph: bool = False) -> Path:
    """Release rules with no edge; a page-level `supersedes` between two outlooks of two claims
    each, written in the newer page's relations section."""
    from conftest import initialize_vault_state_offline

    vault = tmp_path / "vault"
    vault.mkdir()
    initialize_vault_state_offline(vault, source="dimension fixture")
    _write(
        vault,
        RULES,
        PAGE.format(
            title="Release rules",
            body="## Constraint\n\nNo deploys on Friday.\n\n## Requirement\n\nTwo reviewers.\n\n"
            "## Claim\n\nThe launch slips.",
        ),
    )
    _write(
        vault,
        OLD,
        PAGE.format(
            title="Old outlook",
            body="## Claim\n\nThe launch is in January.\n\n## Claim\n\nThe budget is fixed.",
        ),
    )
    _write(
        vault,
        NEW,
        PAGE.format(
            title="New outlook",
            body="## Claim\n\nThe launch is in March.\n\n## Claim\n\nThe team is five.\n\n"
            "## Relations\n\n- supersedes [[Knowledge Base/Notes/Old outlook]]",
        ),
    )
    _write(vault, language_registry.registry_path(vault).relative_to(vault).as_posix(), REGULATION)
    if graph:
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    find_module.clear_cache()
    return vault


def _unit_edges_vault(tmp_path: Path) -> Path:
    """Edges at unit grain: a row on one of three claims, an anchored link between two decisions
    of one page, and an anchored link from another page to one decision."""
    vault = _outlook_vault(tmp_path)
    _write(
        vault,
        FORECAST,
        PAGE.format(
            title="Forecast",
            body="## Claim\n- relations: supersedes: [[Knowledge Base/Notes/Old outlook]]\n\n"
            "Revenue doubles.\n\n## Claim\n\nCosts hold.\n\n## Claim\n\nHiring pauses.",
        ),
    )
    _write(
        vault,
        DECISIONS,
        PAGE.format(
            title="Decision log",
            body="## Decision\n- id: before\n\nShip in January.\n\n"
            "## Decision\n- id: after\n- relations: supersedes: [[#before]]\n\nShip in March.\n\n"
            "## Decision\n- id: hiring\n\nHire two.",
        ),
    )
    _write(
        vault,
        REVIEW,
        PAGE.format(
            title="Review",
            body="## Claim\n- relations: supports: [[Knowledge Base/Notes/Decision log#hiring]]\n\n"
            "Two hires fit the budget.",
        ),
    )
    (vault / NEW).unlink()
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

    statements = _recall_units(vault, {"unit.dimension": {"$eq": "statements"}})
    either = _recall_units(vault, {"unit.dimension": {"$in": ["regulation", "statements"]}})
    assert sorted(either) == sorted(by_key + statements)


def test_recall_names_the_known_dimensions_when_the_filter_names_none(tmp_path: Path) -> None:
    """Catches a typo that answers with no units, which reads as 'nothing is in that dimension'."""
    vault = _outlook_vault(tmp_path)

    with pytest.raises(structured_filters.FilterError) as raised:
        _recall_units(vault, {"unit.dimension": {"$eq": "regulations"}})

    assert raised.value.code == "INVALID_FILTER_VALUE"
    assert "regulation" in raised.value.expected
    assert "currency" in raised.value.expected


def test_a_page_level_edge_reaches_every_unit_of_both_pages(tmp_path: Path) -> None:
    """Catches a page-level edge that reaches only the first unit of a page, or only its source."""
    vault = _outlook_vault(tmp_path, graph=True)

    current = _recall_units(vault, {"unit.dimension": {"$eq": "currency"}})
    assert sorted(current) == [
        ("New outlook", "claim"), ("New outlook", "claim"),
        ("Old outlook", "claim"), ("Old outlook", "claim"),
    ]

    others = _recall_units(vault, {"unit.dimension": {"$ne": "currency"}})
    assert ("Release rules", "claim") in others
    assert ("New outlook", "claim") not in others


def _recall_texts(vault: Path, filters: dict) -> list[tuple[str, str]]:
    hits = find_module.find(
        vault, query="", scope="kb-only", mode="keyword", result_level="unit",
        filters=filters, limit=50,
    )
    return sorted((Path(hit.parent_path).stem, hit.content.strip()) for hit in hits)


def test_an_edge_row_puts_only_the_unit_that_carries_it_in_the_dimension(tmp_path: Path) -> None:
    """Catches the page-grain rule, which put all three forecast claims in `currency` when only
    the first carries the `supersedes` row. The row's link has no anchor, so every unit of the
    superseded page is an end."""
    vault = _unit_edges_vault(tmp_path)

    current = _recall_texts(vault, {"unit.dimension": {"$eq": "currency"}})

    assert ("Forecast", "Revenue doubles.") in current
    assert ("Forecast", "Costs hold.") not in current
    assert ("Forecast", "Hiring pauses.") not in current
    assert ("Old outlook", "The launch is in January.") in current
    assert ("Old outlook", "The budget is fixed.") in current


def test_an_edge_between_two_units_of_one_page_puts_both_in_the_dimension(tmp_path: Path) -> None:
    """Catches a reader that drops an edge whose two ends are on one page, so a decision log that
    supersedes its own earlier decision has neither decision in `currency`."""
    vault = _unit_edges_vault(tmp_path)

    current = _recall_texts(vault, {"unit.dimension": {"$eq": "currency"}})

    assert ("Decision log", "Ship in January.") in current
    assert ("Decision log", "Ship in March.") in current
    assert ("Decision log", "Hire two.") not in current


def test_an_inbound_anchored_edge_reaches_only_the_unit_it_names(tmp_path: Path) -> None:
    """Catches an inbound edge that spreads to every unit of the target page."""
    vault = _unit_edges_vault(tmp_path)

    argued = _recall_texts(vault, {"unit.dimension": {"$eq": "argument"}})

    assert argued == [("Decision log", "Hire two."), ("Review", "Two hires fit the budget.")]


def test_a_vault_extension_edge_reaches_its_parents_dimension_through_the_graph(
    tmp_path: Path,
) -> None:
    """Catches a graph read that asks only for shipped keys and so misses a vault's own relation."""
    vault = _outlook_vault(tmp_path)
    extensions = relation_registry.extension_registry_path(vault)
    extensions.parent.mkdir(parents=True, exist_ok=True)
    extensions.write_text(
        "schema_version: 1\nextensions:\n"
        "  x.overrules: {parent: supersedes, description: Overrules a decision}\n",
        encoding="utf-8",
    )
    new = vault / NEW
    new.write_text(new.read_text().replace("- supersedes [[", "- x.overrules [["), encoding="utf-8")
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    find_module.clear_cache()

    current = _recall_units(vault, {"unit.dimension": {"$eq": "currency"}})

    assert sorted({page for page, _kind in current}) == ["New outlook", "Old outlook"]


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


def test_a_dimension_filter_takes_only_member_operators() -> None:
    """Catches `$contains` read as a substring of a dimension name rather than refused."""
    with pytest.raises(structured_filters.FilterError) as raised:
        structured_filters.compile_filter({"unit.dimension": {"$contains": "curr"}})

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


def test_a_vault_may_remove_its_own_dimension_and_a_filter_naming_it_lists_the_rest(
    tmp_path: Path,
) -> None:
    """Catches a drop guard that blocks the reviewed save from removing a dimension no page
    stores, and a filter that then answers the removed name with no units instead of refusing."""
    from exomem import commands
    from exomem.governance.principal import library_scope

    vault = _outlook_vault(tmp_path)
    current = language_registry.load_registry(vault)
    proposal = language_registry.registry_proposal(current)
    del proposal["dimensions"]["regulation"]

    with library_scope():
        commands.op_schema_memory(
            vault,
            operation="infer",
            subject="categories",
            save=True,
            proposal=proposal,
            expected_hash=current.content_hash,
        )
    find_module.clear_cache()

    with pytest.raises(structured_filters.FilterError) as raised:
        _recall_units(vault, {"unit.dimension": {"$eq": "regulation"}})
    assert raised.value.code == "INVALID_FILTER_VALUE"
    assert "statements" in raised.value.expected
    assert "regulation" not in raised.value.expected


# ---------------------------------------------------------------------------
# The served reader: no corpus scan
# ---------------------------------------------------------------------------


@pytest.fixture
def served_vault(tmp_path: Path, warm_managed_cell, walk_sentinel, monkeypatch):
    """The unit-edge vault, its offline answers, then the vault as the live cell serves it, with
    the scan oracle withdrawn and every enumeration of the corpus counted."""
    from exomem.vault import kb_dirname

    vault = _unit_edges_vault(tmp_path)
    offline = {
        name: _recall_texts(vault, filters)
        for name, filters in _SERVED_FILTERS.items()
    }
    # Flushed before warming, which seeds the freshness registry the served reader checks.
    find_module.clear_cache()
    warm_managed_cell(vault)

    def no_scan(*_args, **_kwargs):
        raise AssertionError("the served reader answered a filter by reading every page")

    monkeypatch.setattr(find_module, "_eligible_filter_paths", no_scan)
    sentinel = walk_sentinel(vault, vault / kb_dirname())
    return vault, offline, sentinel


_SERVED_FILTERS = {
    "or": {"$or": [{"unit.dimension": {"$eq": "regulation"}}, {"unit.kind": {"$eq": "decision"}}]},
    "not": {"$not": {"unit.dimension": {"$in": ["regulation", "statements"]}}},
    "wildcard": {"unit.dimension": {"$eq": "kind"}},
    "currency": {"unit.dimension": {"$eq": "currency"}},
    "currency_and_kind": {
        "$and": [{"unit.dimension": {"$eq": "currency"}}, {"unit.kind": {"$eq": "decision"}}]
    },
}


def test_a_served_search_rewrites_a_dimension_the_index_answers_anywhere_in_the_filter(
    served_vault,
) -> None:
    """Catches a served reader that refuses a dimension of kinds and keys, or answers it by
    reading pages, or answers it differently from the offline reader under `$or` and `$not`."""
    vault, offline, sentinel = served_vault

    for name in ("or", "not", "wildcard"):
        sentinel.reset()
        assert _recall_texts(vault, _SERVED_FILTERS[name]) == offline[name], name
        assert sentinel.count == 0, f"{name}: {sentinel.report()}"


def test_a_served_search_checks_an_edge_dimension_on_its_hits_with_one_graph_read(
    served_vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a served reader that refuses an edge dimension, reads the corpus for it, or reads
    the graph once for each hit."""
    vault, offline, sentinel = served_vault
    reads: list[object] = []
    real = epistemic_graph.EpistemicGraphIndex.unit_edges

    def counted(self, *args, **kwargs):
        reads.append(kwargs.get("pages"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "unit_edges", counted)

    for name in ("currency", "currency_and_kind"):
        sentinel.reset()
        reads.clear()
        assert _recall_texts(vault, _SERVED_FILTERS[name]) == offline[name], name
        assert sentinel.count == 0, f"{name}: {sentinel.report()}"
        assert len(reads) == 1, name
    assert ("Decision log", "Ship in March.") in offline["currency"]
    assert ("Forecast", "Costs hold.") not in offline["currency"]


def test_a_served_search_says_when_its_dimension_check_left_fewer_hits_than_its_limit(
    served_vault,
) -> None:
    """Catches a reply that presents the units left after the check as every unit in the
    dimension, when the limit cut the ranking before the check."""
    from exomem import commands

    vault, _offline, _sentinel = served_vault

    def ask(limit: int) -> dict:
        return commands.op_ask_memory(
            vault, query="hire", mode="keyword", scope="kb-only", result_level="unit",
            filters={"unit.dimension": {"$eq": "currency"}}, limit=limit,
        )

    cut = ask(2)
    assert cut["hits"] == []
    assert "0 of the 2 best matches are in the requested dimension" in cut["user_summary"]
    assert "more may exist" in cut["user_summary"]
    assert "notice" in cut
    # A ranking shorter than the limit was checked whole, so nothing more can exist.
    whole = ask(10)
    assert whole["user_summary"] == "Found no matches."
    assert "notice" not in whole


def test_a_served_search_refuses_an_edge_dimension_under_or_and_not(served_vault) -> None:
    """Catches a served reader that scans the corpus for an edge dimension it cannot check on
    its hits, or refuses it with a message that does not say how to ask instead."""
    vault, _offline, sentinel = served_vault

    for filters in (
        {"$or": [{"unit.dimension": {"$eq": "currency"}}, {"unit.kind": {"$eq": "claim"}}]},
        {"$not": {"unit.dimension": {"$eq": "currency"}}},
    ):
        sentinel.reset()
        with pytest.raises(structured_filters.FilterError) as raised:
            _recall_texts(vault, filters)
        assert raised.value.code == "UNSUPPORTED_FILTER_FIELD"
        assert "top level" in raised.value.remediation
        assert sentinel.count == 0, sentinel.report()


# ---------------------------------------------------------------------------
# The per-dimension report row
# ---------------------------------------------------------------------------


def _report_row(vault: Path, key: str) -> dict:
    report = memory_schema.infer_category_registry(vault)
    return next(row for row in report["dimensions"] if row["key"] == key)


def test_a_vault_dimension_has_a_report_row_that_agrees_with_its_filter(tmp_path: Path) -> None:
    """The spec's `regulation` scenario: members, filter and report all come from the registry.

    Catches a report that counts by its own rule, so the row says 2 while the filter returns 3.
    """
    vault = _outlook_vault(tmp_path)

    row = _report_row(vault, "regulation")
    hits = _recall_units(vault, {"unit.dimension": {"$eq": "regulation"}})

    assert row["members"] == {"kinds": ["constraint", "requirement"]}
    assert row["shipped"] is False
    assert (row["unit_count"], row["page_count"], row["edges"]) == (2, 1, "not_needed")
    assert row["unit_count"] == len(hits)


def test_a_dimension_that_needs_edges_counts_the_units_its_filter_returns(
    tmp_path: Path,
) -> None:
    """Catches a report row that counts every unit of a page an edge touches, so it says 9 while
    the filter returns 6."""
    vault = _unit_edges_vault(tmp_path)

    row = _report_row(vault, "currency")
    hits = _recall_texts(vault, {"unit.dimension": {"$eq": "currency"}})

    assert (row["unit_count"], row["page_count"], row["edges"]) == (len(hits), 3, "counted")


def test_a_dimension_that_needs_edges_reports_unknown_and_its_repair_when_the_graph_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a report that shows 0 units for 'currency' because the sidecar is not built yet,
    or that leaves the agent with no way to build it."""
    scheduled: list[Path] = []
    monkeypatch.setattr(
        epistemic_graph,
        "schedule_background_rebuild",
        lambda vault_root, **_kwargs: scheduled.append(vault_root),
    )
    vault = _outlook_vault(tmp_path)

    row = _report_row(vault, "currency")

    assert (row["unit_count"], row["page_count"], row["edges"]) == (None, None, "unavailable")
    assert row["repair"]["tool"] == "maintain_memory"
    assert row["repair"]["args"]["mode"] == "reconcile"
    assert scheduled == [vault]
    # A dimension that edges do not decide still counts, and needs no repair.
    regulation = _report_row(vault, "regulation")
    assert regulation["unit_count"] == 2
    assert "repair" not in regulation
