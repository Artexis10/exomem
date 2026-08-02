"""Preference-attribution family (t21): holder, time, and calibration gates."""

from __future__ import annotations

from pathlib import Path

import pytest
from membench import families
from membench.generate import generate_corpus
from membench.schema import (
    ClaimRecord,
    CorpusManifest,
    EntityRecord,
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

T21 = "t21_preference_attribution"
FAMILY = "preference_attribution"

Corpus = tuple[
    CorpusManifest,
    list[EntityRecord],
    list[QueryRecord],
    dict[str, ExpectedRecord],
    dict[str, ClaimRecord],
    dict[str, SourceRecord],
]


def _active_probe() -> Template:
    def build(ctx):  # type: ignore[no-untyped-def]
        holder = ctx.entity("person", "business")
        source = ctx.source(
            1,
            f"{holder.canonical_name} preference note",
            lines=[f"{holder.canonical_name} prefers the recorded option."],
        )
        claim = ctx.claim(
            holder,
            "position_on_recorded_option",
            f"{holder.canonical_name} prefers the recorded option",
            source,
        )
        ctx.query(
            "holder_opinion",
            f"What does {holder.canonical_name} prefer?",
            knowledge_week=2,
            family=FAMILY,
            expect=lambda octx, query: ExpectedRecord(
                query_id=query.query_id,
                answer=ExpectedAnswer(kind="text", values=[claim.object.value]),
            ),
        )

    return Template(
        template_id="t98_preference_probe",
        family=FAMILY,
        summary="preference family activation probe",
        variants=1,
        build=build,
    )


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Corpus:
    root = tmp_path_factory.mktemp("preference") / "corpus"
    manifest = generate_corpus(1, root, template_ids=[T21])
    entities = load_jsonl(EntityRecord, root / "entities.jsonl")
    queries = load_jsonl(QueryRecord, root / "queries.jsonl")
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, root / "expected.jsonl")}
    claims = {c.claim_id: c for c in load_jsonl(ClaimRecord, root / "claims.jsonl")}
    sources = {s.source_id: s for s in load_jsonl(SourceRecord, root / "sources.jsonl")}
    return manifest, entities, queries, expected, claims, sources


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


def test_preference_family_is_active_and_accepts_generation(tmp_path: Path) -> None:
    probe = _active_probe()
    manifest = generate_corpus(1, tmp_path / "probe", templates={probe.template_id: probe})
    entry = families.registry()[FAMILY]
    assert entry.classification == "deterministic-oracle"
    assert entry.status == "active"
    assert manifest.counts["queries"] == 1


def test_t21_registers_four_variants_with_four_queries(corpus: Corpus) -> None:
    manifest, _, queries, expected, _, _ = corpus
    template = registry()[T21]
    assert template.family == FAMILY
    assert template.variants == 4
    assert manifest.counts["queries"] == 16
    assert manifest.counts["expected"] == 16
    assert {query.family for query in queries} == {FAMILY}
    assert set(expected) == {query.query_id for query in queries}
    assert not any(query.canary for query in queries)
    assert {query.query_kind for query in queries} == {
        "holder_opinion",
        "opinion_as_of",
        "opinion_objectivity",
        "unrecorded_opinion",
    }


def test_opinion_claims_encode_holder_and_do_not_blur_objective_fact(
    corpus: Corpus,
) -> None:
    _, entities, queries, expected, claims, sources = corpus
    entities_by_id = {entity.entity_id: entity for entity in entities}
    for query in queries:
        record = expected[query.query_id]
        if query.query_kind not in {
            "holder_opinion",
            "opinion_as_of",
            "opinion_objectivity",
        }:
            continue
        [opinion_id] = record.required_claims
        opinion = claims[opinion_id]
        holder = entities_by_id[opinion.subject]
        assert holder.kind == "person"
        assert opinion.predicate.startswith("position_on_")
        assert holder.canonical_name in opinion.object.value
        assert opinion.assertions[0].source_id in record.required_citations

        opinion_source = sources[opinion.assertions[0].source_id]
        assert opinion_source.authority.value == "firsthand"

    objective_claims = [claim for claim in claims.values() if claim.predicate == "observed_level"]
    assert len(objective_claims) == 4
    for objective in objective_claims:
        related_opinions = [
            claim
            for claim in claims.values()
            if claim.subject != objective.subject and claim.predicate.startswith("position_on_")
        ]
        assert related_opinions
        for opinion in related_opinions:
            assert opinion.object.value not in objective.object.value
            assert objective.object.value not in opinion.object.value
        assert sources[objective.assertions[0].source_id].authority.value == "official"


