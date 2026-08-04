"""Procedural family (t17): activation, binding spec scenario, determinism.

Red-first evidence for OpenSpec change ``expand-memory-proof-benchmark``
task 3.1: before the ``procedural`` registry entry flips active, every
generation below refuses with the planned-family error; after the flip the
oracle-derived expectations satisfy the spec scenario "Step-order question
scored deterministically".
"""

from __future__ import annotations

from pathlib import Path

from membench import families
from membench.generate import generate_corpus
from membench.schema import (
    ClaimRecord,
    CorpusManifest,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    load_jsonl,
)
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.gates import GateStatus, ScoringContext, gate_state, gate_value

T17 = "t17_procedural_chains"


def _generate(target: Path, seed: int = 1) -> CorpusManifest:
    return generate_corpus(seed, target, template_ids=[T17])


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# -- (a) family activation -------------------------------------------------


def test_t17_generates_under_active_procedural_family(tmp_path: Path) -> None:
    manifest = _generate(tmp_path / "corpus")
    entry = families.registry()["procedural"]
    assert entry.status == "active"
    assert entry.classification == "deterministic-oracle"
    info = {t.template_id: t for t in manifest.templates}[T17]
    assert info.family == "procedural"
    assert info.variants == 4
    assert manifest.counts["queries"] == 20  # 5 queries x 4 variants
    assert manifest.counts["expected"] == manifest.counts["queries"]


def test_t17_emits_the_required_query_shapes(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _generate(root)
    queries = load_jsonl(QueryRecord, root / "queries.jsonl")
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, root / "expected.jsonl")}
    assert all(q.family == "procedural" for q in queries)
    assert not any(q.canary for q in queries)  # no new canaries

    order_records = [
        expected[q.query_id]
        for q in queries
        if expected[q.query_id].answer.kind == "list"
    ]
    assert len(order_records) == 4  # current-order recall, one per variant
    for record in order_records:
        assert len(record.required_claims) == 4
        assert len(record.forbidden_claims) == 1
        assert len(record.required_citations) >= 2  # manual + revision memo

    as_of = [q for q in queries if q.ask.world_week is not None]
    assert len(as_of) == 4  # as-of pre-revision order question, one per variant
    assert all(q.ask.world_week == 3 for q in as_of)
    assert all("as_of" in expected[q.query_id].gates for q in as_of)

    abstain = [q for q in queries if expected[q.query_id].abstain]
    assert len(abstain) == 4  # never-existed step, one per variant
    assert all("abstention" in expected[q.query_id].gates for q in abstain)


# -- (b) binding spec scenario: step-order scored deterministically --------


def test_step_order_question_scored_deterministically(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    _generate(root)
    queries = load_jsonl(QueryRecord, root / "queries.jsonl")
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, root / "expected.jsonl")}
    claims = {c.claim_id: c for c in load_jsonl(ClaimRecord, root / "claims.jsonl")}
    sources = {s.source_id: s for s in load_jsonl(SourceRecord, root / "sources.jsonl")}

    # The post-revision predecessor query is the only current-truth ask with
    # exactly one required and one forbidden claim.
    picks = [
        (q, expected[q.query_id])
        for q in queries
        if q.ask.world_week is None
        and len(expected[q.query_id].required_claims) == 1
        and len(expected[q.query_id].forbidden_claims) == 1
    ]
    assert len(picks) == 4  # one per variant
    for query, record in picks:
        new_claim = claims[record.required_claims[0]]
        old_claim = claims[record.forbidden_claims[0]]

        # Oracle-derived: the pre-revision predecessor was superseded by the
        # post-revision predecessor, which is the expected answer.
        assert old_claim.superseded_by == new_claim.claim_id
        assert record.answer.values == [new_claim.object.value]
        assert new_claim.object.value != old_claim.object.value

        # The revising source's citation is required, and it is a genuinely
        # later source than the original manual.
        revising_source_id = new_claim.assertions[0].source_id
        original_source_id = old_claim.assertions[0].source_id
        assert revising_source_id in record.required_citations
        assert (
            sources[revising_source_id].recorded_week
            > sources[original_source_id].recorded_week
        )
        assert "current_state" in record.gates

        # Scored deterministically: the pre-revision predecessor fails the
        # current-state gate, the post-revision predecessor passes it.
        ctx = ScoringContext(claims_by_id=claims, sources_by_id=sources)
        wrong = AnswerRecord(
            query_id=query.query_id,
            answer_text=f"The {old_claim.object.value} must come directly before it.",
            citations=[revising_source_id],
        )
        right = AnswerRecord(
            query_id=query.query_id,
            answer_text=f"The {new_claim.object.value} must come directly before it.",
            citations=[revising_source_id],
        )
        wrong_item = gate_state(query, record, wrong, ctx)
        right_item = gate_state(query, record, right, ctx)
        assert wrong_item.gate == "current_state"
        assert wrong_item.status is GateStatus.FAIL
        assert right_item.gate == "current_state"
        assert right_item.status is GateStatus.PASS

        # Stale-echo variant: the provider gives the current value but also
        # echoes the superseded one alongside it. The correct value being
        # present is not enough -- the forbidden (retired) value must also be
        # absent, or the answer is misleading about what changed. A bare
        # value check would let this slip through (it contains the right
        # answer); it is the current-state gate's forbidden-claims check
        # that must catch it.
        both = AnswerRecord(
            query_id=query.query_id,
            answer_text=(
                f"It used to be the {old_claim.object.value}, but it is now "
                f"the {new_claim.object.value}."
            ),
            citations=[revising_source_id],
        )
        both_value_item = gate_value(query, record, both, ctx)
        both_state_item = gate_state(query, record, both, ctx)
        assert both_value_item.status is GateStatus.PASS  # the right value is present
        assert both_state_item.gate == "current_state"
        assert both_state_item.status is GateStatus.FAIL  # but so is the retired one
        assert old_claim.object.value in (both_state_item.evidence or "")


# -- (c) determinism -------------------------------------------------------


def test_t17_double_generation_is_identical(tmp_path: Path) -> None:
    first = _generate(tmp_path / "a")
    second = _generate(tmp_path / "b")
    assert first == second
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")
