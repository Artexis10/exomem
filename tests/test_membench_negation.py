"""Negation & counterfactual family (t19): recorded-false vs not-recorded.

Binding spec scenario "Recorded-false is not unknown" (OpenSpec change
``expand-memory-proof-benchmark``, memory-proof-corpus): when a query asks
about a proposal the corpus records as rejected, the expected answer states
the rejection with its citation — an abstention fails the abstention gate,
and a claim that the proposal is active fails the current-state gate. The
not-recorded sibling, phrased near-identically, must be abstained on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from membench import families
from membench.generate import generate_corpus
from membench.schema import (
    ClaimRecord,
    ClaimStatus,
    CorpusManifest,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    load_jsonl,
)
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.gates import GateStatus, ScoringContext, evaluate
from membench.templates import registry

T19 = "t19_negation_counterfactual"
FAMILY = "negation_counterfactual"

_CONTRAST = re.compile(
    r"^Did (?P<project>.+) adopt the (?P<product>.+) toolchain for reporting\?$"
)

Corpus = tuple[
    CorpusManifest,
    list[QueryRecord],
    dict[str, ExpectedRecord],
    dict[str, ClaimRecord],
    dict[str, SourceRecord],
]


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Corpus:
    root = tmp_path_factory.mktemp("negation") / "corpus"
    manifest = generate_corpus(1, root, template_ids=[T19])
    queries = load_jsonl(QueryRecord, root / "queries.jsonl")
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, root / "expected.jsonl")}
    claims = {c.claim_id: c for c in load_jsonl(ClaimRecord, root / "claims.jsonl")}
    sources = {s.source_id: s for s in load_jsonl(SourceRecord, root / "sources.jsonl")}
    return manifest, queries, expected, claims, sources


def _gate(items, name):  # type: ignore[no-untyped-def]
    matches = [item for item in items if item.gate == name]
    assert len(matches) == 1, f"expected exactly one {name!r} item, got {matches}"
    return matches[0]


def _recorded_false_pairs(
    queries: list[QueryRecord], expected: dict[str, ExpectedRecord]
) -> list[tuple[QueryRecord, ExpectedRecord]]:
    return [
        (query, expected[query.query_id])
        for query in queries
        if expected[query.query_id].forbidden_claims
        and expected[query.query_id].required_claims
        and not expected[query.query_id].abstain
    ]


# -- (a) family activation ---------------------------------------------------


def test_t19_registers_under_the_negation_family() -> None:
    template = registry()[T19]
    assert template.family == FAMILY
    assert template.variants == 4


def test_negation_family_is_active_deterministic_oracle() -> None:
    entry = families.registry()[FAMILY]
    assert entry.classification == "deterministic-oracle"
    assert entry.status == "active", (
        "negation_counterfactual must be flipped active for t19 to generate"
    )


def test_t19_generates_with_five_queries_per_variant(corpus: Corpus) -> None:
    manifest, queries, expected, _, _ = corpus
    assert manifest.counts["queries"] == 4 * 5
    assert manifest.counts["expected"] == manifest.counts["queries"]
    assert {q.family for q in queries} == {FAMILY}
    assert set(expected) == {q.query_id for q in queries}


# -- (b) spec-scenario golden: recorded-false is not unknown -----------------


def test_recorded_false_expected_record_demands_rejection_and_citation(
    corpus: Corpus,
) -> None:
    _, queries, expected, claims, _ = corpus
    pairs = _recorded_false_pairs(queries, expected)
    assert len(pairs) == 2 * 4  # rejected proposal + rejected plan, per variant
    for query, record in pairs:
        assert record.abstain is False
        assert record.answer.kind == "text"
        [rejection_id] = record.required_claims
        assert record.answer.values == [claims[rejection_id].object.value]
        assert record.required_citations, f"{query.query_id}: rejection needs a citation"
        rejecting_sources = {a.source_id for a in claims[rejection_id].assertions}
        assert set(record.required_citations) <= rejecting_sources
        [rejected_id] = record.forbidden_claims
        assert claims[rejected_id].status_timeline[-1].status in (
            ClaimStatus.DISPROVED,
            ClaimStatus.REVOKED,
        )


def test_recorded_false_gates_fail_abstention_and_active_framing(
    corpus: Corpus,
) -> None:
    _, queries, expected, claims, sources = corpus
    ctx = ScoringContext(claims_by_id=claims, sources_by_id=sources)
    for query, record in _recorded_false_pairs(queries, expected):
        rejection_value = claims[record.required_claims[0]].object.value
        active_value = claims[record.forbidden_claims[0]].object.value

        abstaining = AnswerRecord(
            query_id=query.query_id, answer_text="", abstained=True
        )
        assert (
            _gate(evaluate(query, record, abstaining, ctx), "abstention").status
            is GateStatus.FAIL
        ), f"{query.query_id}: abstention must fail on a recorded-false query"

        active = AnswerRecord(
            query_id=query.query_id,
            answer_text=f"Yes, the project is {active_value}.",
        )
        assert (
            _gate(evaluate(query, record, active, ctx), "current_state").status
            is GateStatus.FAIL
        ), f"{query.query_id}: treating the proposal as active must fail"

        hedging_both = AnswerRecord(
            query_id=query.query_id,
            answer_text=(
                f"It {rejection_value}, although it is still {active_value}."
            ),
            citations=list(record.required_citations),
        )
        item = _gate(evaluate(query, record, hedging_both, ctx), "current_state")
        assert item.status is GateStatus.FAIL
        assert item.evidence is not None and "forbidden" in item.evidence

        correct = AnswerRecord(
            query_id=query.query_id,
            answer_text=f"No. Recorded outcome: {rejection_value}.",
            citations=list(record.required_citations),
            abstained=False,
        )
        assert active_value not in correct.answer_text
        items = evaluate(query, record, correct, ctx)
        assert _gate(items, "abstention").status is GateStatus.PASS
        assert _gate(items, "current_state").status is GateStatus.PASS
        assert _gate(items, "citations").status is GateStatus.PASS
        assert not [item for item in items if item.status is GateStatus.FAIL]


# -- (c) contrast golden: not-recorded sibling must abstain ------------------


def test_not_recorded_sibling_requires_abstention(corpus: Corpus) -> None:
    _, queries, expected, claims, sources = corpus
    ctx = ScoringContext(claims_by_id=claims, sources_by_id=sources)
    abstain_pairs = [
        (query, expected[query.query_id])
        for query in queries
        if expected[query.query_id].abstain
    ]
    assert len(abstain_pairs) == 4  # one not-recorded sibling per variant
    for query, record in abstain_pairs:
        assert record.answer.kind == "none"
        answered = AnswerRecord(
            query_id=query.query_id,
            answer_text="Yes, that toolchain was adopted for reporting.",
        )
        assert (
            _gate(evaluate(query, record, answered, ctx), "abstention").status
            is GateStatus.FAIL
        ), f"{query.query_id}: answering a never-recorded sibling must fail"
        abstaining = AnswerRecord(
            query_id=query.query_id, answer_text="", abstained=True
        )
        assert (
            _gate(evaluate(query, record, abstaining, ctx), "abstention").status
            is GateStatus.PASS
        )


def test_contrast_pair_uses_near_identical_phrasing(corpus: Corpus) -> None:
    _, queries, expected, _, _ = corpus
    answered: list[tuple[str, str]] = []
    abstained: list[tuple[str, str]] = []
    for query in queries:
        match = _CONTRAST.match(query.prompt_text)
        if match is None:
            continue
        pair = (match.group("project"), match.group("product"))
        if expected[query.query_id].abstain:
            abstained.append(pair)
        else:
            answered.append(pair)
    assert len(answered) == 4 and len(abstained) == 4
    for project, product in abstained:
        assert any(
            answered_project == project and answered_product != product
            for answered_project, answered_product in answered
        ), f"no answered contrast twin for the not-recorded {product!r} question"


def test_pre_rejection_window_is_asked_as_of(corpus: Corpus) -> None:
    _, queries, expected, _, _ = corpus
    as_of = [q for q in queries if q.ask.world_week is not None]
    assert len(as_of) == 4  # one pre-rejection as-of view per variant
    for query in as_of:
        record = expected[query.query_id]
        assert not record.abstain
        assert record.answer.values, f"{query.query_id}: as-of view needs the proposal"


# -- (d) determinism ---------------------------------------------------------


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_t19_double_generation_is_byte_identical(tmp_path: Path) -> None:
    first = generate_corpus(1, tmp_path / "a", template_ids=[T19])
    second = generate_corpus(1, tmp_path / "b", template_ids=[T19])
    assert first == second
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")
