"""Write overlap queries must describe the written body or explicitly abstain."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
import logging

import numpy as np
import pytest

from exomem import corpus_aware, embeddings, readiness
from exomem import note as note_module

_REAL_GET_MODEL = embeddings.get_model


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
    model = SimpleNamespace(texts_fit=lambda _texts: True)
    monkeypatch.setattr(embeddings, "_MODEL", model)
    monkeypatch.setattr(embeddings, "get_model", lambda: model)
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
    with caplog.at_level(logging.DEBUG, logger=corpus_aware.__name__):
        scores = corpus_aware._best_cosine_per_file(
            vault, title="Private title", body="Private written body."
        )
    assert scores == {}
    assert queries == []
    assert "overlap advisory skipped" in caplog.text
    assert "privatebackenddetail" not in caplog.text
    assert "Private written body" not in caplog.text


def _local_bounded_encoder(*, backend="onnx", limit=512, prefix=""):
    from types import SimpleNamespace

    from tokenizers import Tokenizer, models, pre_tokenizers, processors

    from exomem import embedding_backend

    tokenizer = Tokenizer(models.WordPiece(
        {token: n for n, token in enumerate(
            ["[UNK]", "[CLS]", "[SEP]", "[PAD]", "a", ".", "written", "collections", "compiler"]
        )}, unk_token="[UNK]",
    ))
    tokenizer.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)],
    )
    uncapped = Tokenizer.from_str(tokenizer.to_str())
    tokenizer.enable_truncation(max_length=limit)
    profile = embedding_backend.EncoderProfile(
        model="local", pooling="cls", query_prefix="", passage_prefix=prefix,
        max_seq=limit, pad_token="[PAD]",
    )
    if backend == "onnx":
        model = object.__new__(embedding_backend._OnnxEncoder)
        model._tokenizer = tokenizer
    else:
        model = object.__new__(embedding_backend._TorchEncoder)

        def tokenize(texts, **kwargs):
            assert kwargs["truncation"] is False
            assert kwargs["add_special_tokens"] is True
            return {"input_ids": [uncapped.encode(text).ids for text in texts]}

        model._model = SimpleNamespace(tokenizer=tokenize, max_seq_length=limit)
    model.profile = profile
    retained = []

    def encode(texts, **_kwargs):
        rows = []
        for text in texts:
            ids = tokenizer.encode(prefix + text).ids
            retained.append(ids)
            seed = int.from_bytes(hashlib.sha256(bytes(ids)).digest()[:8], "little")
            row = np.random.default_rng(seed).normal(size=embeddings.VECTOR_DIM)
            rows.append(row / np.linalg.norm(row))
        return np.asarray(rows, dtype=np.float32)

    return model, encode, retained, uncapped


@pytest.mark.parametrize("backend", ["onnx", "torch"])
@pytest.mark.parametrize("published", [False, True])
def test_guard_must_reject_encoder_truncation(
    vault, overlap_corpus, monkeypatch, caplog, backend, published
):
    index, _encoded, queries = overlap_corpus
    model, encode, retained, uncapped = _local_bounded_encoder(backend=backend)
    monkeypatch.setattr(embeddings, "_MODEL", model)
    monkeypatch.setattr(embeddings, "get_model", lambda: model)
    monkeypatch.setattr(embeddings, "embed_texts", encode)
    prefix = ("a." * 150 + " ") * 4
    bodies = [prefix + "collections", prefix + "compiler"]
    assert len(bodies[0].split()) < embeddings.MAX_WORDS_PER_CHUNK
    assert len(uncapped.encode("written\n\n" + bodies[0]).ids) > model.profile.max_seq
    scores = []
    for body in bodies:
        path = "Knowledge Base/Notes/Insights/token-truncated.md"
        if published:
            chunks = embeddings.chunk_text("written", body)
            index.upsert_file(path, chunks, encode(chunks), 1.0)
        scores.append(corpus_aware._best_cosine_per_file(
            vault, title="written", body=body, published_path=path if published else None,
        ))
    assert scores == [{}, {}]
    assert queries == []
    assert "text_truncated" in caplog.text
    if not published:
        assert retained == []


@pytest.mark.parametrize("backend", ["onnx", "torch"])
def test_guard_counts_title_passage_prefix_and_special_tokens(
    vault, overlap_corpus, monkeypatch, caplog, backend
):
    model, encode, _retained, uncapped = _local_bounded_encoder(
        backend=backend, limit=8, prefix="a a ",
    )
    monkeypatch.setattr(embeddings, "_MODEL", model)
    monkeypatch.setattr(embeddings, "get_model", lambda: model)
    monkeypatch.setattr(embeddings, "embed_texts", encode)
    assert len(uncapped.encode("a a written\n\na a a").ids) == 8
    assert corpus_aware._best_cosine_per_file(vault, title="written", body="a a a")
    assert corpus_aware._best_cosine_per_file(vault, title="written", body="a a a a") == {}
    assert "text_truncated" in caplog.text


def test_published_generation_must_not_mix_vector_and_text_snapshots(
    vault, overlap_corpus, monkeypatch
):
    index, _encoded, _queries = overlap_corpus
    rel = "Knowledge Base/Notes/Insights/snapshot-race.md"
    old_row = np.zeros((1, embeddings.VECTOR_DIM), dtype=np.float32)
    new_row = old_row.copy()
    old_row[0, 0] = 1.0
    new_row[0, 1] = 1.0
    index.upsert_file(rel, ["Old written text."], old_row, 1.0)
    read_texts = index._texts_for

    def replace_then_read(pairs):
        index.upsert_file(rel, ["New written text."], new_row, 2.0)
        return read_texts(pairs)

    monkeypatch.setattr(index, "_texts_for", replace_then_read)
    rows = embeddings.published_generation_vectors(vault, rel, chunks=["New written text."])
    assert rows is None or np.array_equal(rows, new_row), (
        "reuse paired a previous vector snapshot with the current chunk text"
    )


def test_warming_skip_returns_status_without_warning_log(vault, overlap_corpus, monkeypatch, caplog):
    monkeypatch.setattr(readiness, "should_defer", lambda *_a: True)
    with caplog.at_level(logging.DEBUG, logger=corpus_aware.__name__):
        result = corpus_aware.write_advisory_for(vault, corpus_aware.WriteAdvisoryInputs(
            route="remember", target_rel_path="probe.md", self_path="probe.md",
            title="Written", body="A short body.", note_type="insight",
        ))
    assert [item.warning for item in result] == ["overlap advisory skipped: embeddings_warming"]
    assert any(record.levelno == logging.DEBUG and "embeddings_warming" in record.message
               for record in caplog.records)
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


@pytest.mark.parametrize("reason", ["disabled", "empty"])
def test_unscheduled_overlap_stays_silent(vault, overlap_corpus, monkeypatch, caplog, reason):
    if reason == "disabled":
        monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    assert corpus_aware._best_cosine_per_file(vault, title="", body="") == {}
    assert "overlap advisory skipped" not in caplog.text


@pytest.mark.parametrize("previous", [False, True])
def test_budget_guard_uses_encoder_execution_slot(monkeypatch, previous):
    from contextlib import contextmanager, nullcontext

    from exomem import recall_space, runtime_resources

    active = []

    @contextmanager
    def execution(*, wait=True):
        assert wait is False
        active.append(True)
        try:
            yield
        finally:
            active.pop()

    def fits(texts):
        assert active, "tokenizer inspection must share the encoder's execution slot"
        return True

    model = SimpleNamespace(texts_fit=fits)
    monkeypatch.setattr(embeddings, "_MODEL", model)
    monkeypatch.setattr(embeddings, "get_model", lambda: model)
    monkeypatch.setattr(runtime_resources, "model_execution", execution)
    monkeypatch.setattr(recall_space, "selected_model", lambda: "previous" if previous else None)
    monkeypatch.setattr(recall_space, "previous_resident", lambda _name: model)
    monkeypatch.setattr(recall_space, "_previous_gate", lambda: SimpleNamespace(
        admission=nullcontext, execution=execution,
    ))
    assert embeddings.advisory_passages_fit(["A short body."])


@pytest.mark.parametrize("backend", ["onnx", "torch"])
def test_budget_guard_uses_selected_previous_encoder(monkeypatch, backend):
    from exomem import recall_space

    model, _encode, _retained, _uncapped = _local_bounded_encoder(
        backend=backend, limit=8, prefix="a a ",
    )
    monkeypatch.setattr(recall_space, "selected_model", lambda: "previous")
    monkeypatch.setattr(recall_space, "previous_resident", lambda name: model if name == "previous" else None)
    monkeypatch.setattr(embeddings, "get_model", lambda: pytest.fail("must use the selected encoder"))
    assert embeddings.advisory_passages_fit(["written\n\na a a"])
    assert not embeddings.advisory_passages_fit(["written\n\na a a a"])


@pytest.mark.parametrize("backend", ["onnx", "torch"])
def test_fully_reused_sweep_takes_no_blocking_slot(
    vault, overlap_corpus, monkeypatch, backend
):
    from contextlib import contextmanager
    from exomem import runtime_resources

    index, _encoded, queries = overlap_corpus
    model, _encode, _retained, _uncapped = _local_bounded_encoder(backend=backend)
    monkeypatch.setattr(embeddings, "_MODEL", model)
    monkeypatch.setattr(embeddings, "get_model", _REAL_GET_MODEL)
    chunks = embeddings.chunk_text("Written", "A short complete body.")
    row = np.zeros((1, index.dim), dtype=np.float32)
    row[0, 0] = 1
    path = "Knowledge Base/Notes/Insights/reused-budget.md"
    index.upsert_file(path, chunks, row, 1.0)
    monkeypatch.setattr(embeddings, "embed_texts", lambda *_a, **_k: pytest.fail("no encode needed"))
    waits = []

    @contextmanager
    def execution(*, wait=True):
        waits.append(wait)
        yield

    monkeypatch.setattr(runtime_resources, "model_execution", execution)
    assert corpus_aware._best_cosine_per_file(
        vault, title="Written", body="A short complete body.", published_path=path, strict=True,
    )
    assert len(queries) == 1
    assert waits == ([] if backend == "onnx" else [False])


def test_reused_sweep_skips_when_encoder_is_not_resident(vault, overlap_corpus, monkeypatch):
    index, _encoded, queries = overlap_corpus
    chunks = embeddings.chunk_text("Written", "A short complete body.")
    row = np.zeros((1, index.dim), dtype=np.float32)
    row[0, 0] = 1
    path = "Knowledge Base/Notes/Insights/cold-budget.md"
    index.upsert_file(path, chunks, row, 1.0)
    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(embeddings, "get_model", lambda: pytest.fail("token proof must never load"))
    with pytest.raises(corpus_aware.OverlapAdvisorySkipped, match="model_warming"):
        corpus_aware._best_cosine_per_file(
            vault, title="Written", body="A short complete body.", published_path=path, strict=True,
        )
    assert queries == []


@pytest.mark.parametrize("previous", [False, True])
def test_budget_guard_skips_busy_torch_slot(monkeypatch, previous):
    from contextlib import nullcontext
    from exomem import recall_space, runtime_resources

    model, _encode, _retained, _uncapped = _local_bounded_encoder(backend="torch")
    monkeypatch.setattr(embeddings, "_MODEL", model)
    monkeypatch.setattr(recall_space, "selected_model", lambda: "previous" if previous else None)
    monkeypatch.setattr(recall_space, "previous_resident", lambda _name: model)

    def busy(*, wait=True):
        assert wait is False
        raise runtime_resources.ModelBusyError("busy")

    monkeypatch.setattr(runtime_resources, "model_execution", busy)
    monkeypatch.setattr(recall_space, "_previous_gate", lambda: SimpleNamespace(admission=nullcontext, execution=busy))
    with pytest.raises(runtime_resources.ModelBusyError):
        embeddings.advisory_passages_fit(["A short body."])


def test_generation_reuse_must_survive_same_width_model_cutover(vault, overlap_corpus, monkeypatch):
    from test_deferred_write_advisory import _seed_page, _fingerprint, _prepare_custody, _run
    from exomem import index_paths, recall_space
    from exomem.embedding_index import EmbeddingIndex

    old_index, _encoded, _queries = overlap_corpus
    target = _seed_page(vault, 'cutover-target', 'A complete unchanged conclusion.')
    from exomem import find as find_module
    page = find_module._CACHE.get(vault / target, vault)
    chunks = embeddings._chunks_for_page(vault, page)
    old_row = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    old_row[0] = 1
    new_row = np.zeros_like(old_row)
    new_row[1] = 1
    old_index.upsert_file(target, chunks, np.tile(old_row, (len(chunks), 1)), 1.0)
    old_identity = old_index.identity
    # The configured new encoder exists before the cutover; the old sidecar
    # keeps serving while its replacement is built, as in recall_migration.
    monkeypatch.setenv(recall_space.RECALL_MODEL_ENV, 'reviewer-next-space')
    monkeypatch.setattr(embeddings, 'MODEL_NAME', 'reviewer-next-space')
    generation = embeddings.prepare_generation_vectors(
        vault, target, expected_fingerprint=_fingerprint(vault, target), allow_encode=False,
    )
    assert generation is not None and generation.reused
    new_index = EmbeddingIndex(vault, path=old_index.path.with_name(index_paths.space_sidecar_name('reviewer-next-space')))
    counterpart = 'Knowledge Base/Notes/Insights/harbor.md'
    new_index.upsert_file(counterpart, ['harbor'], [old_row], 2.0)
    new_index.upsert_file(target, chunks, np.tile(new_row, (len(chunks), 1)), 2.0)
    _prepare_custody(vault, batch_id='reviewer-model-cutover', target_rel=target)
    prepare = embeddings.prepare_generation_vectors

    def prepare_then_cutover(*args, **kwargs):
        result = prepare(*args, **kwargs)
        assert result is not None and result.reused
        index_paths.publish_active_sidecar(vault, new_index.path.name)
        return result

    monkeypatch.setattr(embeddings, 'prepare_generation_vectors', prepare_then_cutover)
    execution = _run(vault)[0]
    active = embeddings.get_embedding_index(vault)
    assert active.identity.model == 'reviewer-next-space'
    assert old_identity != active.identity and old_identity.dim == active.identity.dim
    mixed = corpus_aware.best_cosine_per_file_for_vectors(vault, generation.vectors, self_path=target, strict=True)
    correct = corpus_aware.best_cosine_per_file_for_vectors(vault, np.tile(new_row, (len(chunks), 1)), self_path=target, strict=True)
    assert mixed[counterpart] > 0.9
    assert correct[counterpart] == 0.0
    assert execution.candidate_count == 0 or execution.state == 'failed', (
        'the deferred sweep publishes a near-duplicate from vectors of the retired model space'
    )


def test_generation_space_and_vectors_share_one_read_snapshot(vault, overlap_corpus, monkeypatch):
    from test_deferred_write_advisory import _seed_page, _fingerprint
    from exomem import find as find_module, recall_space
    from exomem.embedding_index import EmbeddingIndex

    index, _encoded, _queries = overlap_corpus
    target = _seed_page(vault, "space-snapshot", "A complete unchanged conclusion.")
    chunks = embeddings._chunks_for_page(vault, find_module._CACHE.get(vault / target, vault))
    old_row = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    old_row[0] = 1
    new_row = np.zeros_like(old_row)
    new_row[1] = 1
    index.upsert_file(target, chunks, np.tile(old_row, (len(chunks), 1)), 1.0)
    old_space = index.identity
    new_space = recall_space.SpaceIdentity("next-space", None, index.dim)
    other = EmbeddingIndex(vault, path=index.path)
    connect = index._connect
    switched = []

    class Cursor:
        def __init__(self, inner):
            self.inner = inner

        def fetchall(self):
            rows = self.inner.fetchall()
            if not switched:
                switched.append(True)
                conn = other._connect()
                try:
                    with conn:
                        recall_space.write_identity(conn, new_space)
                        conn.execute("UPDATE chunks SET vector = ? WHERE file_path = ?", (new_row.tobytes(), target))
                finally:
                    conn.close()
            return rows

    class Connection:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *args):
            cursor = self.inner.execute(sql, *args)
            return Cursor(cursor) if sql.startswith("SELECT key, value FROM meta") else cursor

        def close(self):
            self.inner.close()

    monkeypatch.setattr(index, "_connect", lambda: Connection(connect()))
    generation = embeddings.prepare_generation_vectors(
        vault, target, expected_fingerprint=_fingerprint(vault, target), allow_encode=False,
    )
    assert generation is not None and generation.reused
    assert switched
    assert generation.space == old_space
    assert np.array_equal(generation.vectors, np.tile(old_row, (len(chunks), 1)))
    current = embeddings.prepare_generation_vectors(
        vault, target, expected_fingerprint=_fingerprint(vault, target), allow_encode=False,
    )
    assert current is not None and current.space == new_space
    assert np.array_equal(current.vectors, np.tile(new_row, (len(chunks), 1)))



def test_bound_empty_source_space_refuses_a_different_serving_model(vault, overlap_corpus, monkeypatch):
    from exomem import index_paths, recall_space
    from exomem.embedding_index import EmbeddingIndex

    old_index, _encoded, _queries = overlap_corpus
    new_index = EmbeddingIndex(vault, path=old_index.path.with_name(index_paths.space_sidecar_name("next-space")))
    row = np.zeros((1, embeddings.VECTOR_DIM), dtype=np.float32)
    row[0, 0] = 1
    with recall_space.selecting("next-space"):
        new_index.upsert_file("Knowledge Base/Notes/Insights/harbor.md", ["harbor"], row, 1.0)
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: new_index)
    with pytest.raises(corpus_aware.OverlapAdvisorySkipped, match="vector_space_mismatch"):
        corpus_aware.best_cosine_per_file_for_vectors(vault, row, encoded_for=None, strict=True)
