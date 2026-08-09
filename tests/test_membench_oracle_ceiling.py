"""The oracle-retrieval ceiling: a contender that cannot be beaten.

This adapter exists to measure the harness, not a product. It returns exactly
the sources the oracle admits for each query, so whatever it scores is the best
any retriever could earn *under this scorer*. Two consequences make it worth
its weight:

- A dimension where the ceiling is below 100% is a scorer or corpus defect, not
  a product finding. Every such gap this suite has found so far — 4b.21's
  unwinnable queries, 4b.22's co-presence failures, 4b.31's shotgun provenance —
  was found by hand, one incident at a time. The ceiling finds them in one run.
- A published number without a ceiling has no denominator a reader can reason
  about. "148 of 180" says nothing until you know whether 180 was reachable.

It must never see the expected *answer*. Retrieval ground truth (which sources
bear on the query) is a fair ceiling; answer ground truth would make it an
oracle answerer instead, and it would stop measuring retrieval at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from membench.adapters import create_adapter
from membench.adapters.base import Capability, Profile
from membench.generate import generate_corpus

T00 = "t00_mini_smoke"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("ceiling-corpus") / "s1"
    generate_corpus(1, root, template_ids=[T00])
    return root


def _ready(corpus: Path, workdir: Path):
    adapter = create_adapter("oracle-retrieval")
    adapter.setup(workdir, Profile(name="oracle-ceiling"))
    adapter.ingest(corpus, workdir / "native")
    return adapter


def _queries(corpus: Path) -> list[dict]:
    return [json.loads(line) for line in (corpus / "queries.jsonl").read_text().splitlines()]


def _expected(corpus: Path) -> dict[str, dict]:
    return {
        rec["query_id"]: rec
        for rec in (
            json.loads(line) for line in (corpus / "expected.jsonl").read_text().splitlines()
        )
    }


def test_it_declares_only_what_it_does(corpus: Path, tmp_path: Path) -> None:
    adapter = _ready(corpus, tmp_path)
    assert adapter.capabilities() == frozenset({Capability.INGEST_API, Capability.SEARCH})


def test_every_required_citation_is_returned_and_ranked_first(
    corpus: Path, tmp_path: Path
) -> None:
    """The ceiling has to actually reach the ceiling.

    Required sources ranking first is not cosmetic: the shared answerer quotes
    only its top hits, so a required source ranked below the cut would be
    retrieved and then dropped before scoring.
    """

    adapter = _ready(corpus, tmp_path)
    expected = _expected(corpus)
    checked = 0
    for query in _queries(corpus):
        required = expected[query["query_id"]].get("required_citations") or []
        if not required:
            continue
        hits = adapter.search(query["prompt_text"], 10)
        returned = [hit.provider_path for hit in hits]
        assert set(required) <= set(returned), (
            f"{query['query_id']}: required {required} not all returned ({returned})"
        )
        assert returned[: len(required)] == list(dict.fromkeys(required)), (
            f"{query['query_id']}: required citations must rank first, got {returned}"
        )
        checked += 1
    assert checked, "no query in this corpus required a citation"


def test_it_returns_nothing_outside_the_permitted_set(corpus: Path, tmp_path: Path) -> None:
    """Precision is the other half of a ceiling.

    A ceiling that returned the whole corpus would trivially satisfy recall and
    would fail citation precision everywhere — the 4b.31 shotgun, wearing a
    different hat.
    """

    from membench import oracle
    from membench.schema import ClaimRecord, EntityRecord, ExpectedRecord, SourceRecord, load_jsonl

    adapter = _ready(corpus, tmp_path)
    claims = {c.claim_id: c for c in load_jsonl(ClaimRecord, corpus / "claims.jsonl")}
    entities = {e.entity_id: e for e in load_jsonl(EntityRecord, corpus / "entities.jsonl")}
    sources = {s.source_id: s for s in load_jsonl(SourceRecord, corpus / "sources.jsonl")}
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, corpus / "expected.jsonl")}

    for query in _queries(corpus):
        exp = expected[query["query_id"]]
        permitted, unverifiable = oracle.permitted_citations(
            exp,
            claims_by_id=claims,
            knowledge_week=query["ask"]["knowledge_week"],
            entities_by_id=entities,
            sources_by_id=sources,
        )
        if unverifiable is not None:
            continue
        hits = adapter.search(query["prompt_text"], 10)
        returned = {hit.provider_path for hit in hits}
        assert returned <= set(permitted), (
            f"{query['query_id']}: returned {returned - set(permitted)} outside permitted set"
        )


def test_the_expected_answer_never_reaches_retrieval(corpus: Path, tmp_path: Path) -> None:
    """Structural guard against turning the ceiling into an oracle answerer.

    Retrieval ground truth is a fair ceiling; answer ground truth is not. The
    proof is behavioural rather than by inspection: rewrite every expected
    answer value in the corpus and require the hits to be byte-identical. If
    any answer text leaked into ranking or selection, this moves.
    """

    baseline = _ready(corpus, tmp_path / "before")
    prompts = [q["prompt_text"] for q in _queries(corpus)]
    before = {p: [h.provider_path for h in baseline.search(p, 10)] for p in prompts}

    scrubbed = tmp_path / "scrubbed"
    scrubbed.mkdir()
    for name in ("claims.jsonl", "entities.jsonl", "sources.jsonl", "queries.jsonl"):
        (scrubbed / name).write_text((corpus / name).read_text(), encoding="utf-8")
    (scrubbed / "sources").symlink_to(corpus / "sources")
    rewritten = []
    for line in (corpus / "expected.jsonl").read_text().splitlines():
        record = json.loads(line)
        answer = record.get("answer") or {}
        if answer.get("values"):
            answer["values"] = [f"MUTATED-{index}" for index, _ in enumerate(answer["values"])]
        record["answer"] = answer
        rewritten.append(json.dumps(record))
    (scrubbed / "expected.jsonl").write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    mutated = _ready(scrubbed, tmp_path / "after")
    after = {p: [h.provider_path for h in mutated.search(p, 10)] for p in prompts}
    assert after == before


def test_an_unknown_prompt_returns_nothing(corpus: Path, tmp_path: Path) -> None:
    """No fabrication. A prompt the oracle has no record of gets no hits."""

    adapter = _ready(corpus, tmp_path)
    assert adapter.search("what is the airspeed velocity of an unladen swallow?", 10) == []


def test_hits_carry_the_sentinels_the_scorer_cites(corpus: Path, tmp_path: Path) -> None:
    """Citations are re-derived from sentinels in the quoted text, so a hit
    without its sentinel would retrieve correctly and cite nothing."""

    adapter = _ready(corpus, tmp_path)
    expected = _expected(corpus)
    for query in _queries(corpus):
        required = expected[query["query_id"]].get("required_citations") or []
        if not required:
            continue
        for hit in adapter.search(query["prompt_text"], 10):
            if hit.provider_path in required:
                assert hit.sentinels, f"{hit.provider_path} carries no sentinel"
                assert hit.text, f"{hit.provider_path} carries no text to quote"
        break


def test_the_limit_is_honoured(corpus: Path, tmp_path: Path) -> None:
    adapter = _ready(corpus, tmp_path)
    for query in _queries(corpus):
        assert len(adapter.search(query["prompt_text"], 2)) <= 2


# --- compiled altitude (4b.38) -----------------------------------------------


def _ready_compiled(corpus: Path, workdir: Path):
    adapter = create_adapter("oracle-retrieval", altitude="compiled")
    adapter.setup(workdir, Profile(name="oracle-ceiling-compiled"))
    adapter.ingest(corpus, workdir / "native")
    return adapter


def test_compiled_altitude_retrieves_conclusions_not_sources(
    corpus: Path, tmp_path: Path
) -> None:
    """4b.38: the ceiling served raw sources at *both* altitudes.

    `search` never consulted `self.altitude`, so the two runs' retrieval
    artifacts were byte-identical at 528 hits. Everything the compiled ceiling
    appeared to prove about contradiction came from the calibration gate
    reading the corpus's declared dispute structure, not from retrieving
    compiled conclusions -- and temporal being unchanged across the two runs
    was the necessary consequence rather than a coincidence.

    A ceiling that cannot express an altitude cannot bound a contender measured
    at it, which left compiled contender runs with no denominator: the exact
    gap the ceiling exists to close.
    """

    raw = _ready(corpus, tmp_path / "raw")
    compiled = _ready_compiled(corpus, tmp_path / "compiled")
    prompt = _queries(corpus)[0]["prompt_text"]

    raw_hits = raw.search(prompt, 10)
    compiled_hits = compiled.search(prompt, 10)
    assert raw_hits, "precondition: the raw ceiling retrieves something"
    assert compiled_hits, "the compiled ceiling must retrieve something"

    assert all(h.provider_path.startswith("SRC-") for h in raw_hits)
    assert all(h.provider_path.startswith("CON-") for h in compiled_hits), [
        h.provider_path for h in compiled_hits
    ]


def test_a_compiled_hit_carries_its_declared_basis_as_sentinels(
    corpus: Path, tmp_path: Path
) -> None:
    """Chain preservation is the whole point of the compiled tier.

    A conclusion's body states the value; its basis lives in `cites`. The
    scorer reads citations from a hit's sentinels, so serving the body without
    the cites would retrieve knowledge that had lost its provenance -- and the
    provenance column would then measure the tier's plumbing rather than any
    contender's chain.
    """

    compiled = _ready_compiled(corpus, tmp_path / "compiled")
    plan = {
        rec["conclusion_id"]: rec
        for rec in (
            json.loads(line)
            for line in (corpus / "compile-plan.jsonl").read_text().splitlines()
        )
    }
    hits = compiled.search(_queries(corpus)[0]["prompt_text"], 10)
    assert hits
    for hit in hits:
        declared = tuple(plan[hit.provider_path]["cites"])
        assert declared, f"{hit.provider_path} has no declared basis"
        assert set(declared) <= set(hit.sentinels), (hit.provider_path, hit.sentinels)


def test_the_compiled_ceiling_states_every_required_value(
    corpus: Path, tmp_path: Path
) -> None:
    """The ceiling must be reachable, not merely differently shaped.

    If the compiled tier retrieved conclusions whose text omitted the value the
    oracle requires, the compiled ceiling would sit below the raw one and every
    contender measured against it would be graded on an unreachable bar.
    """

    compiled = _ready_compiled(corpus, tmp_path / "compiled")
    claims = {
        rec["claim_id"]: rec
        for rec in (
            json.loads(line) for line in (corpus / "claims.jsonl").read_text().splitlines()
        )
    }
    expected = _expected(corpus)
    for query in _queries(corpus):
        required = expected[query["query_id"]].get("required_claims") or []
        if not required:
            continue
        text = " ".join(h.text or "" for h in compiled.search(query["prompt_text"], 10))
        for claim_id in required:
            value = claims[claim_id]["object"]["value"]
            assert value in text, (query["query_id"], claim_id, value, text[:200])
