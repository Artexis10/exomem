"""Dimension groups: a search that filters units by an epistemic dimension returns that
dimension's view, ordered and marked by the dimension's display behaviours.

Each test names the wrong answer it catches. The fixture vault holds one decision that supersedes
another, two open questions of which another page answers one, a hypothesis with a verdict, a
claim with its source and its asserter, and four timeline events ordered by dates and by
`precedes` and `follows` edges.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_governance_egress import _external, _reset_caches, write_rule, write_scope

from exomem import (
    commands,
    context_refs,
    dimension_display,
    epistemic_dimensions,
    epistemic_graph,
    memory_schema,
    relation_registry,
)
from exomem import find as find_module
from exomem import semantic_language_registry as language_registry
from exomem.governance import egress, principal

NOTES = "Knowledge Base/Notes"
PAGE = "---\ntype: insight\n---\n# {title}\n\n{body}\n"
PAGES = {
    "Decision log": (
        "## Decision\n- id: before\n\nShip in January.\n\n"
        "## Decision\n- id: after\n- relations: supersedes: [[#before]]\n\nShip in March."
    ),
    "Questions": (
        "## Open question\n- id: beta\n\nDoes the launch need a beta?\n\n"
        "## Open question\n- id: support\n\nWho owns support after launch?\n\n"
        "## Hypothesis\n- verdict: confirmed\n\nUsers prefer a March launch."
    ),
    "Answers": (
        f"## Finding\n- relations: answers: [[{NOTES}/Questions#beta]]\n\n"
        "The launch needs a beta for two weeks."
    ),
    "Survey": "## Claim\n\nNine of ten users asked for exports.",
    "Analysis": (
        f"## Claim\n- asserted_by: Research team\n- relations: derived_from: [[{NOTES}/Survey]]\n\n"
        "Exports come first on the roadmap."
    ),
    "Timeline": (
        "## Timeline event\n- id: launch\n- occurred_at: 2026-03-01\n\nThe launch happens.\n\n"
        "## Timeline event\n- id: kickoff\n- occurred_at: 2026-01-10\n\nThe project kicks off.\n\n"
        "## Timeline event\n- id: review\n- relations: precedes: [[#launch]]\n\n"
        "The security review passes.\n\n"
        "## Timeline event\n- id: retro\n- relations: follows: [[#launch]]\n\n"
        "The team holds a retrospective."
    ),
}


def _write(vault: Path, rel: str, body: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _vault(
    tmp_path: Path, pages: dict[str, str], *, name: str = "vault", registry: str | None = None,
    graph: bool = True,
) -> Path:
    """A vault of `pages` by title under the notes folder, with an optional vault registry of
    the semantic language, and its graph built unless `graph` is false."""
    from conftest import initialize_vault_state_offline

    vault = tmp_path / name
    vault.mkdir(parents=True)
    initialize_vault_state_offline(vault, source="dimension display fixture")
    for title, body in pages.items():
        _write(vault, f"{NOTES}/{title}.md", PAGE.format(title=title.rsplit("/", 1)[-1], body=body))
    if registry is not None:
        _write(vault, language_registry.registry_path(vault).relative_to(vault).as_posix(), registry)
    if graph:
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    find_module.clear_cache()
    return vault


def _display_vault(tmp_path: Path) -> Path:
    return _vault(tmp_path, PAGES)


def _search(vault: Path, filters: dict | None, **kwargs) -> dict:
    return commands.op_ask_memory(
        vault,
        **{
            "query": "", "mode": "keyword", "scope": "kb-only", "result_level": "unit",
            "filters": filters, "limit": 20, **kwargs,
        },
    )


def _view(reply: dict) -> list[tuple[str, str]]:
    return [(hit["parent_title"], hit["excerpt"]) for hit in reply["hits"]]


def _marks(reply: dict) -> dict[str, dict | None]:
    """Each hit's marks, by the hit's excerpt."""
    return {hit["excerpt"]: hit.get("display") for hit in reply["hits"]}


def _packed(reply: dict) -> dict[str, dict]:
    """Each unit of a deep search's pack, by its unit ref."""
    return {
        unit["unit_ref"]: unit
        for page in reply["pack"]["semantic_units"].values()
        for unit in page["units"]
    }


