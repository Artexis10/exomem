"""Cross-lingual fact family (t20): native-script sources, English queries.

Red-first suite for OpenSpec change ``expand-memory-proof-benchmark`` task 3.4
(spec requirement "Cross-Lingual Fact Family", scenario "Cross-script recall").
"""

from __future__ import annotations

import re
from pathlib import Path
from random import Random

import pytest

from exomem.public_artifact_privacy import scan_artifact

from membench import families, wordbank
from membench.generate import generate_corpus
from membench.ids import sentinel
from membench.schema import (
    ClaimRecord,
    EntityRecord,
    ExpectedAnswer,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    load_jsonl,
)
from membench.scoring.answer_contract import AnswerRecord, extract_structure
from membench.scoring.gates import GateStatus, ScoringContext, gate_citations
from membench.templates import registry
from membench.templates.base import BuildContext, Template
from membench.templates.t20_cross_lingual import VARIANTS
from membench.templates.t20_cross_lingual import build as build_t20

T20 = "t20_cross_lingual"
FAMILY = "cross_lingual"
_ASCII_LETTER = re.compile(r"[A-Za-z]")


def _activation_probe() -> Template:
    def build(ctx):  # type: ignore[no-untyped-def]
        entity = ctx.entity("concept", "science")
        source = ctx.source(
            2, f"{entity.canonical_name} note", lines=["A minor recorded fact."]
        )
        ctx.claim(entity, "noted_by", "field team", source)

        def expect(octx, query):  # type: ignore[no-untyped-def]
            return ExpectedRecord(
                query_id=query.query_id, answer=ExpectedAnswer(kind="none")
            )

        ctx.query("direct_recall", "Anything noted?", knowledge_week=4, expect=expect)

    return Template(
        template_id="t98_cross_lingual_probe",
        family=FAMILY,
        summary="cross-lingual activation probe",
        variants=1,
        build=build,
    )


def test_cross_lingual_family_accepts_registered_template(tmp_path: Path) -> None:
    """RED while the registry still declares cross_lingual as planned."""

    probe = _activation_probe()
    manifest = generate_corpus(
        1, tmp_path / "probe", templates={probe.template_id: probe}
    )
    assert manifest.counts["queries"] == 1


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("cross-lingual") / "corpus"
    generate_corpus(1, root, template_ids=[T20])
    return root


def _records(
    root: Path,
) -> tuple[
    list[EntityRecord],
    list[QueryRecord],
    dict[str, ExpectedRecord],
    dict[str, ClaimRecord],
    dict[str, SourceRecord],
]:
    entities = load_jsonl(EntityRecord, root / "entities.jsonl")
    queries = load_jsonl(QueryRecord, root / "queries.jsonl")
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, root / "expected.jsonl")}
    claims = {c.claim_id: c for c in load_jsonl(ClaimRecord, root / "claims.jsonl")}
    sources = {s.source_id: s for s in load_jsonl(SourceRecord, root / "sources.jsonl")}
    return entities, queries, expected, claims, sources


def test_t20_registers_four_variants_with_four_queries_each(corpus: Path) -> None:
    template = registry()[T20]
    assert template.family == FAMILY
    assert template.variants == 4
    _, queries, expected, _, _ = _records(corpus)
    assert len(queries) == 4 * 4
    assert set(expected) == {query.query_id for query in queries}
    assert {query.family for query in queries} == {FAMILY}
    counts: dict[str, int] = {}
    for query in queries:
        counts[query.query_kind] = counts.get(query.query_kind, 0) + 1
    assert counts == {
        "cross_script_direct_recall": 4,
        "cross_script_current_truth": 4,
        "unanswerable": 4,
        "same_script_control": 4,
    }


def test_t20_aliases_do_not_collide_across_variants(tmp_path: Path) -> None:
    root = tmp_path / "seed-4"
    generate_corpus(4, root, template_ids=[T20])
    queries = load_jsonl(QueryRecord, root / "queries.jsonl")
    direct_prompts = {
        query.prompt_text
        for query in queries
        if query.query_kind == "cross_script_direct_recall"
    }
    assert len(direct_prompts) == 4


