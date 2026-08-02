"""Source-reliability family (t22): corrections, citations, and hedging."""

from __future__ import annotations

from pathlib import Path

import pytest
from membench import families
from membench.generate import generate_corpus
from membench.schema import (
    ClaimRecord,
    CorpusManifest,
    ExpectedAnswer,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    load_jsonl,
)
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.gates import GateStatus, ScoringContext, evaluate
from membench.templates import registry
from membench.templates.base import Template

T22 = "t22_source_reliability"
FAMILY = "source_reliability"

Corpus = tuple[
    Path,
    CorpusManifest,
    list[QueryRecord],
    dict[str, ExpectedRecord],
    dict[str, ClaimRecord],
    dict[str, SourceRecord],
]


def _active_probe() -> Template:
    def build(ctx):  # type: ignore[no-untyped-def]
        bulletin = ctx.entity("organization", "business")
        source = ctx.source(
            1,
            f"{bulletin.canonical_name} bulletin",
            lines=["The bulletin records a stable value."],
        )
        claim = ctx.claim(bulletin, "stable_value", "stable value", source)
        ctx.query(
            "clean_metric",
            "What value is recorded?",
            knowledge_week=2,
            family=FAMILY,
            expect=lambda octx, query: ExpectedRecord(
                query_id=query.query_id,
                answer=ExpectedAnswer(kind="text", values=[claim.object.value]),
            ),
        )

    return Template(
        template_id="t98_reliability_probe",
        family=FAMILY,
        summary="source reliability family activation probe",
        variants=1,
        build=build,
    )


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Corpus:
    root = tmp_path_factory.mktemp("source-reliability") / "corpus"
    manifest = generate_corpus(1, root, template_ids=[T22])
    queries = load_jsonl(QueryRecord, root / "queries.jsonl")
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, root / "expected.jsonl")}
    claims = {c.claim_id: c for c in load_jsonl(ClaimRecord, root / "claims.jsonl")}
    sources = {s.source_id: s for s in load_jsonl(SourceRecord, root / "sources.jsonl")}
    return root, manifest, queries, expected, claims, sources


def _gate(items, name):  # type: ignore[no-untyped-def]
    matches = [item for item in items if item.gate == name]
    assert len(matches) == 1
    return matches[0]


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_source_reliability_family_is_active_and_accepts_generation(
    tmp_path: Path,
) -> None:
    probe = _active_probe()
    manifest = generate_corpus(1, tmp_path / "probe", templates={probe.template_id: probe})
    entry = families.registry()[FAMILY]
    assert entry.classification == "deterministic-oracle"
    assert entry.status == "active"
    assert manifest.counts["queries"] == 1


def test_t22_registers_four_variants_with_four_queries(corpus: Corpus) -> None:
    _, manifest, queries, expected, _, _ = corpus
    template = registry()[T22]
    assert template.family == FAMILY
    assert template.variants == 4
    assert manifest.counts["queries"] == 16
    assert manifest.counts["expected"] == 16
    assert {query.family for query in queries} == {FAMILY}
    assert set(expected) == {query.query_id for query in queries}
    assert not any(query.canary for query in queries)
    assert {query.query_kind for query in queries} == {
        "current_corrected_metric",
        "correction_history",
        "clean_metric",
        "fresh_unconfirmed",
    }


def test_twice_corrected_metric_has_claim_and_source_supersession_chain(
    corpus: Corpus,
) -> None:
    root, _, queries, expected, claims, sources = corpus
    picks = [query for query in queries if query.query_kind == "current_corrected_metric"]
    assert len(picks) == 4
    for query in picks:
        record = expected[query.query_id]
        [latest_id] = record.required_claims
        assert len(record.forbidden_claims) == 2
        old_claims = [claims[claim_id] for claim_id in record.forbidden_claims]
        latest = claims[latest_id]
        first = next(claim for claim in old_claims if claim.supersedes is None)
        second = next(claim for claim in old_claims if claim.supersedes == first.claim_id)
        assert first.superseded_by == second.claim_id
        assert second.superseded_by == latest.claim_id
        assert latest.supersedes == second.claim_id

        chain = [first, second, latest]
        chain_sources = [sources[claim.assertions[0].source_id] for claim in chain]
        assert [source.version for source in chain_sources] == [1, 2, 3]
        assert chain_sources[1].supersedes_source == chain_sources[0].source_id
        assert chain_sources[2].supersedes_source == chain_sources[1].source_id
        assert len({source.title for source in chain_sources}) == 1
        assert chain_sources[2].source_id in record.required_citations

        for claim, source in zip(chain, chain_sources, strict=True):
            artifact = (root / source.path).read_text(encoding="utf-8")
            assert claim.object.value in artifact