def _ref(title: str, anchor: str) -> str:
    return f"{context_refs.vault_ref(f'{NOTES}/{title}.md')}#{anchor}"


# The fields every unit hit of these searches carries, besides `verdict` and `display`.
UNIT_FIELDS = [
    "category", "dimension", "excerpt", "kind", "parent_path", "parent_status", "parent_title",
    "parent_type", "parent_updated", "result_type", "source_anchor", "unit_ref",
]


def test_a_search_by_dimension_returns_the_dimensions_view(tmp_path: Path) -> None:
    """A search filtered by each shipped dimension that names a display behaviour returns that
    dimension's group: its units ordered and marked, the group block, and an outcome that names
    the group. The commit before the groups recorded the rank order, the bare reply keys, the
    "Found N matches." summary and the unmarked hits that this test now replaces.

    Catches a group request that loses a member, changes a member's own fields, or orders and
    marks it without the dimension's behaviours.
    """
    vault = _display_vault(tmp_path)
    replies = {
        dimension: _search(vault, {"unit.dimension": {"$eq": dimension}})
        for dimension in ("currency", "uncertainty", "provenance", "order")
    }
    decisions = context_refs.vault_ref(f"{NOTES}/Decision log.md")

    # lead_with_current puts the current decision first and names its successor on the other.
    assert _view(replies["currency"]) == [
        ("Decision log", "Ship in March."),
        ("Decision log", "Ship in January."),
    ]
    assert [hit.get("display") for hit in replies["currency"]["hits"]] == [
        None, {"replaced": {"by": f"{decisions}#after"}},
    ]
    # flag_open marks the question that nothing answers; the hypothesis has a verdict.
    assert _view(replies["uncertainty"]) == [
        ("Questions", "Does the launch need a beta?"),
        ("Questions", "Who owns support after launch?"),
        ("Questions", "Users prefer a March launch."),
        ("Answers", "The launch needs a beta for two weeks."),
    ]
    assert [hit.get("display") for hit in replies["uncertainty"]["hits"]] == [
        None, {"open": True}, None, None,
    ]
    assert [hit.get("verdict") for hit in replies["uncertainty"]["hits"]] == [
        None, None, "confirmed", None,
    ]
    # show_origin names who asserted the analysis and the page it derives from.
    assert _view(replies["provenance"]) == [
        ("Survey", "Nine of ten users asked for exports."),
        ("Analysis", "Exports come first on the roadmap."),
    ]
    assert replies["provenance"]["hits"][1]["display"] == {
        "origin": {"by": "Research team", "from": [context_refs.vault_ref(f"{NOTES}/Survey.md")]}
    }
    # order_in_time sorts the dated events, then the precedes edge puts the review before the
    # launch; the follows edge already holds.
    assert _view(replies["order"]) == [
        ("Timeline", "The project kicks off."),
        ("Timeline", "The security review passes."),
        ("Timeline", "The launch happens."),
        ("Timeline", "The team holds a retrospective."),
    ]
    assert [reply["user_summary"] for reply in replies.values()] == [
        "2 units in 'Currency'; 1 was replaced.",
        "4 units in 'Uncertainty'; 1 is open.",
        "2 units in 'Provenance'.",
        "4 units in 'Order'.",
    ]
    for dimension, reply in replies.items():
        assert sorted(reply) == ["dimension_groups", "hits", "user_summary"], dimension
        [group] = reply["dimension_groups"]
        assert group["dimension"] == dimension
        assert [unit["unit_ref"] for unit in group["units"]] == [
            hit["unit_ref"] for hit in reply["hits"]
        ]
        for hit in reply["hits"]:
            assert hit["dimension"] == dimension
            own = sorted(set(hit) - {"verdict", "display"})
            assert own == UNIT_FIELDS, (dimension, hit["excerpt"])


# ---------------------------------------------------------------------------
# Groups at the request boundary: the search tool and its deep pack
# ---------------------------------------------------------------------------

ORDER_PAGES = {
    # By date alone the announcement comes first and the undated freeze last.
    "Launch plan": (
        "## Timeline event\n- id: ship\n- occurred_at: 2026-03-01\n\nThe product ships.\n\n"
        "## Timeline event\n- id: freeze\n- relations: precedes: [[#ship]]\n\n"
        "The code freezes.\n\n"
        "## Timeline event\n- id: announce\n- occurred_at: 2026-02-01\n"
        "- relations: follows: [[#ship]]\n\nThe launch is announced."
    ),
}


