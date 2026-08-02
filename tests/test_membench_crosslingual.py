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
from membench.schema import (
    ClaimRecord,
    EntityRecord,
    ExpectedAnswer,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    load_jsonl,
)
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.gates import GateStatus, ScoringContext, gate_citations
from membench.templates import registry
from membench.templates.base import BuildContext, Template

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


def test_paired_org_names_are_variant_unique_across_many_seeds() -> None:
    for seed in range(1_000):
        pairs = [
            wordbank.org_name_cyr(
                BuildContext(T20, variant, seed).rng, discriminator=variant
            )
            for variant in range(4)
        ]
        assert len({native for native, _ in pairs}) == 4, seed
        assert len({latin for _, latin in pairs}) == 4, seed


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
    _, queries, expected, claims, sources = _records(corpus)
    query = next(q for q in queries if q.query_kind == "cross_script_direct_recall")
    record = expected[query.query_id]
    [value] = record.answer.values
    ctx = ScoringContext(claims_by_id=claims, sources_by_id=sources)
    answer = AnswerRecord(query_id=query.query_id, answer_text=f"The count is {value}.")

    item = gate_citations(query, record, answer, ctx)
    assert item.status is GateStatus.FAIL
    assert "missing citations" in (item.evidence or "")


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
