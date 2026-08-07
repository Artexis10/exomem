"""Oracle-retrieval adapter: the ceiling, and a self-test for the harness.

This is not a product. It answers every query with exactly the sources the
oracle admits for it, so whatever it scores is **the best score any retriever
could earn under this scorer**. Two things follow, and both are the point:

- **A dimension whose ceiling is below 100% has a harness defect, not a product
  finding.** Every such defect this suite has found was found by hand, one
  incident at a time: 4b.21's queries whose prompt supplied a forbidden value,
  4b.22's co-presence rule failing correct answers, 4b.31's provenance column
  scoring the answerer's citation policy. A ceiling run surfaces the whole
  class in one pass, and keeps surfacing it as the suite grows.
- **A published figure without a ceiling has no denominator.** "148 of 180"
  is uninterpretable until a reader knows whether 180 was reachable. Industry
  benchmarks publish bounds for exactly this reason.

What it may read
================

Retrieval ground truth only: which sources bear on the query, via
:func:`oracle.permitted_citations` — ``required_citations`` closed over the
evidence neighbourhood. It must **never** read ``ExpectedRecord.answer``. That
line is what keeps it a *retrieval* ceiling: an adapter that saw the answer
would be an oracle answerer, would score 100% by construction, and would
measure nothing. ``test_the_expected_answer_never_reaches_retrieval`` enforces
this behaviourally by mutating every expected value and requiring identical
hits, rather than trusting the class to be well-behaved.

Ranking
=======

Required citations first, in declaration order, then the rest of the permitted
set in corpus order. This is not cosmetic. The shared extractive answerer
quotes only its top hits, so a required source ranked below that cut would be
retrieved and then dropped before scoring — the ceiling would understate itself
for a reason that has nothing to do with retrieval.

Prompt lookup, and its one honest limitation
============================================

The adapter contract passes ``search(query_text, limit)`` — no query id — so
the oracle is reached by prompt text. Seed-1 has 234 distinct prompts across
240 queries; where several queries share a prompt the adapter cannot tell them
apart and serves the **union** of their permitted sets, which is the smallest
set that satisfies whichever query it actually is. Those collisions are a
corpus defect in their own right (task 4b.32: three of them declare mutually
exclusive expected values under a byte-identical prompt), and the union is
recorded in :meth:`version_info` so a ceiling that dips there is attributable
rather than mysterious.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path

from membench import oracle
from membench.adapters.base import (
    AdapterEnvironmentError,
    AdapterUnsupported,
    Capability,
    Hit,
    OpResult,
    Profile,
    StateExport,
    register_adapter,
)
from membench.ids import sentinel, sentinels_in
from membench.schema import (
    ClaimRecord,
    EntityRecord,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    load_jsonl,
)

PROFILE_NOTE = (
    "oracle retrieval ceiling: returns exactly the oracle-permitted source set "
    "per query; reads retrieval ground truth only, never the expected answer"
)


class OracleRetrievalAdapter:
    name = "oracle-retrieval"
    supports_group_reuse = False

    def __init__(self, *, mode: str = "leaf", search_style: str = "neutral") -> None:
        # The runner hands every adapter the run's mode and search style. The
        # ceiling has no wire path and no product search to style, so both are
        # recorded rather than applied — a run that requested `wire` and got a
        # ceiling should be able to see that from the artifacts instead of
        # assuming the knob did something.
        self._mode = mode
        self._search_style = search_style
        self._workdir: Path | None = None
        self._profile: Profile | None = None
        #: prompt text -> ordered source ids (required first, then permitted)
        self._by_prompt: dict[str, list[str]] = {}
        #: source id -> stored text, for the shared answerer to quote
        self._texts: dict[str, str] = {}
        self._collisions: dict[str, int] = {}
        self._degraded: dict[str, str] = {}
        self._unverifiable = 0

    # -- lifecycle --------------------------------------------------------
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.INGEST_API, Capability.SEARCH})

    def setup(self, workdir: Path, profile: Profile) -> None:
        self._workdir = Path(workdir)
        self._profile = profile
        self._workdir.mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> None:
        self._by_prompt.clear()
        self._texts.clear()

    # -- ingest -----------------------------------------------------------
    def ingest(self, corpus_dir: Path, native_dir: Path) -> list[OpResult]:
        """Load the corpus and precompute each prompt's permitted source set.

        There is no product to write into, so "ingest" here means building the
        oracle index. The per-source OpResults keep the ingested-doc-count
        parity check (falsification register item 2) meaningful: a ceiling that
        silently saw fewer documents than a contender would not be a ceiling.
        """

        corpus_dir = Path(corpus_dir)
        try:
            claims = {c.claim_id: c for c in load_jsonl(ClaimRecord, corpus_dir / "claims.jsonl")}
            entities = {
                e.entity_id: e for e in load_jsonl(EntityRecord, corpus_dir / "entities.jsonl")
            }
            sources = {
                s.source_id: s for s in load_jsonl(SourceRecord, corpus_dir / "sources.jsonl")
            }
            queries = load_jsonl(QueryRecord, corpus_dir / "queries.jsonl")
            expected = {
                e.query_id: e for e in load_jsonl(ExpectedRecord, corpus_dir / "expected.jsonl")
            }
        except FileNotFoundError as exc:
            raise AdapterEnvironmentError(f"corpus incomplete: {exc}") from exc

        results: list[OpResult] = []
        for seq, (source_id, source) in enumerate(sources.items()):
            started = time.perf_counter()
            artifact = corpus_dir / source.path
            try:
                self._texts[source_id] = artifact.read_text(encoding="utf-8")
                ok, detail = True, None
            except (OSError, UnicodeDecodeError):
                # A binary artifact (PNG/PDF) holds its value in pixels, which a
                # text-only ceiling cannot read — and should not pretend to.
                # Indexing still *succeeded*: the source stays citable via its
                # title and sentinel, so provenance remains measurable while the
                # value genuinely is not. Recording this as a failure would have
                # been worse in both directions — it would drop the sentinel, so
                # every required citation pointing at a PNG would fail, and the
                # provenance ceiling would understate itself for a reason that
                # has nothing to do with retrieval.
                self._texts[source_id] = f"{source.title}\n\n{sentinel(source_id)}\n"
                self._degraded[source_id] = f"{source.artifact_kind.value}: cited, not quotable"
                ok, detail = True, self._degraded[source_id]
            results.append(
                OpResult(
                    seq=seq,
                    op="index",
                    source_id=source_id,
                    ok=ok,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    detail=detail,
                )
            )

        staged: dict[str, list[list[str]]] = defaultdict(list)
        for query in queries:
            exp = expected.get(query.query_id)
            if exp is None:
                continue
            permitted, unverifiable = oracle.permitted_citations(
                exp,
                claims_by_id=claims,
                knowledge_week=query.ask.knowledge_week,
                entities_by_id=entities,
                sources_by_id=sources,
            )
            if unverifiable is not None:
                # No claim basis: the oracle cannot say which sources bear on
                # this query, so the ceiling declines to guess and serves the
                # declared required citations alone.
                self._unverifiable += 1
            required = list(dict.fromkeys(exp.required_citations))
            rest = [s for s in sorted(permitted) if s not in required]
            staged[query.prompt_text].append(required + rest)

        for prompt, variants in staged.items():
            if len(variants) > 1:
                self._collisions[prompt] = len(variants)
            merged: dict[str, None] = {}
            for variant in variants:
                for source_id in variant:
                    merged.setdefault(source_id)
            self._by_prompt[prompt] = list(merged)
        return results

    # -- search -----------------------------------------------------------
    def search(self, query: str, limit: int) -> list[Hit]:
        if self._workdir is None:
            raise AdapterEnvironmentError("adapter not set up")
        ordered = self._by_prompt.get(query)
        if not ordered:
            # A prompt the oracle has no record of. Returning nothing is the
            # honest answer; inventing hits would make the ceiling unfalsifiable.
            return []
        hits: list[Hit] = []
        for rank, source_id in enumerate(ordered[:limit], start=1):
            text = self._texts.get(source_id, "")
            hits.append(
                Hit(
                    rank=rank,
                    provider_path=source_id,
                    title=None,
                    excerpt=text[:200] or None,
                    sentinels=tuple(sentinels_in(text)),
                    raw={"source_id": source_id, "oracle": True},
                    text=text or None,
                )
            )
        return hits

    # -- state export ------------------------------------------------------
    def export_state(self) -> StateExport:
        raise AdapterUnsupported("the oracle ceiling declares no STATE_EXPORT")

    def version_info(self) -> dict[str, str]:
        info = {
            "provider": self.name,
            "profile_note": PROFILE_NOTE,
            "indexed_sources": str(len(self._texts)),
            "indexed_prompts": str(len(self._by_prompt)),
            # Attribution for any ceiling dip: a shared prompt is served the
            # union of its queries' permitted sets, so precision can suffer for
            # a corpus reason rather than a retrieval one (task 4b.32).
            "colliding_prompts": str(len(self._collisions)),
            "queries_without_claim_basis": str(self._unverifiable),
            "sources_cited_not_quotable": str(len(self._degraded)),
            "requested_mode": self._mode,
            "requested_search_style": self._search_style,
        }
        if self._profile is not None:
            info["profile"] = self._profile.name
        return info


register_adapter("oracle-retrieval", lambda **kw: OracleRetrievalAdapter(**kw))