def test_only_opinion_support_requires_hedging_and_attribution(
    corpus: Corpus,
) -> None:
    _, entities, queries, expected, claims, sources = corpus
    entities_by_id = {entity.entity_id: entity for entity in entities}
    ctx = ScoringContext(claims_by_id=claims, sources_by_id=sources)
    picks = [query for query in queries if query.query_kind == "opinion_objectivity"]
    assert len(picks) == 4
    for query in picks:
        record = expected[query.query_id]
        [claim_id] = record.required_claims
        value = claims[claim_id].object.value
        holder = entities_by_id[claims[claim_id].subject].canonical_name
        assert holder in value
        assert record.uncertainty.hedged is True
        assert record.required_citations

        flat = AnswerRecord(
            query_id=query.query_id,
            answer_text=f"It is objectively true that {value}.",
            citations=list(record.required_citations),
        )
        flat_items = evaluate(query, record, flat, ctx)
        assert _gate(flat_items, "current_state").status is GateStatus.PASS
        assert _gate(flat_items, "citations").status is GateStatus.PASS
        assert _gate(flat_items, "calibration").status is GateStatus.FAIL

        unattributed = AnswerRecord(
            query_id=query.query_id,
            answer_text="The proposed level is objectively better.",
            citations=list(record.required_citations),
            hedged=True,
        )
        assert (
            _gate(evaluate(query, record, unattributed, ctx), "current_state").status
            is GateStatus.FAIL
        )

        attributed = AnswerRecord(
            query_id=query.query_id,
            answer_text=f"Reportedly, this is only {holder}'s view: {value}.",
            citations=list(record.required_citations),
        )
        attributed_items = evaluate(query, record, attributed, ctx)
        assert _gate(attributed_items, "current_state").status is GateStatus.PASS
        assert _gate(attributed_items, "calibration").status is GateStatus.PASS
        assert _gate(attributed_items, "citations").status is GateStatus.PASS


def test_changed_position_as_of_forbids_the_old_view(corpus: Corpus) -> None:
    _, _, queries, expected, claims, sources = corpus
    ctx = ScoringContext(claims_by_id=claims, sources_by_id=sources)
    picks = [query for query in queries if query.query_kind == "opinion_as_of"]
    assert len(picks) == 4
    for query in picks:
        record = expected[query.query_id]
        assert query.ask.world_week is not None
        [new_id] = record.required_claims
        [old_id] = record.forbidden_claims
        old = claims[old_id]
        new = claims[new_id]
        assert old.superseded_by == new.claim_id
        assert old.object.value != new.object.value

        wrong = AnswerRecord(
            query_id=query.query_id,
            answer_text=old.object.value,
            citations=list(record.required_citations),
        )
        right = AnswerRecord(
            query_id=query.query_id,
            answer_text=new.object.value,
            citations=list(record.required_citations),
        )
        assert _gate(evaluate(query, record, wrong, ctx), "as_of").status is GateStatus.FAIL
        assert _gate(evaluate(query, record, right, ctx), "as_of").status is GateStatus.PASS


def test_unrecorded_opinion_requires_abstention(corpus: Corpus) -> None:
    _, _, queries, expected, _, _ = corpus
    picks = [query for query in queries if query.query_kind == "unrecorded_opinion"]
    assert len(picks) == 4
    for query in picks:
        record = expected[query.query_id]
        assert record.abstain is True
        assert record.answer.kind == "none"
        assert record.required_claims == []


def test_t21_double_generation_is_byte_identical(tmp_path: Path) -> None:
    first = generate_corpus(7, tmp_path / "a", template_ids=[T21])
    second = generate_corpus(7, tmp_path / "b", template_ids=[T21])
    assert first == second
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")
