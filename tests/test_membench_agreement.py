"""Judge–human agreement: blindness of the sheet, balance, determinism, kappa."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from membench.agreement import (
    SampleItem,
    _outcome,
    agreement_report,
    build_sample,
    cohen_kappa,
    parse_labels,
    render_answer_form,
    render_sheet,
)
from membench.judge.backends import make_judge_item
from membench.judge.blinding import leakage_scan

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


def test_labeller_grades_exactly_what_the_judge_grades(items: list[SampleItem]) -> None:
    """Both raters must see the same input, or kappa measures the sheet.

    Two asymmetries were live in the first revision and are pinned here. The
    sheet truncated candidates at 1200 characters while the judge receives the
    whole answer (31 of 140 non-empty v0.1 answers exceeded it), and the sheet
    showed raw text carrying sentinels, vault paths and product names that the
    judge never sees (33 of the first 60 answers tripped ``leakage_scan``).
    Either one turns a disagreement into an artefact of this module.
    """

    raw = {
        row["query_id"]: row
        for row in (
            json.loads(line)
            for line in (RUN / "answers.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }

    for item in items:
        text = (raw[item.query_id].get("answer_text") or "").strip()
        if raw[item.query_id].get("abstained"):
            text = f"(the system declined to answer) {text}".strip()
        judge_item = make_judge_item(
            item.query_id,
            question=item.question,
            expected_summary=item.expected,
            candidate_answer=text,
            provider_token="system-A",
        )
        if text:
            assert item.candidate == judge_item.payload["candidate_answer"], (
                f"{item.item_id}: labeller and judge see different candidate text"
            )
        assert not leakage_scan(item.candidate), (
            f"{item.item_id} shows provider-identifying text the judge never sees: "
            f"{leakage_scan(item.candidate)}"
        )
        assert not leakage_scan(item.question), f"{item.item_id} question leaks identity"


def test_candidate_text_is_never_truncated(items: list[SampleItem]) -> None:
    """Truncation would silently shorten one rater's evidence, not the other's."""

    assert not any("(truncated)" in i.candidate for i in items)
    longest = max(len(i.candidate) for i in items)
    assert longest > 1200, (
        "no sampled answer exceeds the old 1200-char cut, so this test no longer "
        "demonstrates that truncation was removed"
    )


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
    # The header carries a format demo and two pre-filled worked examples, so
    # count only within the item section — everything from the first item on.
    body = sheet[sheet.index(f"## {items[0].item_id}") :]
    assert body.count("**Your label:** <yes | no | unsure>") == len(items), (
        "every item needs exactly one blank, or labels cannot be matched back"
    )
    assert body.count("**Your label:**") == len(items), "an item label is pre-filled"
    # Query ids stay out of the prose a labeller reads; they live in the
    # separate keys file so labels can be joined without anchoring the labeller.
    for item in items:
        assert item.query_id not in sheet, f"{item.item_id} leaks its query id"


def test_judged_stratum_has_content_and_control_stratum_does_not(
    items: list[SampleItem],
) -> None:
    """Rows with nothing to read inflate kappa and waste the labeller.

    An empty or abstained response is decided by inspection by both raters, so
    it contributes near-free agreement — the same majority-class inflation kappa
    exists to avoid. Abstention is also already decided by `gate_abstention`, a
    deterministic gate, so the judge is not load-bearing there. A capped control
    stratum is kept as a sanity check and separated so it cannot dilute the
    headline figure.
    """

    judged = [i for i in items if i.stratum == "judged"]
    control = [i for i in items if i.stratum == "control"]
    assert len(judged) == 40 and len(control) == 10

    def _empty(item: SampleItem) -> bool:
        return item.candidate == "(empty response)" or item.candidate.startswith(
            "(the system declined"
        )

    assert not any(_empty(i) for i in judged), "a judged row has nothing to judge"
    assert all(_empty(i) for i in control), "a control row carries real content"


def test_judged_stratum_is_itself_outcome_balanced(items: list[SampleItem]) -> None:
    """Balance has to hold where the headline kappa is computed, not just overall."""

    scores = json.loads((RUN / "deterministic-scores.json").read_text(encoding="utf-8"))
    by_query = {r["query_id"]: _outcome(r.get("gates", [])) for r in scores["per_query"]}
    judged = [by_query[i.query_id] for i in items if i.stratum == "judged"]
    assert abs(judged.count("pass") - judged.count("fail")) <= 2, (
        f"judged stratum unbalanced: {judged.count('pass')} pass vs {judged.count('fail')} fail"
    )


def test_stratum_is_never_rendered_into_the_sheet(items: list[SampleItem]) -> None:
    """Telling a labeller a row is a control invites them to skim it."""

    sheet = render_sheet(items)
    assert "control" not in sheet.lower()
    assert "stratum" not in sheet.lower()


# -- agreement report ------------------------------------------------------


def _items(*specs: tuple[str, str]) -> list[SampleItem]:
    return [
        SampleItem(
            item_id=item_id,
            query_id=f"QRY-{item_id}",
            question="q",
            expected="e",
            candidate="c",
            stratum=stratum,
        )
        for item_id, stratum in specs
    ]


def test_report_separates_judged_from_control() -> None:
    """A perfect control stratum must not rescue a poor judged stratum.

    This is the failure mode the split exists to expose: control rows agree for
    free, so mixing them in drags the combined figure upward and a weak judge
    looks acceptable.
    """

    items = _items(*[(f"J{n:03d}", "judged") for n in range(1, 5)],
                   *[(f"J{n:03d}", "control") for n in range(5, 9)])
    human = {"J001": True, "J002": False, "J003": True, "J004": False,
             "J005": True, "J006": True, "J007": False, "J008": False}
    judge = {"J001": False, "J002": True, "J003": False, "J004": True,
             "J005": True, "J006": True, "J007": False, "J008": False}

    report = agreement_report(items, human, judge)
    assert report["strata"]["judged"]["kappa"] == pytest.approx(-1.0)
    assert report["strata"]["control"]["kappa"] == pytest.approx(1.0)
    assert report["combined"]["kappa"] > report["strata"]["judged"]["kappa"], (
        "the combined figure should visibly differ from the judged one, which is "
        "why the judged one is the headline"
    )


def test_report_excludes_unsure_rather_than_counting_it() -> None:
    items = _items(("J001", "judged"), ("J002", "judged"))
    report = agreement_report(items, {"J001": True, "J002": None}, {"J001": True, "J002": True})
    assert report["unsure"] == 1
    assert report["strata"]["judged"]["n"] == 1


def test_report_records_rows_the_judge_never_scored() -> None:
    items = _items(("J001", "judged"), ("J002", "judged"))
    report = agreement_report(items, {"J001": True, "J002": True}, {"J001": True})
    assert report["unjudged"] == ["J002"]
    assert report["strata"]["judged"]["n"] == 1


def test_report_refuses_a_label_for_an_unknown_item() -> None:
    with pytest.raises(ValueError, match="not in this sample"):
        agreement_report(_items(("J001", "judged")), {"J999": True}, {"J999": True})


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


def test_regenerating_preserves_hand_entered_labels(items: list[SampleItem]) -> None:
    """Hand-entered labels are the one input here that cannot be recomputed.

    The sheet gets improved while a labelling pass is in progress, so rendering
    must be able to carry existing answers forward. Losing them costs a human
    another hour; nothing else in this pipeline has that property.
    """

    entered: dict[str, bool | None] = {
        items[0].item_id: True,
        items[1].item_id: False,
        items[2].item_id: None,
    }
    sheet = render_sheet(items, entered)
    form = render_answer_form(items, entered)

    assert parse_labels(sheet) == entered
    assert parse_labels(form) == entered
    # Unentered items stay blank rather than defaulting to anything.
    remaining = {i.item_id for i in items} - set(entered)
    assert sheet.count("**Your label:** <yes | no | unsure>") == len(remaining) + 1
    assert all(f"{item_id}: <yes | no | unsure>" in form for item_id in remaining)


def test_labels_read_back_from_a_filled_sheet(items: list[SampleItem]) -> None:
    """The sheet must be machine-readable after a human edits it in place."""

    sheet = render_sheet(items)
    filled = []
    for line in sheet.splitlines():
        if line.startswith("**Your label:** <"):
            filled.append("**Your label:** yes")
        else:
            filled.append(line)
    labels = parse_labels("\n".join(filled))
    assert set(labels) == {i.item_id for i in items}
    assert all(v is True for v in labels.values())


def test_labels_read_back_from_the_answer_form(items: list[SampleItem]) -> None:
    form = render_answer_form(items).replace(
        "<yes | no | unsure>", "no"
    )
    labels = parse_labels(form)
    assert set(labels) == {i.item_id for i in items}
    assert all(v is False for v in labels.values())


def test_worked_examples_are_not_parsed_as_answers(items: list[SampleItem]) -> None:
    """The header's two examples carry real labels; they must not be scored.

    They sit above the first `## J###` heading, so no item id is in scope —
    if that ever changed, the sample would gain two phantom rows.
    """

    sheet = render_sheet(items)
    assert "**Your label:** yes" in sheet, "example A should model a filled answer"
    assert "**Your label:** no" in sheet, "example B should model a filled answer"
    assert parse_labels(sheet) == {}, "unfilled sheet must yield no labels"


def test_unfilled_items_are_omitted_not_guessed(items: list[SampleItem]) -> None:
    """A half-finished sheet must not silently become a half-sized sample."""

    form = render_answer_form(items)
    partial = form.replace(f"{items[0].item_id}: <yes | no | unsure>", f"{items[0].item_id}: yes")
    labels = parse_labels(partial)
    assert labels == {items[0].item_id: True}


def test_labels_tolerate_realistic_human_variation() -> None:
    assert parse_labels("J001: YES") == {"J001": True}
    assert parse_labels("J002:  n ") == {"J002": False}
    assert parse_labels("J003: Unsure") == {"J003": None}
    assert parse_labels("**J004**: `yes`") == {"J004": True}


def test_unreadable_label_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="cannot read label"):
        parse_labels("J001: probably?")


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
