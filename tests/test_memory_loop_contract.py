"""The shared contract every close-memory-loop fixture is scored with.

Pinned here: the evaluator semantics each fixture digest carries (version,
constants and every declared default, since digests omit fields left at
their default), whole-word marker matching, direction-aware truthful edges,
and retained input (Sources, Evidence, episode recaps) never being a home.
"""

from __future__ import annotations

import pytest
from epistemic.memory_loop import contract
from epistemic.memory_loop.contract import (
    EXTENSION,
    Admissible,
    CoLocated,
    EdgeCount,
    EdgeView,
    NewPages,
    NoEdgeBetween,
    NoFutureRecords,
    NoNewPlanning,
    PageView,
    RecordView,
    Select,
    TypedEdge,
    VaultState,
)

SEMANTICS_FINGERPRINT = "287f80dfffe5d0e635b7d5c310f3de6c8e32ecf63a245495a7d4a3e6e87199fc"

#: Every declared default of every contract type. A changed default changes
#: meaning without moving any fixture digest on its own, so it is pinned here
#: and folded into :func:`contract.semantics_fingerprint`.
DECLARED_DEFAULTS = {
    "AttributedLines": {"allow_none": False},
    "Candidate": {"attributed_to": None, "partition": None, "same_home_as": [], "uncertain": False},
    "EdgeView": {"family": ""},
    "EntitySeed": {"aliases": []},
    "FieldIs": {"any_of": [], "empty": False, "equals": [], "tokens": []},
    "LaterUse": {"audience": "owner", "expected_status": None, "wrong": []},
    "LinesCarry": {"select": None},
    "Mentions": {"new_lines": False},
    "NewPages": {"exclude": [], "exclude_types": [], "tolerate": []},
    "NoEdgeBetween": {"forbidden": [], "tolerated": []},
    "NoNewEdge": {"relations": [], "target": None},
    "NoNewMention": {"ignore_links": False, "select": None},
    "NoNewPlanning": {"markers": []},
    "NoteSeed": {"category": "finding", "extra_body": "", "note_type": "insight"},
    "PageView": {"frontmatter": {}},
    "PreCapture": {
        "entities": [],
        "notes": [],
        "records": [],
        "relation_types": [],
        "relations": [],
        "types": [],
    },
    "RecordsSeed": {"description": "Synthetic observed state for a close-memory-loop fixture.", "items": []},
    "RelationTypeSeed": {"direction": "directed"},
    "Select": {
        "any_tokens": [],
        "created_only": False,
        "entity_type": None,
        "key": None,
        "kind": "entity",
        "tokens": [],
    },
    "VaultState": {"edges": []},
}


def test_the_declared_defaults_are_pinned() -> None:
    assert contract._declared_defaults() == DECLARED_DEFAULTS  # noqa: SLF001


def test_the_semantics_fingerprint_is_pinned() -> None:
    assert contract.semantics_fingerprint() == SEMANTICS_FINGERPRINT


@pytest.mark.parametrize(
    "name, value",
    [
        ("GENERIC_RELATIONS", frozenset({"links_to", "mentions"})),
        ("RETAINED_INPUT_ROOTS", ("Knowledge Base/Sources/",)),
        ("SEMANTICS_VERSION", 99),
    ],
)
def test_changing_an_evaluator_constant_moves_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch, name: str, value: object
) -> None:
    monkeypatch.setattr(contract, name, value)

    assert contract.semantics_fingerprint() != SEMANTICS_FINGERPRINT