def test_a_precedes_and_a_follows_edge_order_a_group_in_search_and_pack(tmp_path: Path) -> None:
    """The spec's "Stated order survives compilation" and "A follows edge orders the same way".
    Catches a group that orders by date alone, so the freeze that precedes the release comes last
    and the announcement that follows it comes first."""
    vault = _vault(tmp_path, ORDER_PAGES)

    reply = _search(vault, {"unit.dimension": {"$eq": "order"}}, deep=True)

    order = [_ref("Launch plan", anchor) for anchor in ("freeze", "ship", "announce")]
    assert [hit["unit_ref"] for hit in reply["hits"]] == order
    assert reply["pack"]["groups"] == [{"dimension": "order", "units": order}]
    assert "order_conflict" not in reply["dimension_groups"][0]


def test_leading_with_the_current_view_hides_nothing_in_search_or_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec's "Leading with the current view hides nothing". Catches a replaced decision that
    a group drops, that the pack loses from its group when its cap leaves no room for it, or whose
    own fields a mark rewrites."""
    vault = _display_vault(tmp_path)
    current, earlier = _ref("Decision log", "after"), _ref("Decision log", "before")

    reply = _search(vault, {"unit.dimension": {"$eq": "currency"}}, deep=True)

    assert [hit["unit_ref"] for hit in reply["hits"]] == [current, earlier]
    # Each unit keeps the standing an ungrouped search of the same units gives it.
    ungrouped = _search(vault, {"$or": [{"unit.dimension": {"$eq": "currency"}}]}, deep=True)
    group_fields = {"dimension", "display"}
    assert {
        hit["unit_ref"]: {key: value for key, value in hit.items() if key not in group_fields}
        for hit in reply["hits"]
    } == {hit["unit_ref"]: hit for hit in ungrouped["hits"]}
    assert _packed(reply)[earlier]["display"] == {"replaced": {"by": current}}
    assert "display" not in _packed(reply)[current]
    assert reply["pack"]["groups"] == [{"dimension": "currency", "units": [current, earlier]}]

    # With room for one unit, the earlier decision stays in its group as a ref.
    monkeypatch.setenv("EXOMEM_PACK_MAX_UNITS", "1")
    short = _search(vault, {"unit.dimension": {"$eq": "currency"}}, deep=True)
    assert list(_packed(short)) == [current]
    assert short["pack"]["groups"] == [{"dimension": "currency", "units": [current, earlier]}]


REGULATION = (
    "schema_version: 1\ndimensions:\n"
    "  regulation:\n    labels: {en: Regulation}\n"
    "    description: What the rules require\n    members: {kinds: [constraint, requirement]}\n"
    "    display: [show_origin]\n"
)


def test_an_added_dimension_borrows_a_display_behaviour(tmp_path: Path) -> None:
    """The spec's "An added dimension borrows a display behaviour". Catches a display that reads
    only shipped dimensions, so a vault dimension that names `show_origin` shows no origin."""
    vault = _vault(
        tmp_path,
        {
            "Release rules": (
                "## Constraint\n- asserted_by: Security team\n"
                f"- relations: derived_from: [[{NOTES}/Audit]]\n\nNo deploys on Friday.\n\n"
                "## Requirement\n\nTwo reviewers approve each change."
            ),
            "Audit": "## Finding\n\nFriday deploys caused two outages.",
        },
        registry=REGULATION,
    )

    reply = _search(vault, {"unit.dimension": {"$eq": "Regulation"}}, deep=True)

    origin = {"by": "Security team", "from": [context_refs.vault_ref(f"{NOTES}/Audit.md")]}
    assert _marks(reply) == {
        "No deploys on Friday.": {"origin": origin},
        "Two reviewers approve each change.": None,
    }
    assert reply["user_summary"] == "2 units in 'Regulation'."
    assert [unit.get("display") for unit in _packed(reply).values()] == [{"origin": origin}, None]


def test_an_open_question_is_open_only_until_another_page_answers_it(tmp_path: Path) -> None:
    """Catches `open` that ignores an inbound `answers` edge from another page, or that marks a
    question open whatever its edges say."""
    answered = _display_vault(tmp_path / "answered")
    unanswered = _vault(
        tmp_path / "unanswered", {key: body for key, body in PAGES.items() if key != "Answers"}
    )
    beta = "Does the launch need a beta?"

    for vault, is_open in ((answered, False), (unanswered, True)):
        reply = _search(vault, {"unit.dimension": {"$eq": "uncertainty"}}, deep=True)
        assert (_marks(reply)[beta] == {"open": True}) is is_open
        assert (_packed(reply)[_ref("Questions", "beta")].get("display") == {"open": True}) is is_open
        assert reply["dimension_groups"][0]["open"] == (1 if not is_open else 2)


WITHHELD_ANSWER = "Patterns/Answers"


def _answer_twin(tmp_path: Path, *, withheld: bool) -> Path:
    """The display vault with its answering page in a folder that an egress rule withholds from
    an external caller, or as if it never had that page."""
    pages = {key: body for key, body in PAGES.items() if key != "Answers"}
    if withheld:
        pages[WITHHELD_ANSWER] = PAGES["Answers"]
    vault = _vault(tmp_path, pages, name="with-page" if withheld else "without-page", graph=False)
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    _reset_caches()
    return vault


@pytest.mark.parametrize("served", [False, True], ids=["scan", "served"])
def test_a_caller_who_may_not_see_the_answer_gets_the_reply_of_a_vault_without_it(
    tmp_path: Path, request: pytest.FixtureRequest, served: bool
) -> None:
    """Catches a mark read from an edge the caller may not see: the question would lose `open`
    and tell the caller that a withheld page answers it."""
    replies = []
    for withheld in (True, False):
        vault = _answer_twin(tmp_path, withheld=withheld)
        if served:
            request.getfixturevalue("warm_managed_cell")(vault)
        with principal.request_scope(_external()):
            replies.append(_search(vault, {"unit.dimension": {"$eq": "uncertainty"}}, deep=True))
    with_page, without_page = replies

    assert with_page == without_page
    assert _marks(with_page)["Does the launch need a beta?"] == {"open": True}


def test_a_unit_in_two_requested_groups_is_in_full_in_the_first_and_a_ref_in_the_next(
    tmp_path: Path,
) -> None:
    """Catches a unit repeated in full in a second group, or missing from it."""
    vault = _display_vault(tmp_path)
    analysis = next(
        hit["unit_ref"]
        for hit in _search(vault, {"unit.dimension": {"$eq": "attribution"}})["hits"]
    )

    reply = _search(vault, {"unit.dimension": {"$in": ["provenance", "attribution"]}}, deep=True)

    assert [hit["unit_ref"] for hit in reply["hits"]].count(analysis) == 1
    assert next(h for h in reply["hits"] if h["unit_ref"] == analysis)["dimension"] == "provenance"
    provenance, attribution = reply["dimension_groups"]
    assert {"unit_ref": analysis} in provenance["units"]
    assert attribution["units"] == [{"unit_ref": analysis, "shown_in": "provenance"}]
    assert reply["user_summary"] == "2 units in 'Provenance'. 1 unit in 'Attribution'."
    assert [group["units"] for group in reply["pack"]["groups"]][1] == [analysis]
    assert list(_packed(reply)).count(analysis) == 1


def test_a_cycle_of_precedes_edges_keeps_the_date_order_and_names_the_conflict(
    tmp_path: Path,
) -> None:
    """Catches a cycle that loops, drops its events, or reorders them with no word to the caller."""
    vault = _vault(
        tmp_path,
        {
            "Cycle": (
                "## Timeline event\n- id: a\n- occurred_at: 2026-03-01\n"
                "- relations: precedes: [[#b]]\n\nEvent A.\n\n"
                "## Timeline event\n- id: b\n- occurred_at: 2026-02-01\n"
                "- relations: precedes: [[#c]]\n\nEvent B.\n\n"
                "## Timeline event\n- id: c\n- occurred_at: 2026-01-01\n"
                "- relations: precedes: [[#a]]\n\nEvent C."
            ),
        },
    )

    reply = _search(vault, {"unit.dimension": {"$eq": "order"}}, deep=True)

    by_date = [_ref("Cycle", anchor) for anchor in ("c", "b", "a")]
    assert [hit["unit_ref"] for hit in reply["hits"]] == by_date
    assert sorted(reply["dimension_groups"][0]["order_conflict"]) == sorted(by_date)
    assert reply["user_summary"] == "3 units in 'Order'; 3 have conflicting order links."
    assert reply["pack"]["groups"] == [{"dimension": "order", "units": by_date}]


QUESTIONS_ONLY = (
    "schema_version: 1\ndimensions:\n"
    "  questions:\n    labels: {en: Questions}\n"
    "    description: What is still asked\n    members: {kinds: [open_question]}\n"
    "    display: [flag_open]\n"
)


def test_an_unreadable_graph_marks_nothing_open_and_says_the_links_are_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a reader that takes a graph it has not built as one with no answers, so every
    question reads as open. Membership by kind needs no graph, so the search still answers."""
    monkeypatch.setattr(epistemic_graph, "schedule_background_rebuild", lambda *a, **k: None)
    vault = _vault(tmp_path, PAGES, registry=QUESTIONS_ONLY, graph=False)

    reply = _search(vault, {"unit.dimension": {"$eq": "questions"}}, deep=True)

    assert len(reply["hits"]) == 2
    assert not any(_marks(reply).values())
    [group] = reply["dimension_groups"]
    assert group["edges"] == "unknown"
    assert "open" not in group
    assert reply["user_summary"] == (
        "2 units in 'Questions'; its links could not be read yet, so its marks may be incomplete."
    )