_SWEEP_SEEDS = 1_000
_VariantRow = tuple[str, str, list[tuple[str, int]]]


@pytest.fixture(scope="module")
def variant_sweep() -> list[tuple[int, list[_VariantRow]]]:
    """Every t20 variant over many master seeds, built in memory (no artifacts).

    ``build`` is driven directly rather than through ``generate_corpus`` so the
    sweep stays cheap enough to enumerate rather than sample.
    """

    sweep: list[tuple[int, list[_VariantRow]]] = []
    for seed in range(_SWEEP_SEEDS):
        rows: list[_VariantRow] = []
        for variant in range(VARIANTS):
            ctx = BuildContext(T20, variant, seed)
            build_t20(ctx)
            [entity] = ctx.graph.entities
            rows.append(
                (
                    entity.canonical_name,
                    entity.aliases[0],
                    [(c.predicate, int(c.object.value)) for c in ctx.graph.claims],
                )
            )
        sweep.append((seed, rows))
    return sweep


def _near_duplicate(first: str, second: str) -> bool:
    """Names a retriever could confuse: equal, one a token-prefix of the other,
    or sharing stem and suffix and differing only in trailing decoration."""

    left, right = first.split(), second.split()
    if first == second:
        return True
    if left[: len(right)] == right or right[: len(left)] == left:
        return True
    return left[:2] == right[:2]


def test_variant_org_names_are_distinct_not_merely_marked(
    variant_sweep: list[tuple[int, list[_VariantRow]]],
) -> None:
    """No two variants may hold confusable organisation names.

    The previous version of this test drew names straight from
    ``wordbank.org_name_cyr(..., discriminator=variant)`` and asserted the four
    results differed. The discriminator appended a distinct per-variant marker,
    so that assertion held by construction and could not fail: it exercised the
    template's inputs rather than the template, and it hid the real defect. The
    *base* names collided in 158 of 1000 seeds, leaving two organisations that
    differed only by a trailing token — a retrieval confound that would have
    been scored as a cross-lingual failure of the contender rather than of the
    harness. This version runs ``build`` and compares what a retriever actually
    has to tell apart, so reintroducing any decorate-instead-of-redraw scheme
    fails here.
    """

    for seed, rows in variant_sweep:
        natives = [native for native, _, _ in rows]
        latins = [latin for _, latin, _ in rows]
        assert len(set(natives)) == VARIANTS, seed
        assert len(set(latins)) == VARIANTS, seed
        for index, first in enumerate(natives):
            for second in natives[index + 1 :]:
                assert not _near_duplicate(first, second), (seed, first, second)
        for index, first in enumerate(latins):
            for second in latins[index + 1 :]:
                assert not _near_duplicate(first, second), (seed, first, second)


def test_archive_and_routing_counts_are_never_confusable(
    variant_sweep: list[tuple[int, list[_VariantRow]]],
) -> None:
    """A wrong-metric answer must never land on the right number.

    ``archive_count`` and ``routing_count`` are both bare unit counts of the
    same organisation. With the ranges overlapping, 12 of 2000 variants gave an
    archive count equal to a routing count, so answering the wrong metric scored
    as correct. The ranges are now disjoint by construction; this enumerates the
    whole sweep rather than trusting the arithmetic.
    """

    for seed, rows in variant_sweep:
        for variant, (_, _, quantities) in enumerate(rows):
            where = (seed, variant, quantities)
            values = [value for _, value in quantities]
            assert len(values) == 3, where
            assert len(set(values)) == 3, where
            archive = [v for predicate, v in quantities if predicate == "archive_count"]
            routing = [v for predicate, v in quantities if predicate == "routing_count"]
            assert len(archive) == 1, where
            assert len(routing) == 2, where
            assert min(archive) > max(routing), where


