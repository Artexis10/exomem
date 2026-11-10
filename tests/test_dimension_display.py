"""Dimension groups: a search that filters units by an epistemic dimension returns that
dimension's view, ordered and marked by the dimension's display behaviours.

Each test names the wrong answer it catches. The fixture vault holds one decision that supersedes
another, two open questions of which another page answers one, a hypothesis with a verdict, a
claim with its source and its asserter, and four timeline events ordered by dates and by
`precedes` and `follows` edges.
"""

from __future__ import annotations

from pathlib import Path

from exomem import (
    commands,
    context_refs,
    dimension_display,
    epistemic_dimensions,
    epistemic_graph,
    relation_registry,
)
from exomem import find as find_module
from exomem import semantic_language_registry as language_registry

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


def _display_vault(tmp_path: Path) -> Path:
    from conftest import initialize_vault_state_offline

    vault = tmp_path / "vault"
    vault.mkdir()
    initialize_vault_state_offline(vault, source="dimension display fixture")
    for title, body in PAGES.items():
        _write(vault, f"{NOTES}/{title}.md", PAGE.format(title=title, body=body))
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    find_module.clear_cache()
    return vault


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


# The fields every unit hit of these searches carries today, before any display mark.
UNIT_FIELDS = [
    "category", "excerpt", "kind", "parent_path", "parent_status", "parent_title", "parent_type",
    "parent_updated", "result_type", "source_anchor", "unit_ref",
]


def test_a_search_by_dimension_before_groups_returns_rank_order_and_no_marks(
    tmp_path: Path,
) -> None:
    """The baseline: what a search filtered by a dimension returns before dimension groups. Each
    line marked `Flips` is one this slice changes; the rest must hold after it.

    Catches a group request that loses a member or changes a member's own fields.
    """
    vault = _display_vault(tmp_path)
    replies = {
        dimension: _search(vault, {"unit.dimension": {"$eq": dimension}})
        for dimension in ("currency", "uncertainty", "provenance", "order")
    }

    # Flips: lead_with_current puts the current decision first.
    assert _view(replies["currency"]) == [
        ("Decision log", "Ship in January."),
        ("Decision log", "Ship in March."),
    ]
    assert _view(replies["uncertainty"]) == [
        ("Questions", "Does the launch need a beta?"),
        ("Questions", "Who owns support after launch?"),
        ("Questions", "Users prefer a March launch."),
        ("Answers", "The launch needs a beta for two weeks."),
    ]
    assert _view(replies["provenance"]) == [
        ("Survey", "Nine of ten users asked for exports."),
        ("Analysis", "Exports come first on the roadmap."),
    ]
    # Flips: order_in_time orders the events by date and by their precedes and follows edges.
    assert _view(replies["order"]) == [
        ("Timeline", "The launch happens."),
        ("Timeline", "The project kicks off."),
        ("Timeline", "The security review passes."),
        ("Timeline", "The team holds a retrospective."),
    ]
    for dimension, reply in replies.items():
        # Flips: the reply names its group in `dimension_groups`.
        assert sorted(reply) == ["hits", "user_summary"], dimension
        # Flips: the summary names the group by its label and gives its counts.
        assert reply["user_summary"] == f"Found {len(reply['hits'])} matches.", dimension
        for hit in reply["hits"]:
            own = sorted(set(hit) - {"verdict"})
            # Flips: each hit names its group in `dimension` and carries its marks in `display`.
            assert own == UNIT_FIELDS, (dimension, hit["excerpt"])
    assert [hit.get("verdict") for hit in replies["uncertainty"]["hits"]] == [
        None, None, "confirmed", None,
    ]


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
