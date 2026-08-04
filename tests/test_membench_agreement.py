"""Judge–human agreement: blindness of the sheet, balance, determinism, kappa."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from membench.agreement import (
    SampleItem,
    _outcome,
    build_sample,
    cohen_kappa,
    render_sheet,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN = REPO_ROOT / "benchmarks/runs/20260801T115138Z-exomem-local-postfix-lexical-v2-30586b"
CORPUS = REPO_ROOT / "benchmarks/corpus/generated/s1"

pytestmark = pytest.mark.skipif(
    not (RUN / "answers.jsonl").exists() or not (CORPUS / "queries.jsonl").exists(),
    reason="v0.1 seed-1 run or its corpus is not present in this checkout",
)


@pytest.fixture(scope="module")
def items() -> list[SampleItem]:
    return build_sample(RUN, CORPUS, size=50)


# -- the property that makes the measurement worth anything -----------------


def test_sheet_never_reveals_a_verdict(items: list[SampleItem]) -> None:
    """A labeller who can see any verdict is not independent.

    The whole point of measuring judge–human agreement is that the human
    labels without anchoring. If the sheet leaked the judge's ``semantic_match``
    or the deterministic gate outcome, a high kappa would prove nothing except
    that the labeller could read.
    """

    scores = json.loads((RUN / "deterministic-scores.json").read_text(encoding="utf-8"))
    by_query = {r["query_id"]: r for r in scores["per_query"]}

    for item in items:
        rendered = "\n".join(
            [item.item_id, item.question, item.expected, item.candidate]
        )
        for gate in by_query[item.query_id].get("gates", []):
            evidence = gate.get("evidence")
            assert not (evidence and evidence in rendered), (
                f"{item.item_id} leaks gate evidence {evidence!r} to the labeller"
            )
        # Structural verdict fields must never appear in a labelling row. The
        # instruction header may *name* them (it tells the labeller they are
        # withheld), so the check is scoped to item bodies.
        for field in ("semantic_match", "explanation_quality", "not_applicable"):
            assert field not in rendered, f"{item.item_id} leaks {field!r}"


def test_sample_is_balanced_against_a_skewed_population(items: list[SampleItem]) -> None:
    """Balance is the reason kappa is trustworthy here.

    The run's own outcomes are roughly 2:1 toward failure. Sampling the
    population directly would let a rater who always answered "no" score well.
    """

    scores = json.loads((RUN / "deterministic-scores.json").read_text(encoding="utf-8"))
    by_query = {r["query_id"]: _outcome(r.get("gates", [])) for r in scores["per_query"]}
    sampled = [by_query[i.query_id] for i in items]
    passes, fails = sampled.count("pass"), sampled.count("fail")
    assert abs(passes - fails) <= 2, f"sheet is unbalanced: {passes} pass vs {fails} fail"

    population = list(by_query.values())
    assert abs(population.count("pass") - population.count("fail")) > 20, (
        "population is no longer skewed, so this test no longer proves balancing works"
    )


def test_rendered_sheet_carries_every_item_and_a_blank_to_fill(
    items: list[SampleItem],
) -> None:
    """The sheet is the deliverable; a dropped item silently shrinks the sample."""

    sheet = render_sheet(items)
    for item in items:
        assert f"## {item.item_id}" in sheet, f"{item.item_id} missing from the sheet"
        assert item.question in sheet
    assert sheet.count("**Your label:**") == len(items), (
        "every item needs exactly one blank, or labels cannot be matched back"
    )
    # Query ids stay out of the prose a labeller reads; they live in the
    # separate keys file so labels can be joined without anchoring the labeller.
    for item in items:
        assert item.query_id not in sheet, f"{item.item_id} leaks its query id"


def test_sample_is_deterministic(items: list[SampleItem]) -> None:
    again = build_sample(RUN, CORPUS, size=50)
    assert [(i.item_id, i.query_id) for i in items] == [
        (i.item_id, i.query_id) for i in again
    ]


def test_every_item_carries_question_expected_and_candidate(
    items: list[SampleItem],
) -> None:
    assert len(items) == 50
    assert len({i.query_id for i in items}) == 50, "duplicate query in the sheet"
    for item in items:
        assert item.question.strip()
        assert item.expected.strip()
        assert item.candidate.strip()


# -- kappa -----------------------------------------------------------------


def test_kappa_is_zero_for_chance_agreement() -> None:
    """Two raters who agree exactly as often as chance predicts score 0.

    This is why kappa is reported instead of raw agreement: these pairs are
    50% raw agreement, which sounds like a coin flip and is one.
    """

    pairs = [(True, True), (True, False), (False, True), (False, False)]
    assert cohen_kappa(pairs) == pytest.approx(0.0)


def test_kappa_is_one_for_perfect_agreement() -> None:
    assert cohen_kappa([(True, True), (False, False), (True, True)]) == pytest.approx(1.0)


def test_kappa_is_negative_for_systematic_disagreement() -> None:
    assert cohen_kappa([(True, False), (False, True), (True, False), (False, True)]) < 0


def test_kappa_punishes_a_lazy_majority_rater() -> None:
    """Raw agreement 0.9, kappa ~0 — the case raw agreement would hide.

    A rater that always says "no" against a judge that says "no" 90% of the
    time looks excellent on raw agreement and is worthless.
    """

    pairs = [(False, False)] * 9 + [(False, True)]
    raw = sum(1 for a, b in pairs if a == b) / len(pairs)
    assert raw == pytest.approx(0.9)
    assert cohen_kappa(pairs) == pytest.approx(0.0)


def test_kappa_refuses_an_empty_sample() -> None:
    with pytest.raises(ValueError, match="no labelled pairs"):
        cohen_kappa([])