ARGUMENT_PAGES = {
    "Plan": "## Claim\n- id: plan\n\nWe ship the export first.",
    "Review": f"## Claim\n- relations: supports: [[{NOTES}/Plan#plan]]\n\nUsers asked for exports.",
    "Test": (
        f"## Finding\n- relations: contradicts: [[{NOTES}/Plan#plan]]\n\n"
        "The export breaks on large vaults."
    ),
    "Judged": "## Hypothesis\n- verdict: refuted\n\nExports are rarely used.",
}


@pytest.mark.parametrize("served", [False, True], ids=["scan", "served"])
def test_a_dimension_with_no_display_behaviour_groups_the_units_its_filter_keeps(
    tmp_path: Path, request: pytest.FixtureRequest, served: bool
) -> None:
    """Catches a group that places its members by a second reading of membership: a dimension
    whose members come from relation families and that names no display behaviour reports an
    empty group beside the hits its filter kept."""
    vault = _vault(tmp_path, ARGUMENT_PAGES)
    if served:
        request.getfixturevalue("warm_managed_cell")(vault)

    reply = _search(vault, {"unit.dimension": {"$eq": "argument"}}, deep=True)

    refs = [hit["unit_ref"] for hit in reply["hits"]]
    assert sorted(hit["excerpt"] for hit in reply["hits"]) == [
        "The export breaks on large vaults.", "Users asked for exports.", "We ship the export first.",
    ]
    [group] = reply["dimension_groups"]
    assert (group["count"], [unit["unit_ref"] for unit in group["units"]]) == (3, refs)
    assert reply["user_summary"] == "3 units in 'Argument'."
    assert reply["pack"]["groups"] == [{"dimension": "argument", "units": refs}]