def test_cross_script_golden_requires_native_source_value_and_citation(
    corpus: Path,
) -> None:
    entities, queries, expected, claims, sources = _records(corpus)
    entities_by_id = {entity.entity_id: entity for entity in entities}
    direct = [q for q in queries if q.query_kind == "cross_script_direct_recall"]
    assert len(direct) == 4
    for query in direct:
        record = expected[query.query_id]
        [claim_id] = record.required_claims
        claim = claims[claim_id]
        entity = entities_by_id[claim.subject]
        [latin_alias] = entity.aliases
        [source_id] = record.required_citations

        assert latin_alias in query.prompt_text
        assert entity.canonical_name not in query.prompt_text
        assert _ASCII_LETTER.search(latin_alias)
        assert not _ASCII_LETTER.search(entity.canonical_name)
        assert record.answer.values == [claim.object.value]
        assert source_id in {a.source_id for a in claim.assertions}
        assert not _ASCII_LETTER.search(sources[source_id].title)

        source_text = (corpus / sources[source_id].path).read_text(encoding="utf-8")
        assert f"[ref:{source_id}]" in source_text


def test_right_value_without_native_source_citation_fails(corpus: Path) -> None:
    entities, queries, expected, claims, sources = _records(corpus)
    query = next(q for q in queries if q.query_kind == "cross_script_direct_recall")
    record = expected[query.query_id]
    [value] = record.answer.values
    ctx = ScoringContext(
        claims_by_id=claims,
        sources_by_id=sources,
    entities_by_id={e.entity_id: e for e in entities},
    )
    answer = AnswerRecord(query_id=query.query_id, answer_text=f"The count is {value}.")

    item = gate_citations(query, record, answer, ctx)
    assert item.status is GateStatus.FAIL
    assert "missing citations" in (item.evidence or "")


def _queries_by_entity(
    queries: list[QueryRecord],
    expected: dict[str, ExpectedRecord],
    claims: dict[str, ClaimRecord],
) -> dict[str, dict[str, QueryRecord]]:
    """Answerable queries grouped by the organisation they are about."""

    grouped: dict[str, dict[str, QueryRecord]] = {}
    for query in queries:
        record = expected[query.query_id]
        if not record.required_claims:
            continue
        entity_id = claims[record.required_claims[0]].subject
        grouped.setdefault(entity_id, {})[query.query_kind] = query
    return grouped


def test_citing_the_wrong_source_fails_even_with_the_right_value(corpus: Path) -> None:
    """Right number, wrong provenance must fail — not merely a missing citation.

    ``test_right_value_without_native_source_citation_fails`` only covers an
    answer that cites nothing. This pins the harder case: the answer carries a
    real sentinel from this corpus that is not the source recording the fact —
    the same organisation's later revision ledger, and a *different*
    organisation's ledger, which is the confound a cross-lingual retriever
    actually falls into.

    Note there is no Latin-script source to cite: every t20 source is
    native-script by construction (asserted by
    ``test_native_sources_and_same_script_control_stay_non_latin``), so the
    other organisation's ledger is the strongest available wrong-source
    citation for the same question asked in the other script.
    """

    entities, queries, expected, claims, sources = _records(corpus)
    grouped = _queries_by_entity(queries, expected, claims)
    ctx = ScoringContext(
        claims_by_id=claims,
        sources_by_id=sources,
    entities_by_id={e.entity_id: e for e in entities},
    )
    entity_ids = sorted(grouped)
    assert len(entity_ids) == VARIANTS

    for index, entity_id in enumerate(entity_ids):
        kinds = grouped[entity_id]
        for asked in ("cross_script_direct_recall", "same_script_control"):
            query = kinds[asked]
            record = expected[query.query_id]
            [right_source] = record.required_citations
            [value] = record.answer.values

            other = entity_ids[(index + 1) % VARIANTS]
            [other_source] = expected[
                grouped[other]["cross_script_direct_recall"].query_id
            ].required_citations
            [revision_source] = expected[
                kinds["cross_script_current_truth"].query_id
            ].required_citations

            for wrong_source in (revision_source, other_source):
                assert wrong_source != right_source
                answer = extract_structure(
                    AnswerRecord(
                        query_id=query.query_id,
                        answer_text=f"The count is {value}. {sentinel(wrong_source)}",
                    )
                )
                assert answer.citations == [wrong_source]
                item = gate_citations(query, record, answer, ctx)
                assert item.status is GateStatus.FAIL, (asked, wrong_source)
                assert right_source in (item.evidence or "")

            good = extract_structure(
                AnswerRecord(
                    query_id=query.query_id,
                    answer_text=f"The count is {value}. {sentinel(right_source)}",
                )
            )
            assert gate_citations(query, record, good, ctx).status is GateStatus.PASS


