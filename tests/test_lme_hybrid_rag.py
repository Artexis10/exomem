from __future__ import annotations

import dataclasses
import hashlib
import time

import pytest


def _identity():
    from protocol.models import DatasetIdentity

    return DatasetIdentity(id="fixture", variant="mini", source="local", revision="1", sha256="a" * 64, case_count=1)


def _event(content: str, *, session_ordinal: int = 1, case_id: str = "case-1"):
    from protocol.models import EventProvenance, ProtocolEvent

    return ProtocolEvent(
        dataset=_identity(), case_id=case_id, session_ordinal=session_ordinal, sequence=0, role="user",
        turn_ordinal=1, content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        original_timestamp="2026-01-01T00:00:00Z", timestamp_semantics="event_time_declared_by_dataset",
        ingestion_ordinal=0,
        provenance=EventProvenance(dataset_row_index=0, upstream_session_id_sha256="b" * 64, converter="fixture", converter_version="1"),
    )


def _handle(case_id: str = "case-1"):
    from protocol.models import CaseHandle

    return CaseHandle(case_id=case_id, case_ordinal=1, question_date="2026-01-02T00:00:00Z")


def _provider(monkeypatch):
    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider

    monkeypatch.setenv("PROTOCOL_FIXTURE_EMBEDDER", "1")
    provider = HybridRagDirectProvider()
    provider.setup(None)
    return provider


def test_hybrid_rag_is_byte_identical_with_fixture_embedder(monkeypatch) -> None:
    provider = _provider(monkeypatch)
    provider.ingest_case([_event("The clockwork fox visits every Thursday afternoon.")], _handle())
    first = provider.retrieve("When does the clockwork fox visit?", 3)
    second = provider.retrieve("When does the clockwork fox visit?", 3)
    assert first == second
    assert repr(first).encode() == repr(second).encode()


def test_setup_refuses_to_pretend_a_model_without_the_declared_seam(monkeypatch) -> None:
    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider

    monkeypatch.delenv("PROTOCOL_FIXTURE_EMBEDDER", raising=False)
    with pytest.raises(RuntimeError, match="PROTOCOL_FIXTURE_EMBEDDER"):
        HybridRagDirectProvider().setup(None)


def test_config_records_only_what_actually_ran(monkeypatch) -> None:
    """RM7: no dead model field may sit in the hash that claims to describe the run."""

    from lme.providers.hybrid_rag_direct import HybridRagConfig, HybridRagDirectProvider

    fields = {field.name for field in dataclasses.fields(HybridRagConfig)}
    assert "embedding_model" not in fields, "dead config field still claims a model that never loads"
    monkeypatch.setenv("PROTOCOL_FIXTURE_EMBEDDER", "1")
    fixture = HybridRagDirectProvider()
    assert fixture.config.embedding_backend == "fixture-hash-32"
    assert fixture.variant_id() == "hybrid-rag-fixture"
    monkeypatch.delenv("PROTOCOL_FIXTURE_EMBEDDER", raising=False)
    real = HybridRagDirectProvider()
    assert real.variant_id() == "hybrid-rag-control"
    assert fixture.config.sha256() != real.config.sha256()


def test_chunking_prefers_sentence_boundaries_and_never_spans_sessions(monkeypatch) -> None:
    """RM7/RM8 chunker edge: a chunk belongs to exactly one session and ends on a sentence."""

    provider = _provider(monkeypatch)
    first = " ".join(f"Alpha sentence number {index} about lanterns." for index in range(200))
    second = " ".join(f"Beta sentence number {index} about harbours." for index in range(200))
    provider.ingest_case(
        [_event(first, session_ordinal=1), _event(second, session_ordinal=2)], _handle()
    )
    chunks = provider.export_state()
    assert len(chunks) > 2, "a 200-sentence session must produce more than one chunk"
    for chunk in chunks:
        assert not ("Alpha" in chunk.text and "Beta" in chunk.text), "chunk spans two sessions"
        assert chunk.text.rstrip().endswith("."), "chunk does not end on a sentence boundary"
    assert {chunk.session_ordinal for chunk in chunks} == {1, 2}


