"""Write overlap queries must describe the written body or explicitly abstain."""

from __future__ import annotations

import hashlib
import logging

import numpy as np
import pytest

from exomem import corpus_aware, embeddings, readiness
from exomem import note as note_module


@pytest.fixture
def overlap_corpus(vault, monkeypatch):
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(readiness, "should_defer", lambda *_a: False)
    embeddings.clear_passage_vectors()
    encoded = []

    def encode(texts, *, is_query=False):
        assert is_query is False
        encoded.extend(texts)
        rows = []
        for text in texts:
            seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "little")
            row = np.random.default_rng(seed).standard_normal(embeddings.VECTOR_DIM)
            rows.append(row / np.linalg.norm(row))
        return np.asarray(rows, dtype=np.float32)

    monkeypatch.setattr(embeddings, "embed_texts", encode)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    index = embeddings.get_embedding_index(vault)
    for name, body in (("harbor", "Harbor tides and navigation."),
                       ("garden", "Garden irrigation and harvest.")):
        rel = f"Knowledge Base/Notes/Insights/{name}.md"
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
        chunks = embeddings.chunk_text(name, body)
        index.upsert_file(rel, chunks, encode(chunks), 1.0)
    encoded.clear()
    queries = []
    search = index.search_many

    def record(vectors, *args, **kwargs):
        queries.append(np.array(vectors, copy=True))
        return search(vectors, *args, **kwargs)

    monkeypatch.setattr(index, "search_many", record)
    yield index, encoded, queries
    embeddings.clear_passage_vectors()


@pytest.mark.parametrize("published", [False, True])
def test_overlap_distinct_written_bodies_have_distinct_vectors_and_scores(
    vault, overlap_corpus, published
):
    index, encoded, queries = overlap_corpus
    texts = ["SQLite is authoritative for structured collections.",
             "Compiler currency follows authored relations."]
    scores = []
    for number, text in enumerate(texts):
        path = f"Knowledge Base/Notes/Insights/written-{number}.md"
        if published:
            chunks = embeddings.chunk_text("Written conclusion", text)
            index.upsert_file(path, chunks, embeddings.embed_texts(chunks), 1.0)
        scores.append(corpus_aware._best_cosine_per_file(
            vault, title="Written conclusion", body=text, published_path=path
        ))
    assert len(queries) == 2
    assert not np.array_equal(queries[0], queries[1])
    corpus_paths = [f"Knowledge Base/Notes/Insights/{name}.md" for name in ("harbor", "garden")]
    assert [scores[0][path] for path in corpus_paths] != [scores[1][path] for path in corpus_paths]
    assert all(text in "\n".join(encoded) for text in texts)


def test_overlap_truncated_bodies_cannot_score_the_same_retained_prefix(
    vault, overlap_corpus, caplog
):
    _index, _encoded, queries = overlap_corpus
    prefix = "shared " * embeddings.MAX_WORDS_PER_CHUNK
    scores = []
    with caplog.at_level(logging.WARNING, logger=corpus_aware.__name__):
        for tail in ("SQLite collections are authoritative.", "Compiler currency follows relations."):
            scores.append(corpus_aware._best_cosine_per_file(
                vault, title="Written conclusion", body=prefix + tail
            ))
    if queries:
        assert len(queries) == 2
        assert not np.array_equal(queries[0], queries[1]), (
            "different written tails must not produce identical retained-prefix queries"
        )
        return
    assert scores == [{}, {}]
    assert "overlap advisory skipped" in caplog.text
    assert "text_truncated" in caplog.text


def test_overlap_long_body_split_without_truncation_keeps_distinct_tails(
    vault, overlap_corpus
):
    _index, encoded, queries = overlap_corpus
    prefix = "字" * (embeddings.MAX_UNSPACED_CHARS_PER_CHUNK * 2)
    tails = ("Collections are authoritative.", "Relations determine compiler currency.")
    for tail in tails:
        corpus_aware._best_cosine_per_file(
            vault, title="Written conclusion", body=prefix + tail
        )
    assert len(queries) == 2
    assert not np.array_equal(queries[0], queries[1])
    assert all(tail in "\n".join(encoded) for tail in tails)