def test_same_script_control_matches_its_cross_script_twin(corpus: Path) -> None:
    """The control only controls if it asks for exactly the same fact.

    ``same_script_control`` and ``cross_script_direct_recall`` must resolve to
    the same claim, the same expected value and unit, and the same required
    citation. If they diverged, a gap between the two would not be attributable
    to the script change and the family would measure nothing.
    """

    _, queries, expected, claims, _ = _records(corpus)
    grouped = _queries_by_entity(queries, expected, claims)
    assert len(grouped) == VARIANTS
    for entity_id, kinds in grouped.items():
        cross = kinds["cross_script_direct_recall"]
        control = kinds["same_script_control"]
        cross_record = expected[cross.query_id]
        control_record = expected[control.query_id]

        assert control_record.required_claims == cross_record.required_claims, entity_id
        assert control_record.answer.values == cross_record.answer.values, entity_id
        assert control_record.answer.unit == cross_record.answer.unit, entity_id
        assert control_record.answer.kind == cross_record.answer.kind, entity_id
        assert (
            control_record.required_citations == cross_record.required_citations
        ), entity_id
        assert control_record.abstain is False, entity_id
        assert cross_record.abstain is False, entity_id
        assert control.ask == cross.ask, entity_id

        # Only the script of the question differs.
        assert control.prompt_text != cross.prompt_text, entity_id
        assert _ASCII_LETTER.search(cross.prompt_text)
        assert not _ASCII_LETTER.search(control.prompt_text)


def test_current_truth_forbids_old_native_script_claim(corpus: Path) -> None:
    _, queries, expected, claims, _ = _records(corpus)
    current = [q for q in queries if q.query_kind == "cross_script_current_truth"]
    assert len(current) == 4
    for query in current:
        record = expected[query.query_id]
        [current_id] = record.required_claims
        [old_id] = record.forbidden_claims
        assert record.answer.values == [claims[current_id].object.value]
        assert claims[old_id].superseded_by == current_id
        assert record.required_citations == [claims[current_id].assertions[0].source_id]


def test_native_sources_and_same_script_control_stay_non_latin(corpus: Path) -> None:
    entities, queries, expected, _, sources = _records(corpus)
    assert len(sources) >= 2 * 4
    for source in sources.values():
        assert not _ASCII_LETTER.search(source.title)
        text = (corpus / source.path).read_text(encoding="utf-8")
        fact_text = "\n".join(
            line for line in text.splitlines() if not line.startswith("[ref:")
        )
        assert not _ASCII_LETTER.search(fact_text)

    native_names = {entity.canonical_name for entity in entities}
    controls = [q for q in queries if q.query_kind == "same_script_control"]
    assert len(controls) == 4
    for query in controls:
        assert any(name in query.prompt_text for name in native_names)
        assert not expected[query.query_id].abstain


def test_cross_lingual_wordbank_and_generated_corpus_pass_privacy_scan(
    corpus: Path, tmp_path: Path
) -> None:
    rng = Random(17)
    sample = tmp_path / "cross-lingual-wordbank.md"
    pairs = [
        pair
        for _ in range(200)
        for pair in (wordbank.person_name_cyr(rng), wordbank.org_name_cyr(rng))
    ]
    sample.write_text("\n".join(part for pair in pairs for part in pair), encoding="utf-8")
    findings = list(scan_artifact(sample, label=sample.name))
    for path in sorted(corpus.rglob("*")):
        if path.is_file() and path.suffix in {
            ".md",
            ".csv",
            ".txt",
            ".jsonl",
            ".json",
            ".yaml",
        }:
            findings.extend(scan_artifact(path, label=path.name))
    assert findings == []


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_t20_double_generation_is_byte_identical(tmp_path: Path) -> None:
    first = generate_corpus(7, tmp_path / "a", template_ids=[T20])
    second = generate_corpus(7, tmp_path / "b", template_ids=[T20])
    assert first == second
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")