SERVED_GROUPS = {
    "currency": {"unit.dimension": {"$eq": "currency"}},
    "uncertainty": {"unit.dimension": {"$eq": "uncertainty"}},
    "order": {"unit.dimension": {"$eq": "order"}},
    "two groups": {"unit.dimension": {"$in": ["provenance", "attribution"]}},
    "questions": {"unit.dimension": {"$eq": "questions"}},
    "argument": {"unit.dimension": {"$eq": "argument"}},
}


def _groups(reply: dict) -> tuple:
    return (
        [(hit["unit_ref"], hit["dimension"], hit.get("display")) for hit in reply["hits"]],
        reply["dimension_groups"],
    )


def test_a_served_search_returns_the_scans_groups_with_one_graph_read_and_no_scan(
    tmp_path: Path, warm_managed_cell, walk_sentinel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a served reader that reads the corpus to group its hits, reads the graph again for
    the display, or groups the same hits differently from the scan."""
    from exomem.vault import kb_dirname

    vault = _vault(tmp_path, {**PAGES, **ARGUMENT_PAGES}, registry=QUESTIONS_ONLY)
    offline = {name: _groups(_search(vault, filters)) for name, filters in SERVED_GROUPS.items()}
    find_module.clear_cache()
    warm_managed_cell(vault)

    def no_scan(*_args, **_kwargs):
        raise AssertionError("the served reader answered a filter by reading every page")

    monkeypatch.setattr(find_module, "_eligible_filter_paths", no_scan)
    sentinel = walk_sentinel(vault, vault / kb_dirname())
    reads: list[object] = []
    real = epistemic_graph.EpistemicGraphIndex.unit_edges

    def counted(self, *args, **kwargs):
        reads.append(kwargs.get("pages"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "unit_edges", counted)

    for name, filters in SERVED_GROUPS.items():
        sentinel.reset()
        reads.clear()
        assert _groups(_search(vault, filters)) == offline[name], name
        assert sentinel.count == 0, f"{name}: {sentinel.report()}"
        assert len(reads) == 1, name


def test_a_search_that_asks_for_no_group_returns_no_group_fields(tmp_path: Path) -> None:
    """Catches group fields, marks or a group order leaking into a search that names no
    dimension, or that excludes one with `$ne`: those replies must stay as they were."""
    vault = _display_vault(tmp_path)

    for filters in (
        None,
        {"unit.kind": {"$eq": "decision"}},
        {"unit.dimension": {"$ne": "currency"}},
        {"$or": [{"unit.dimension": {"$eq": "currency"}}, {"unit.kind": {"$eq": "claim"}}]},
    ):
        reply = _search(vault, filters, deep=True)
        assert sorted(reply) == ["hits", "pack", "user_summary"], filters
        assert reply["user_summary"] == f"Found {len(reply['hits'])} matches.", filters
        assert "groups" not in reply["pack"], filters
        for hit in reply["hits"]:
            assert "dimension" not in hit and "display" not in hit, filters
        assert all("display" not in unit for unit in _packed(reply).values()), filters


def _report_rows(vault: Path) -> dict[str, dict]:
    report = memory_schema.infer_category_registry(vault)
    return {row["key"]: row for row in report["dimensions"]}


def test_the_category_report_counts_each_dimensions_marks_or_says_it_cannot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a report row whose `open` or `replaced` count disagrees with what the group marks,
    or that shows 0 open questions when it could not read the answers."""
    monkeypatch.setattr(epistemic_graph, "schedule_background_rebuild", lambda *a, **k: None)
    rows = _report_rows(_vault(tmp_path, PAGES, registry=QUESTIONS_ONLY))

    assert (rows["uncertainty"]["unit_count"], rows["uncertainty"]["open"]) == (4, 1)
    assert (rows["currency"]["unit_count"], rows["currency"]["replaced"]) == (2, 1)
    assert (rows["questions"]["open"], rows["questions"]["edges"]) == (1, "counted")
    assert "open" not in rows["provenance"] and "replaced" not in rows["provenance"]

    unread = _report_rows(
        _vault(tmp_path, PAGES, name="no-graph", registry=QUESTIONS_ONLY, graph=False)
    )
    # Kind decides who is a question, so the row still counts them, but not which are open.
    assert unread["questions"]["unit_count"] == 2
    assert (unread["questions"]["open"], unread["questions"]["edges"]) == (None, "unavailable")
    assert unread["questions"]["repair"]["tool"] == "maintain_memory"
    assert (unread["currency"]["unit_count"], unread["currency"]["replaced"]) == (None, None)


# ---------------------------------------------------------------------------
# The display function, on its inputs: units, a group's behaviours and the request's edges
# ---------------------------------------------------------------------------

Q = "Knowledge Base/Notes/Questions.md"
A = "Knowledge Base/Notes/Answers.md"
F = "Knowledge Base/Notes/Forecast.md"
D = "Knowledge Base/Notes/Decision log.md"
UnitEdge = epistemic_graph.UnitEdge


def _unit(path: str, node: str | None, kind: str | None, **facts) -> dimension_display.DisplayUnit:
    return dimension_display.DisplayUnit(
        ref=f"{context_refs.vault_ref(path)}#{node}", path=path, node=node, kind=kind, **facts
    )


def _show(units, behaviours, edges, status: str = "available", **kwargs):
    return dimension_display.display(
        units,
        behaviours,
        language=language_registry.core_registry(),
        relations=relation_registry.core_registry(),
        edges=epistemic_dimensions.DimensionEdges(status, tuple(edges)),
        **kwargs,
    )


QUESTIONS = (
    _unit(Q, "answered", "open_question"),
    _unit(Q, "unanswered", "open_question"),
    _unit(Q, "judged", "hypothesis", verdict="confirmed"),
    _unit(Q, "claimed", "claim"),
    _unit(F, "forecast", "prediction"),
)
# An anchored answer to one question, and an answer to a whole page through a vault extension of
# `answers` that this registry does not hold, so it answers through its parent.
ANSWERS = (
    UnitEdge(A, "finding", Q, "answered", "answers"),
    UnitEdge(A, None, F, None, "x.replies", parent="answers"),
)


def test_flag_open_marks_only_a_closable_unit_that_nothing_settles() -> None:
    """Catches `open` read from a kind list instead of the `closable` trait, on a unit with a
    verdict, or past an answer that ends at the unit's page or comes through an extension."""
    view = _show(QUESTIONS, ["flag_open"], ANSWERS)

    assert [QUESTIONS[i].node for i, mark in enumerate(view.marks) if mark.get("open")] == [
        "unanswered"
    ]
    assert view.counts == {"open": 1}
    assert view.order == (0, 1, 2, 3, 4)


def test_unknown_edges_mark_nothing_open_and_say_so() -> None:
    """Catches a reader that takes a graph it could not read as one with no answers, so every
    question reads as open."""
    view = _show(QUESTIONS, ["flag_open"], (), status="warming")

    assert not any(view.marks)
    assert view.edges_known is False
    assert "open" not in view.counts


def test_lead_with_current_puts_replaced_units_last_and_names_each_successor() -> None:
    """The spec's "Leading with the current view hides nothing". Catches a replaced unit dropped
    from its group, or marked without the successor that its edge names."""
    units = (
        _unit(D, "before", "decision"),
        _unit(D, "after", "decision"),
        _unit("Knowledge Base/Notes/Archive.md", "old", "decision", historical=True),
        _unit(D, "hiring", "decision"),
        _unit("Knowledge Base/Notes/Old plan.md", "plan", "decision"),
    )
    edges = (
        UnitEdge(D, "after", D, "before", "supersedes"),
        UnitEdge("Knowledge Base/Notes/New plan.md", None, "Knowledge Base/Notes/Old plan.md", None,
                 "supersedes"),
    )

    view = _show(units, ["lead_with_current"], edges)

    assert [units[i].node for i in view.order] == ["after", "hiring", "before", "old", "plan"]
    assert view.marks[0] == {"replaced": {"by": units[1].ref}}
    assert view.marks[2] == {"replaced": True}
    assert view.marks[4] == {
        "replaced": {"by": context_refs.vault_ref("Knowledge Base/Notes/New plan.md")}
    }
    assert view.counts == {"replaced": 3}


def test_show_origin_names_the_holder_and_at_most_three_sources() -> None:
    """Catches an origin that misses its page's own source edges, names a result unit by its page,
    or lists every source of a unit cited from dozens of pages."""
    analysis = "Knowledge Base/Notes/Analysis.md"
    claim = _unit(analysis, "claim", "claim", holder="Research team")
    survey = _unit("Knowledge Base/Notes/Survey.md", "row", "claim")
    edges = (
        UnitEdge(analysis, "claim", survey.path, "row", "derived_from"),
        UnitEdge(analysis, None, "Knowledge Base/Notes/Interviews.md", None, "evidenced_by"),
        *(
            UnitEdge(analysis, "claim", f"Knowledge Base/Sources/Paper {n}.md", None, "cites")
            for n in range(3)
        ),
    )

    view = _show([claim], ["show_origin"], edges, named={(survey.path, survey.node): survey.ref})

    origin = view.marks[0]["origin"]
    assert origin["by"] == "Research team"
    assert origin["more"] == 2
    assert len(origin["from"]) == 3
    every = sorted([survey.ref, context_refs.vault_ref("Knowledge Base/Notes/Interviews.md"), *(
        context_refs.vault_ref(f"Knowledge Base/Sources/Paper {n}.md") for n in range(3)
    )])
    assert origin["from"] == every[:3]


T = "Knowledge Base/Notes/Timeline.md"


def test_order_in_time_sorts_by_date_then_lets_each_order_edge_decide() -> None:
    """The spec's "Stated order survives compilation" and "A follows edge orders the same way", on
    the function's inputs. Catches undated events sorted to the front, an offset time compared as
    text, or an edge that moves an event past others that no edge orders."""
    units = (
        _unit(T, "launch", "timeline_event", occurred_at="2026-03-01"),
        _unit(T, "kickoff", "timeline_event", occurred_at="2026-01-10T23:30-05:00"),
        _unit(T, "review", "timeline_event"),
        _unit(T, "retro", "timeline_event"),
        _unit(T, "planning", "timeline_event", occurred_at="2026-01-11T02:00+00:00"),
    )
    edges = (
        UnitEdge(T, "review", T, "launch", "precedes"),
        UnitEdge(T, "retro", T, "launch", "follows"),
    )

    view = _show(units, ["order_in_time"], edges)

    # Kickoff is 04:30 UTC on 11 January, after planning at 02:00.
    assert [units[i].node for i in view.order] == ["planning", "kickoff", "review", "launch", "retro"]
    assert view.order_conflict == ()
    # Without edges the group still orders by date, and says the edge state is unknown.
    unknown = _show(units, ["order_in_time"], (), status="warming")
    assert [units[i].node for i in unknown.order] == [
        "planning", "kickoff", "launch", "review", "retro",
    ]
    assert unknown.edges_known is False


def test_order_in_time_keeps_the_date_order_of_a_cycle_and_names_it() -> None:
    """Catches a cycle of `precedes` edges that loops, drops its units, or reorders them silently."""
    units = (
        _unit(T, "a", "timeline_event", occurred_at="2026-03-01"),
        _unit(T, "b", "timeline_event", occurred_at="2026-02-01"),
        _unit(T, "c", "timeline_event", occurred_at="2026-01-01"),
        _unit(T, "d", "timeline_event", occurred_at="2026-04-01"),
    )
    edges = (
        UnitEdge(T, "a", T, "b", "precedes"),
        UnitEdge(T, "b", T, "c", "precedes"),
        UnitEdge(T, "c", T, "a", "precedes"),
        UnitEdge(T, "d", T, "c", "precedes"),
    )

    view = _show(units, ["order_in_time"], edges)

    assert [units[i].node for i in view.order] == ["d", "c", "b", "a"]
    assert sorted(units[i].node for i in view.order_conflict) == ["a", "b", "c"]


def _dated(node: str, month: str | None = None) -> dimension_display.DisplayUnit:
    return _unit(T, node, "timeline_event", occurred_at=f"2026-{month}-01" if month else None)


def test_order_in_time_moves_only_the_units_an_edge_orders() -> None:
    """Catches an edge that pushes dated events, which no edge orders, ahead of the events it does
    order: an undated step before January's event must not let February to May jump ahead."""
    units = (_dated("jan", "01"), _dated("feb", "02"), _dated("mar", "03"), _dated("may", "05"),
             _dated("prep"))

    view = _show(units, ["order_in_time"], (UnitEdge(T, "prep", T, "jan", "precedes"),))

    assert [units[i].node for i in view.order] == ["prep", "jan", "feb", "mar", "may"]
    # A chain into an event that is not the latest dated one lands just before that event.
    chain = (_dated("jan", "01"), _dated("feb", "02"), _dated("mar", "03"), _dated("draft"),
             _dated("review"))
    edges = (
        UnitEdge(T, "draft", T, "review", "precedes"),
        UnitEdge(T, "review", T, "feb", "precedes"),
    )

    view = _show(chain, ["order_in_time"], edges)

    assert [chain[i].node for i in view.order] == ["jan", "draft", "review", "feb", "mar"]
    assert view.order_conflict == ()


def test_order_in_time_leaves_a_unit_between_two_in_a_cycle_where_its_date_puts_it() -> None:
    """Catches a cycle that is kept together as one block, so the event dated between its two
    members is pushed after both of them."""
    units = (_dated("a", "01"), _dated("b", "02"), _dated("c", "03"))
    edges = (UnitEdge(T, "a", T, "c", "precedes"), UnitEdge(T, "c", T, "a", "precedes"))

    view = _show(units, ["order_in_time"], edges)

    assert [units[i].node for i in view.order] == ["a", "b", "c"]
    assert sorted(units[i].node for i in view.order_conflict) == ["a", "c"]