def test_short_session_yields_exactly_one_chunk_holding_the_whole_text(monkeypatch) -> None:
    """RM7/RM8 chunker edge: the short-session case must not degenerate or duplicate."""

    provider = _provider(monkeypatch)
    provider.ingest_case([_event("A single short line.")], _handle())
    chunks = provider.export_state()
    assert len(chunks) == 1
    assert chunks[0].text.strip() == "A single short line."


def test_bm25_ranking_matches_the_rank_bm25_reference_implementation(monkeypatch) -> None:
    """RM7: the declared BM25 parameters are the ones that actually score."""

    from rank_bm25 import BM25Okapi

    provider = _provider(monkeypatch)
    corpus = [
        "The harbour lantern was repainted in seafoam green.",
        "A clockwork fox visits the harbour every Thursday.",
        "Nothing in this line mentions the same subject at all.",
    ]
    provider.ingest_case([_event(text, session_ordinal=index + 1) for index, text in enumerate(corpus)], _handle())
    chunks = provider.export_state()
    query = "harbour lantern"
    from lme.providers.hybrid_rag_direct import tokenize

    reference = BM25Okapi([list(chunk.tokens) for chunk in chunks], k1=provider.config.bm25_k1, b=provider.config.bm25_b)
    scored = sorted(
        ((-float(score), chunk.chunk_id) for score, chunk in zip(reference.get_scores(tokenize(query)), chunks, strict=True))
    )
    assert provider.lexical_ranking(query) == [chunk_id for _score, chunk_id in scored]


def test_rrf_tie_break_is_deterministic_for_equal_scores(monkeypatch) -> None:
    """RM8: equal-score fusion resolves by chunk id, identically across instances."""

    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider

    monkeypatch.setenv("PROTOCOL_FIXTURE_EMBEDDER", "1")
    from lme.providers.hybrid_rag_direct import tokenize

    identical = "Identical harbour lantern sentence."
    orders = []
    for _attempt in range(2):
        provider = HybridRagDirectProvider()
        provider.setup(None)
        provider.ingest_case(
            [_event(identical, session_ordinal=1), _event(identical, session_ordinal=2), _event(identical, session_ordinal=3)],
            _handle(),
        )
        chunk_ids = sorted(chunk.chunk_id for chunk in provider.export_state())
        assert len(chunk_ids) == 3
        raw = {round(float(score), 12) for score in provider._index.get_scores(tokenize("harbour lantern"))}
        assert len(raw) == 1, f"fixture did not produce a genuine equal-score case: {raw}"
        # Both rankings see an exact score tie, so each must fall back to the
        # chunk id; RRF then inherits that order.
        assert provider.lexical_ranking("harbour lantern") == chunk_ids
        assert provider.semantic_ranking("harbour lantern") == chunk_ids
        orders.append([hit.hit_id for hit in provider.retrieve("harbour lantern", 3)])
    assert orders[0] == sorted(orders[0])
    assert orders[0] == orders[1]


def test_cached_index_keeps_five_hundred_chunks_and_ten_queries_within_the_bound(monkeypatch) -> None:
    """RM7: document-frequency tables and chunk embeddings are computed once at ingest."""

    provider = _provider(monkeypatch)
    events = [
        _event(" ".join(f"tok{(index * 37 + word) % 900} sentence." for word in range(256)), session_ordinal=index + 1)
        for index in range(500)
    ]
    started = time.perf_counter()
    provider.ingest_case(events, _handle())
    ingest_seconds = time.perf_counter() - started
    assert len(provider.export_state()) >= 500
    started = time.perf_counter()
    for index in range(10):
        provider.retrieve(f"tok{index * 13} tok{index * 7} sentence", 10)
    query_seconds = time.perf_counter() - started
    assert query_seconds < 5.0, f"10 queries over 500 chunks took {query_seconds:.2f}s"
    assert ingest_seconds < 20.0, f"ingest of 500 chunks took {ingest_seconds:.2f}s"


def test_retrieve_refuses_a_negative_top_k(monkeypatch) -> None:
    provider = _provider(monkeypatch)
    provider.ingest_case([_event("Anything at all.")], _handle())
    with pytest.raises(ValueError, match="top_k"):
        provider.retrieve("anything", -1)
