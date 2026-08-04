"""Source-reliability family (t22): corrections, citations, and hedging."""

from __future__ import annotations

from pathlib import Path

import pytest
from membench import families
from membench.generate import generate_corpus
from membench.schema import (
    ClaimRecord,
    ClaimStatus,
    CorpusManifest,
    EntityRecord,
    ExpectedAnswer,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    TypedValue,
    load_jsonl,
)
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.gates import GateStatus, ScoringContext, evaluate
from membench.templates import registry
from membench.templates.base import GenerationError, Template
from membench.templates.builders_ext import (
    expect_correction_history,
    expect_value_with_correction_history,
)

T22 = "t22_source_reliability"
FAMILY = "source_reliability"

Corpus = tuple[
    Path,
    CorpusManifest,
    list[QueryRecord],
    dict[str, ExpectedRecord],
    dict[str, ClaimRecord],
    dict[str, SourceRecord],
    dict[str, EntityRecord],
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
    entities_by_id = {
        e.entity_id: e for e in load_jsonl(EntityRecord, root / "entities.jsonl")
    }
    return root, manifest, queries, expected, claims, sources, entities_by_id


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


def _scored_gates(query: QueryRecord, record: ExpectedRecord) -> set[str]:
    """The deterministic gates this record actually makes discriminating.

    ``expected.jsonl`` is published, so its ``gates`` field is a claim about the
    record and has to be true. A gate belongs in the list when the record's own
    fields give it something to decide: state gates need claims (and take the
    scorer's ``as_of``/``current_state`` naming from the ask), citations need
    required citations, ``no_leak`` needs forbidden disclosures, ``abstention``
    needs a required abstention/clarification, ``calibration`` needs a hedging
    requirement. ``value`` is subsumed by the state gate wherever required
    claims exist, which is the convention the rest of the suite follows.
    """

    gates: set[str] = set()
    if record.required_claims or record.forbidden_claims:
        gates.add("as_of" if query.ask.world_week is not None else "current_state")
    if record.required_citations:
        gates.add("citations")
    if record.forbidden_disclosures:
        gates.add("no_leak")
    if record.abstain or record.clarify:
        gates.add("abstention")
    if record.uncertainty.hedged is not None:
        gates.add("calibration")
    return gates


def _perfect_answer(
    record: ExpectedRecord, claims: dict[str, ClaimRecord], *, hedged: bool
) -> AnswerRecord:
    """An answer correct on every axis except its hedging policy."""

    text = " ".join(claims[claim_id].object.value for claim_id in record.required_claims)
    return AnswerRecord(
        query_id=record.query_id,
        answer_text=text or "nothing is recorded",
        citations=list(record.required_citations),
        hedged=hedged,
    )


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
    _, manifest, queries, expected, _, _, _ = corpus
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
    root, _, queries, expected, claims, sources, _ = corpus
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
    _, _, queries, expected, claims, sources, entities_by_id = corpus
    ctx = ScoringContext(
        claims_by_id=claims, sources_by_id=sources, entities_by_id=entities_by_id
    )
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
    _, _, queries, expected, claims, _, _ = corpus
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
    _, _, queries, expected, claims, sources, entities_by_id = corpus
    ctx = ScoringContext(
        claims_by_id=claims, sources_by_id=sources, entities_by_id=entities_by_id
    )
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


def test_clean_source_metric_must_be_stated_plainly(corpus: Corpus) -> None:
    _, _, queries, expected, claims, sources, entities_by_id = corpus
    ctx = ScoringContext(
        claims_by_id=claims, sources_by_id=sources, entities_by_id=entities_by_id
    )
    picks = [query for query in queries if query.query_kind == "clean_metric"]
    assert len(picks) == 4
    for query in picks:
        record = expected[query.query_id]
        assert record.uncertainty.hedged is False
        assert "calibration" in record.gates
        assert len(record.required_claims) == 1
        assert len(record.required_citations) == 1
        assert record.forbidden_claims == []

        [claim_id] = record.required_claims
        value = claims[claim_id].object.value
        # The register is the system of record and has never been corrected,
        # so the plain answer is the right one and hedging is the error.
        flat = AnswerRecord(
            query_id=query.query_id,
            answer_text=f"The register records {value} points.",
            citations=list(record.required_citations),
        )
        flat_items = evaluate(query, record, flat, ctx)
        assert _gate(flat_items, "current_state").status is GateStatus.PASS
        assert _gate(flat_items, "citations").status is GateStatus.PASS
        assert _gate(flat_items, "calibration").status is GateStatus.PASS

        hedged = AnswerRecord(
            query_id=query.query_id,
            answer_text=f"The register reportedly records around {value} points.",
            citations=list(record.required_citations),
        )
        assert (
            _gate(evaluate(query, record, hedged, ctx), "calibration").status
            is GateStatus.FAIL
        )


def test_no_constant_hedging_policy_passes_every_calibration_gate(
    corpus: Corpus,
) -> None:
    """The calibration gate only measures something if both extremes lose.

    A provider that hedges every answer and a provider that hedges none must
    each fail somewhere in this family; otherwise a constant hedging policy
    scores a clean sheet and the gate is decorative.
    """

    _, _, queries, expected, claims, sources, entities_by_id = corpus
    ctx = ScoringContext(
        claims_by_id=claims, sources_by_id=sources, entities_by_id=entities_by_id
    )
    penalised: dict[bool, set[str]] = {}
    for policy in (True, False):
        penalised[policy] = {
            query.query_kind
            for query in queries
            if _gate(
                evaluate(
                    query,
                    expected[query.query_id],
                    _perfect_answer(expected[query.query_id], claims, hedged=policy),
                    ctx,
                ),
                "calibration",
            ).status
            is GateStatus.FAIL
        }
    assert "clean_metric" in penalised[True]
    assert "fresh_unconfirmed" in penalised[False]


def test_source_supersession_edges_retire_the_superseded_edition(
    corpus: Corpus,
) -> None:
    """``supersedes_source`` means "this edition replaces that one".

    So every claim asserted by a superseded edition must itself be superseded,
    and the edition backing a still-current answer must not be marked replaced.
    Without this the corpus would require citing a source it had also told the
    system to retire.
    """

    _, _, queries, expected, claims, sources, _ = corpus
    asserted_by: dict[str, list[ClaimRecord]] = {}
    for claim in claims.values():
        for assertion in claim.assertions:
            asserted_by.setdefault(assertion.source_id, []).append(claim)

    superseded_sources = {
        source.supersedes_source
        for source in sources.values()
        if source.supersedes_source is not None
    }
    assert superseded_sources, "t22 must exercise source-level supersession"
    for source_id in superseded_sources:
        for claim in asserted_by.get(source_id, []):
            assert claim.superseded_by is not None, (
                f"{source_id} is superseded but its claim {claim.claim_id} is not"
            )

    for query in queries:
        if query.query_kind != "current_corrected_metric":
            continue
        record = expected[query.query_id]
        for source_id in record.required_citations:
            assert source_id not in superseded_sources, (
                f"{query.query_id} requires citing superseded source {source_id}"
            )


def test_declared_gates_match_what_each_record_evaluates(corpus: Corpus) -> None:
    _, _, queries, expected, _, _, _ = corpus
    for query in queries:
        record = expected[query.query_id]
        assert set(record.gates) == _scored_gates(query, record), (
            f"{query.query_kind} declares {record.gates}"
        )


def _chain_probe(
    *, world_week: int | None, fresh_supersedes: bool
) -> Template:
    """A twice-corrected bulletin plus one later issue, knobs exposed."""

    def build(ctx):  # type: ignore[no-untyped-def]
        bulletin = ctx.entity("organization", "business")
        project = ctx.entity("project", "operations")
        title = f"{bulletin.canonical_name} weekly bulletin"
        editions = []
        claims = []
        for index, (week, value) in enumerate(((1, "10"), (4, "20"), (7, "30"))):
            previous = editions[-1] if editions else None
            source = ctx.source(
                week,
                title,
                supersedes_source=None if previous is None else previous.source_id,
                version=index + 1,
                lines=[f"The bulletin reports {value} points."],
            )
            editions.append(source)
            claims.append(
                ctx.claim(
                    project,
                    "bulletin_points",
                    TypedValue(kind="quantity", value=value, unit="points"),
                    source,
                )
            )
        ctx.supersede(claims[0], claims[1], week=4)
        ctx.supersede(claims[1], claims[2], week=7)

        fresh_source = ctx.source(
            9,
            title,
            supersedes_source=editions[-1].source_id if fresh_supersedes else None,
            version=4 if fresh_supersedes else 1,
            lines=["A fresh unconfirmed reading is 77 points."],
        )
        fresh_claim = ctx.claim(
            project,
            "fresh_points",
            TypedValue(kind="quantity", value="77", unit="points"),
            fresh_source,
            status=ClaimStatus.TENTATIVE,
        )
        ctx.query(
            "correction_history",
            "Which values has the bulletin published?",
            knowledge_week=10,
            world_week=world_week,
            family=FAMILY,
            expect=expect_correction_history(claims[0]),
        )
        ctx.query(
            "fresh_unconfirmed",
            "What fresh value does the bulletin give?",
            knowledge_week=10,
            family=FAMILY,
            expect=expect_value_with_correction_history(fresh_claim, claims[0]),
        )

    return Template(
        template_id="t97_chain_probe",
        family=FAMILY,
        summary="correction-chain probe",
        variants=1,
        build=build,
    )


def test_correction_chain_refuses_an_as_of_ask_it_cannot_answer(
    tmp_path: Path,
) -> None:
    """A chain asked about week 5 must not answer with the week-10 chain.

    The second correction lands in week 7, so at world week 5 its value is not
    yet part of the published history. The chain builder reads the as-of view
    and refuses rather than listing the latest truth under an as-of question.
    """

    probe = _chain_probe(world_week=5, fresh_supersedes=False)
    with pytest.raises(GenerationError, match="oracle-visible value at the asked time"):
        generate_corpus(1, tmp_path / "as-of", templates={probe.template_id: probe})

    current = _chain_probe(world_week=None, fresh_supersedes=False)
    manifest = generate_corpus(
        1, tmp_path / "current", templates={current.template_id: current}
    )
    assert manifest.counts["expected"] == 2


def test_fresh_issue_may_not_declare_a_supersession_edge(tmp_path: Path) -> None:
    """The 4b.1 topology, enforced at generation time.

    A source that carries a live claim of its own is not a replacement edition,
    so it must not declare ``supersedes_source`` over an edition whose claim is
    still current and still a required citation.
    """

    probe = _chain_probe(world_week=None, fresh_supersedes=True)
    with pytest.raises(GenerationError, match="supersedes_source"):
        generate_corpus(1, tmp_path / "fresh", templates={probe.template_id: probe})


def test_t22_double_generation_is_byte_identical(tmp_path: Path) -> None:
    first = generate_corpus(7, tmp_path / "a", template_ids=[T22])
    second = generate_corpus(7, tmp_path / "b", template_ids=[T22])
    assert first == second
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")