@pytest.mark.parametrize("sync_pending", [False, True])
def test_overlap_queries_follow_two_committed_remember_bodies(
    vault, overlap_corpus, monkeypatch, sync_pending
):
    _index, _encoded, queries = overlap_corpus
    if sync_pending:
        monkeypatch.setattr(
            embeddings, "upsert_after_write_status",
            lambda _root, paths, **_k: embeddings.EmbeddingSyncStatus(
                "degraded", "embedding_upsert_failed", len(paths)
            ),
        )
    for title, body in (
        ("Authoritative collections", "SQLite is authoritative for structured collections."),
        ("Compiler currency", "Compiler currency follows authored relations."),
    ):
        result = note_module.note(
            vault, content=body, note_type="insight", title=title, status="draft"
        )
        assert (vault / result.path).is_file()
    assert len(queries) == 2
    assert not np.array_equal(queries[0], queries[1])


def test_overlap_invalid_published_vector_is_not_used_as_query(
    vault, overlap_corpus, caplog
):
    index, encoded, queries = overlap_corpus
    body = "Written body whose derived vector is invalid."
    path = "Knowledge Base/Notes/Insights/invalid-published.md"
    chunks = embeddings.chunk_text("Written conclusion", body)
    index.upsert_file(path, chunks, np.zeros((1, embeddings.VECTOR_DIM), dtype=np.float32), 1.0)
    with caplog.at_level(logging.WARNING, logger=corpus_aware.__name__):
        assert corpus_aware._best_cosine_per_file(
            vault, title="Written conclusion", body=body, published_path=path
        ) == {}
    assert encoded == []
    assert queries == []
    assert "overlap advisory skipped: invalid_vectors" in caplog.text


def test_overlap_wrong_width_rows_cannot_be_reshaped_into_a_query(
    vault, overlap_corpus, monkeypatch, caplog
):
    index, _encoded, queries = overlap_corpus
    monkeypatch.setattr(
        embeddings, "embed_texts",
        lambda texts, **_k: np.full(
            (len(texts), index.dim // 2), 1.0 / np.sqrt(index.dim // 2), dtype=np.float32
        ),
    )
    with caplog.at_level(logging.WARNING, logger=corpus_aware.__name__):
        assert corpus_aware._best_cosine_per_file(
            vault, title="Written conclusion", body="First written paragraph.\n\nSecond paragraph."
        ) == {}
    assert queries == []
    assert "invalid_vectors" in caplog.text


def test_inline_remember_reports_skipped_overlap_in_returned_warnings(
    vault, overlap_corpus, monkeypatch
):
    monkeypatch.setattr(readiness, "should_defer", lambda *_a: True)
    result = note_module.note(
        vault, content="Authored conclusion about collections.",
        note_type="insight", title="Skipped overlap", status="draft",
    )
    assert (vault / result.path).is_file()
    assert any("overlap advisory skipped" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "title", ["shared " * 600, "字" * 600, "x" * 600],
    ids=["spaced", "unspaced", "single-word"],
)
def test_overlap_long_title_cannot_hide_different_written_bodies(vault, overlap_corpus, title):
    _index, _encoded, queries = overlap_corpus
    for body in ("Collections are authoritative.", "Relations determine compiler currency."):
        assert corpus_aware._best_cosine_per_file(
            vault, title=title, body=body
        ) == {}
    assert queries == []


@pytest.mark.parametrize("failure", ["warming", "import", "backend", "zero", "nan", "empty", "overflow", "nonunit"])
def test_overlap_unavailable_vector_skips_without_scores(
    vault, overlap_corpus, monkeypatch, caplog, failure
):
    _index, _encoded, queries = overlap_corpus
    if failure == "warming":
        monkeypatch.setattr(readiness, "should_defer", lambda *_a: True)
    else:
        def unavailable(*_a, **_k):
            if failure == "import":
                raise ImportError("privatebackenddetail")
            if failure == "backend":
                raise RuntimeError("privatebackenddetail")
            if failure == "empty":
                return np.empty((0, embeddings.VECTOR_DIM), dtype=np.float32)
            value = {"nan": np.nan, "overflow": 1e30, "nonunit": 1.0}.get(failure, 0.0)
            return np.full((1, embeddings.VECTOR_DIM), value, dtype=np.float32)
        monkeypatch.setattr(embeddings, "embed_texts", unavailable)
    with caplog.at_level(logging.WARNING, logger=corpus_aware.__name__):
        scores = corpus_aware._best_cosine_per_file(
            vault, title="Private title", body="Private written body."
        )
    assert scores == {}
    assert queries == []
    assert "overlap advisory skipped" in caplog.text
    assert "privatebackenddetail" not in caplog.text
    assert "Private written body" not in caplog.text