@pytest.mark.parametrize("name", ["_effects", "_initiation", "_decision_trace", "_uncovered"])
def test_changing_the_observation_gate_moves_the_fingerprint(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from epistemic.memory_loop import observation

    def edited(*args, **kwargs):  # a gate that decides differently
        return None

    monkeypatch.setattr(observation, name, edited)

    assert contract.semantics_fingerprint() != SEMANTICS_FINGERPRINT


@pytest.mark.parametrize("name", ["_SEMANTIC_PHASES", "PROMPT_SLOTS", "HOOK_SCRIPTS"])
def test_changing_an_observation_constant_moves_the_fingerprint(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from epistemic.memory_loop import observation

    monkeypatch.setattr(observation, name, type(getattr(observation, name))())

    assert contract.semantics_fingerprint() != SEMANTICS_FINGERPRINT


@pytest.mark.parametrize(
    "text, marker, found",
    [
        ("The Lowfield tin keeps it fresh", "tin", True),
        ("tasting notes, continued", "tin", False),
        ("a tint of amber", "tin", False),
        ("it may be limescale", "may", True),
        ("maybe next month", "may", False),
        ("deliveries on Thursdays", "thursday", False),
        ("12.5% protein", "12.5", True),
        ("the owner-custody principle", "custody", True),
    ],
)
def test_markers_match_whole_words_only(text: str, marker: str, found: bool) -> None:
    assert contract._has_marker(text, marker) is found  # noqa: SLF001


# --------------------------------------------------------------------------- #
# Truthful edges, read as the product derives them
# --------------------------------------------------------------------------- #

A = "Knowledge Base/Notes/Insights/a.md"
B = "Knowledge Base/Notes/Insights/b.md"


def _page(path: str, title: str, body: str = "") -> PageView:
    return PageView(path=path, page_type="insight", title=title, status="active", entity_type="", aliases=(), body=body)


def _state(*edges: EdgeView, pages: tuple[PageView, ...] = ()) -> VaultState:
    pages = pages or (_page(A, "A"), _page(B, "B"))
    return VaultState(pages={page.path: page for page in pages}, records=(), edges=edges)


def _edge(source: str, relation: str, target: str, status: str = "core", family: str = "") -> EdgeView:
    return EdgeView(
        source=source, relation=relation, target=target, origin="markdown_relation", status=status, family=family
    )


WORLD = {"a": A, "b": B}


def _typed(*admissible: Admissible) -> TypedEdge:
    return TypedEdge(
        key="k", polarity="positive", source=Select(key="a"), target=Select(key="b"), admissible=admissible, reason="r"
    )


def test_an_admissible_relation_passes_only_in_its_direction() -> None:
    forward = _state(_edge(A, "derived_from", B))
    backward = _state(_edge(B, "derived_from", A))
    expectation = _typed(Admissible("derived_from", "forward"))

    assert expectation.evaluate(WORLD, _state(), forward).outcome == "pass"
    assert expectation.evaluate(WORLD, _state(), backward).outcome == "fail"


@pytest.mark.parametrize("relation", ["relates_to", "links_to", "mentions"])
def test_a_generic_relation_is_never_admissible(relation: str) -> None:
    after = _state(_edge(A, relation, B))

    assert _typed(Admissible(relation, "either")).evaluate(WORLD, _state(), after).outcome == "fail"


def test_an_unregistered_relation_is_never_admissible() -> None:
    after = _state(_edge(A, "vault.sells_under", B, status="unregistered"))

    assert _typed(Admissible(EXTENSION, "either")).evaluate(WORLD, _state(), after).outcome == "fail"


def test_the_extension_sentinel_accepts_governed_extensions_but_not_core_relations() -> None:
    governed = _state(_edge(A, "vault.sells_under", B, status="extension"))
    core = _state(_edge(A, "contradicts", B))
    expectation = _typed(Admissible(EXTENSION, "either"))

    assert expectation.evaluate(WORLD, _state(), governed).outcome == "pass"
    assert expectation.evaluate(WORLD, _state(), core).outcome == "fail"


@pytest.mark.parametrize(
    "family", ["ownership", "composition", "supersession", "duplication", "contradiction", "causality"]
)
def test_an_extension_under_a_structural_or_epistemic_parent_is_not_a_governed_meaning(family: str) -> None:
    after = _state(_edge(A, "vault.holds", B, status="extension", family=family))

    assert _typed(Admissible(EXTENSION, "either")).evaluate(WORLD, _state(), after).outcome == "fail"


def test_an_extension_under_a_generic_parent_is_a_governed_meaning() -> None:
    after = _state(_edge(A, "vault.sells_under", B, status="extension", family="relation"))

    assert _typed(Admissible(EXTENSION, "either")).evaluate(WORLD, _state(), after).outcome == "pass"


def test_a_forbidden_relation_also_forbids_extensions_of_its_family() -> None:
    check = NoEdgeBetween(
        key="k", polarity="negative", first=Select(key="a"), second=Select(key="b"), forbidden=("owns",), reason="r"
    )
    owning = _state(_edge(A, "vault.holds_site", B, status="extension", family="ownership"))
    operating = _state(_edge(A, "vault.operates", B, status="extension", family="relation"))

    assert check.evaluate(WORLD, _state(), owning).outcome == "fail"
    assert check.evaluate(WORLD, _state(), operating).outcome == "pass"


def test_edge_count_needs_its_declared_share_of_targets() -> None:
    targets = tuple(Select(key=f"t{i}") for i in range(3))
    world = {"a": A, **{f"t{i}": f"Knowledge Base/Notes/Insights/t{i}.md" for i in range(3)}}
    pages = (_page(A, "A"), *(_page(world[f"t{i}"], f"T{i}") for i in range(3)))
    count = EdgeCount(
        key="k",
        polarity="positive",
        source=Select(key="a"),
        targets=targets,
        admissible=(Admissible("derived_from", "forward"),),
        at_least=2,
        reason="r",
    )
    one = _state(_edge(A, "derived_from", world["t0"]), pages=pages)
    two = _state(_edge(A, "derived_from", world["t0"]), _edge(A, "derived_from", world["t1"]), pages=pages)

    assert count.evaluate(world, _state(pages=pages), one).outcome == "fail"
    assert count.evaluate(world, _state(pages=pages), two).outcome == "pass"


def test_no_edge_between_reads_the_state_after_capture_not_only_new_edges() -> None:
    seeded = _state(_edge(A, "vault.operates", B, status="extension"))
    check = NoEdgeBetween(
        key="k",
        polarity="negative",
        first=Select(key="a"),
        second=Select(key="b"),
        tolerated=("owns", "relates_to", "links_to", "mentions"),
        reason="r",
    )

    assert check.evaluate(WORLD, seeded, seeded).outcome == "fail"
    assert check.evaluate(WORLD, seeded, _state(_edge(A, "owns", B))).outcome == "pass"


# --------------------------------------------------------------------------- #
# Planning is judged in the Planning tree, never in Records
# --------------------------------------------------------------------------- #

BAKES = "Knowledge Base/Records/Bake Log/_collection.md"
TRIALS = "Knowledge Base/Planning/Supplier Trials"
_NO_PLANNING = NoNewPlanning(key="k", polarity="negative", markers=("lowmere",), reason="r")


def _planning_state(*pages: PageView, records: tuple[RecordView, ...] = ()) -> VaultState:
    return VaultState(pages={page.path: page for page in pages}, records=records)


def test_a_records_line_naming_a_possibility_is_not_planning() -> None:
    line = RecordView(collection=BAKES, item_key="r1", fields={"loaf": "rye", "outcome": "might try Lowmere rye"})

    assert _NO_PLANNING.evaluate({}, _planning_state(), _planning_state(records=(line,))).outcome == "pass"


def test_an_item_in_a_new_plan_titled_collection_naming_the_possibility_is_planning() -> None:
    plans = PageView(
        path="Knowledge Base/Records/Flour Plans/_collection.md",
        page_type="collection",
        title="Flour plans",
        status="active",
        entity_type="",
        aliases=(),
        body="",
    )
    item = RecordView(collection=plans.path, item_key="r1", fields={"flour": "Lowmere rye", "when": "next month"})

    after = _planning_state(plans, records=(item,))
    assert _NO_PLANNING.evaluate({}, _planning_state(plans), after).outcome == "fail"


_NO_FUTURE = NoFutureRecords(key="k", polarity="negative", turn_date="2026-09-24", reason="r")


@pytest.mark.parametrize(
    "value, outcome",
    [
        ("2026-10-15", "fail"),
        ("2026-09-25T08:00:00", "fail"),
        ("2026-09-24", "pass"),
        ("2026-09-23", "pass"),
        ("might try it on 2026-10-15", "pass"),
    ],
)
def test_a_new_records_item_dated_after_the_turn_is_misrouted_intent(value: str, outcome: str) -> None:
    item = RecordView(collection=BAKES, item_key="r1", fields={"baked_on": value, "loaf": "rye"})

    assert _NO_FUTURE.evaluate({}, _planning_state(), _planning_state(records=(item,))).outcome == outcome


def test_a_future_date_in_a_prose_line_or_an_existing_item_is_not_a_new_future_record() -> None:
    old = RecordView(collection=BAKES, item_key="r0", fields={"baked_on": "2026-12-01"})
    page = _page(A, "A", body="- [assumption] Might try Lowmere rye on 2026-10-15.")

    assert _NO_FUTURE.evaluate({}, _planning_state(records=(old,)), _planning_state(page, records=(old,))).outcome == "pass"


def test_a_planning_item_naming_the_possibility_is_planning() -> None:
    item = PageView(
        path=f"{TRIALS}/Items/p1.md",
        page_type="plan",
        title="Try Lowmere rye",
        status="active",
        entity_type="",
        aliases=(),
        body="Order a sack of Lowmere rye.",
        frontmatter={"plan_id": "p1"},
    )

    assert _NO_PLANNING.evaluate({}, _planning_state(), _planning_state(item)).outcome == "fail"


def test_a_planning_item_about_something_else_is_not_this_possibility() -> None:
    item = PageView(
        path=f"{TRIALS}/Items/p2.md",
        page_type="plan",
        title="Descale the kettle",
        status="active",
        entity_type="",
        aliases=(),
        body="Monthly.",
        frontmatter={"plan_id": "p2"},
    )

    assert _NO_PLANNING.evaluate({}, _planning_state(), _planning_state(item)).outcome == "pass"
    unscoped = NoNewPlanning(key="k", polarity="negative", reason="r")
    assert unscoped.evaluate({}, _planning_state(), _planning_state(item)).outcome == "fail"


# --------------------------------------------------------------------------- #
# Retained input is never a home
# --------------------------------------------------------------------------- #

RECAP = "Knowledge Base/Sources/Episodes/recap.md"
NOTE = "Knowledge Base/Notes/Insights/flat.md"


def test_retained_input_is_not_a_co_location_home_or_a_new_page() -> None:
    before = _state(pages=(_page(A, "A"),))
    after = _state(
        pages=(
            _page(A, "A"),
            _page(RECAP, "Recap", "- flat on Monday"),
            _page(NOTE, "Flat mornings", "- flat on Monday"),
        )
    )
    homes = (Select(kind="page", created_only=True),)
    co_located = CoLocated(key="k", polarity="positive", groups=(("monday",),), homes=homes, reason="r")
    new_pages = NewPages(key="n", polarity="positive", exactly=1, reason="r")

    assert co_located.evaluate({}, before, after).outcome == "pass"
    assert new_pages.evaluate({}, before, after).outcome == "pass"
    assert contract.is_retained_input(RECAP) and not contract.is_retained_input(NOTE)