def test_current_metric_forbids_both_corrected_values(corpus: Corpus) -> None:
    _, _, queries, expected, claims, sources = corpus
    ctx = ScoringContext(claims_by_id=claims, sources_by_id=sources)
    picks = [query for query in queries if query.query_kind == "current_corrected_metric"]
    for query in picks:
        record = expected[query.query_id]
        [latest_id] = record.required_claims
        latest = claims[latest_id]
        assert len(record.forbidden_claims) == 2
        right = AnswerRecord(
            query_id=query.query_id,
            answer_text=latest.object.value,
            citations=list(record.required_citations),
        )
        assert _gate(evaluate(query, record, right, ctx), "current_state").status is GateStatus.PASS
        for forbidden_id in record.forbidden_claims:
            wrong = AnswerRecord(
                query_id=query.query_id,
                answer_text=claims[forbidden_id].object.value,
                citations=list(record.required_citations),
            )
            assert (
                _gate(evaluate(query, record, wrong, ctx), "current_state").status
                is GateStatus.FAIL
            )


def test_correction_history_is_oracle_bound_to_all_three_values_and_sources(
    corpus: Corpus,
) -> None:
    _, _, queries, expected, claims, _ = corpus
    picks = [query for query in queries if query.query_kind == "correction_history"]
    assert len(picks) == 4
    for query in picks:
        record = expected[query.query_id]
        assert record.answer.kind == "list"
        assert len(record.answer.values) == 3
        assert len(record.required_claims) == 3
        assert record.answer.values == [
            claims[claim_id].object.value for claim_id in record.required_claims
        ]
        assert len(set(record.required_citations)) == 3
        assert len({claims[claim_id].predicate for claim_id in record.required_claims}) == 1


def test_fresh_error_prone_value_requires_hedging_and_correction_citations(
    corpus: Corpus,
) -> None:
    _, _, queries, expected, claims, sources = corpus
    ctx = ScoringContext(claims_by_id=claims, sources_by_id=sources)
    picks = [query for query in queries if query.query_kind == "fresh_unconfirmed"]
    assert len(picks) == 4
    for query in picks:
        record = expected[query.query_id]
        [claim_id] = record.required_claims
        value = claims[claim_id].object.value
        assert record.uncertainty.hedged is True
        assert len(set(record.required_citations)) == 4

        flat = AnswerRecord(
            query_id=query.query_id,
            answer_text=f"The fresh value is {value}.",
            citations=list(record.required_citations),
        )
        flat_items = evaluate(query, record, flat, ctx)
        assert _gate(flat_items, "current_state").status is GateStatus.PASS
        assert _gate(flat_items, "calibration").status is GateStatus.FAIL

        hedged = AnswerRecord(
            query_id=query.query_id,
            answer_text=f"The unconfirmed bulletin value is reportedly {value}.",
            citations=list(record.required_citations),
        )
        hedged_items = evaluate(query, record, hedged, ctx)
        assert _gate(hedged_items, "calibration").status is GateStatus.PASS
        assert _gate(hedged_items, "citations").status is GateStatus.PASS

        missing_history = hedged.model_copy(
            update={"citations": list(record.required_citations)[-1:]}
        )
        assert (
            _gate(evaluate(query, record, missing_history, ctx), "citations").status
            is GateStatus.FAIL
        )


def test_clean_source_metric_has_plain_expectation(corpus: Corpus) -> None:
    _, _, queries, expected, _, _ = corpus
    picks = [query for query in queries if query.query_kind == "clean_metric"]
    assert len(picks) == 4
    for query in picks:
        record = expected[query.query_id]
        assert record.uncertainty.hedged is None
        assert len(record.required_claims) == 1
        assert len(record.required_citations) == 1
        assert record.forbidden_claims == []


def test_t22_double_generation_is_byte_identical(tmp_path: Path) -> None:
    first = generate_corpus(7, tmp_path / "a", template_ids=[T22])
    second = generate_corpus(7, tmp_path / "b", template_ids=[T22])
    assert first == second
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")
