"""Bounded embedding publication preserves coherent serving snapshots."""

import sqlite3

import numpy as np
import pytest

from exomem import embeddings, recall_space, sidecar_store
from exomem.embedding_index import CHUNK_PATH_LOG, EmbeddingIndex


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "Knowledge Base").mkdir()
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    monkeypatch.setattr(embeddings, "_MODEL", None)
    embeddings.clear_embedding_indexes()
    return tmp_path


def vectors(n, value=1, dim=8):
    return np.full((n, dim), value, dtype=np.float32)


def page(root, name="one"):
    path = root / "Knowledge Base" / f"{name}.md"
    path.write_text("---\ntype: note\n---\n# Note\ncontent\n", encoding="utf-8")
    return path


def token(index):
    conn = index._connect()
    try:
        return (
            *sidecar_store.read_meta_token(conn),
            conn.execute(
                "SELECT value FROM meta WHERE key = 'semantic_unit_generation'"
            ).fetchone(),
        )
    finally:
        conn.close()


def test_matrix_load_does_not_stack_blob_arrays(root, monkeypatch):
    index = EmbeddingIndex(root)
    index.upsert_file("one.md", ["one"], vectors(1), 1)
    monkeypatch.setattr(np, "stack", lambda *_args, **_kwargs: pytest.fail("whole-matrix stack"))
    loaded = index._load_all_rows()
    assert loaded.matrix.shape == (1, 8)
    np.testing.assert_array_equal(loaded.matrix, vectors(1))


def test_matrix_rejects_blob_width_different_from_snapshot_identity(root):
    index = EmbeddingIndex(root)
    index.upsert_file("one.md", ["one"], vectors(1), 1)
    conn = index._connect()
    with conn:
        conn.execute("UPDATE chunks SET vector = ?", (vectors(1, dim=4).tobytes(),))
    conn.close()
    with pytest.raises(ValueError, match="width"):
        index._load_all_rows()
    assert index._cache is None


def test_batch_replaces_parents_once_without_splicing_retained_matrix(root, monkeypatch):
    index = EmbeddingIndex(root)
    index.upsert_file("one.md", ["old"], vectors(1), 1)
    _, old = index.all_vectors()
    before = token(index)
    monkeypatch.setattr(index, "_patch_cache", lambda *_args: pytest.fail("per-file splice"))
    index.upsert_batch(
        [("one.md", ["new"], vectors(1, 2), 2), ("two.md", ["two"], vectors(1, 3), 2)],
        identity=recall_space.current_identity(8),
    )
    after = token(index)
    assert after[1] == before[1] + 1
    assert index._cache is None
    np.testing.assert_array_equal(old, vectors(1))
    assert index.all_vectors()[1].shape == (2, 8)


def test_batch_same_width_different_model_refuses_inside_transaction(root):
    index = EmbeddingIndex(root)
    identity = recall_space.current_identity(8)
    index.upsert_file("one.md", ["old"], vectors(1), 1)
    before = token(index)
    with pytest.raises(recall_space.VectorSpaceMismatch):
        index.upsert_batch(
            [("one.md", ["new"], vectors(1, 2), 2)],
            identity=recall_space.SpaceIdentity("different-model", None, 8),
        )
    assert token(index) == before
    assert index.identity == identity
    np.testing.assert_array_equal(index.all_vectors()[1], vectors(1))


def test_rebuild_encodes_in_bounded_groups_and_keeps_serving_during_staging(root, monkeypatch):
    for i in range(5):
        page(root, str(i))
    index = EmbeddingIndex(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    before = token(index)
    calls = []
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["chunk"])

    def encode(texts, **_kwargs):
        calls.append(len(texts))
        assert token(index) == before
        assert EmbeddingIndex(root).all_vectors()[0] == [("old.md", 0)]
        return vectors(len(texts), 2)

    monkeypatch.setattr(embeddings, "embed_texts", encode)
    assert index.rebuild_all(batch_size=2) == 5
    assert calls == [2, 2, 1]
    assert index.all_vectors()[1].shape == (5, 8)


@pytest.mark.parametrize("mutation", ["chunk", "unit", "builder"])
def test_rebuild_refuses_intervening_serving_publication(root, monkeypatch, mutation):
    page(root)
    index = EmbeddingIndex(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["chunk"])

    def encode(texts, **_kwargs):
        monkeypatch.setattr(embeddings, "embed_texts", lambda texts, **_kw: vectors(len(texts), 3))
        writer = EmbeddingIndex(root)
        if mutation == "chunk":
            writer.upsert_file("winner.md", ["winner"], vectors(1, 3), 2)
        elif mutation == "unit":
            writer.delete_semantic_units("absent.md")
        else:
            assert writer.rebuild_all(batch_size=2) == 1
        return vectors(len(texts), 2)

    monkeypatch.setattr(embeddings, "embed_texts", encode)
    assert index.rebuild_all(batch_size=2) == 0
    metadata, matrix = index.all_vectors()
    assert (
        ("old.md", 0) in metadata
        if mutation != "builder"
        else metadata == [("Knowledge Base/one.md", 0)]
    )
    if mutation == "builder":
        np.testing.assert_array_equal(matrix, vectors(1, 3))


def test_incremental_flush_publishes_one_batch_without_splices(root, monkeypatch):
    paths = [page(root, str(i)) for i in range(3)]
    index = embeddings.get_embedding_index(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    index.all_vectors()
    before = token(index)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["chunk"])
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts, **_kwargs: vectors(len(texts), 2))
    monkeypatch.setattr(
        index, "_patch_cache", lambda *_args: pytest.fail("incremental per-file splice")
    )
    result = embeddings.index_incremental(root, batch_size=8)
    assert result["chunks_embedded"] == len(paths)
    # Pruning has its own publication; the three replacements share the next one.
    assert token(index)[1] == before[1] + 2


def test_deferred_replay_retains_all_captured_receipts_after_later_batch_failure(root, monkeypatch):
    from exomem import deferred_index, index_sync, readiness

    paths = [page(root, str(i)) for i in range(3)]
    deferred_index.add(root, [path.relative_to(root).as_posix() for path in paths])
    captured = deferred_index.snapshot(root)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(readiness, "defer", lambda *_args: False)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["chunk"])
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts, **_kwargs: vectors(len(texts), 2))
    index = embeddings.get_embedding_index(root)
    publish = index.upsert_batch
    calls = 0

    def fail_later(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("injected second batch failure")
        return publish(*args, **kwargs)

    monkeypatch.setattr(index, "upsert_batch", fail_later)
    real_upsert = embeddings.upsert_after_write_status
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda *args, **kwargs: real_upsert(*args, **kwargs, batch_size=1),
    )
    result = index_sync.replay_deferred_embedding(root, paths, captured)
    assert result.status == "degraded"
    assert calls == 3
    assert deferred_index.snapshot(root) == captured
    assert len(index.all_vectors()[0]) == 2


@pytest.mark.parametrize("backend", ["numpy", "sqlite-vec"])
@pytest.mark.parametrize("failure", ["identity", "mirror", "swap"])
def test_rebuild_publication_failure_rolls_back_rows_identity_tokens_and_mirror(
    root, monkeypatch, backend, failure
):
    from exomem import vecstore

    monkeypatch.setenv("EXOMEM_VEC_BACKEND", backend)
    vecstore.reset_load_memo()
    index = EmbeddingIndex(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    _, retained = index.all_vectors()
    before = token(index)
    original_identity = index.identity
    page(root)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["chunk"])
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda texts, **_kwargs: vectors(len(texts), 2, dim=12)
    )
    if failure == "identity":
        original = recall_space.write_identity

        def failing(conn, identity):
            original(conn, identity)
            raise sqlite3.OperationalError("after identity")

        monkeypatch.setattr(recall_space, "write_identity", failing)
    elif failure == "mirror":
        original = index._publication_vec

        def failing(conn, identity):
            original(conn, identity)
            raise sqlite3.OperationalError("after mirror")

        monkeypatch.setattr(index, "_publication_vec", failing)
    else:
        original = sidecar_store.bump_generation_for_reset

        def failing(conn, *args):
            original(conn, *args)
            raise sqlite3.OperationalError("after swap")

        monkeypatch.setattr(sidecar_store, "bump_generation_for_reset", failing)
    with pytest.raises(sqlite3.OperationalError, match="after"):
        index.rebuild_all(batch_size=2)
    assert token(index) == before
    assert index.identity == original_identity
    np.testing.assert_array_equal(retained, vectors(1))
    assert EmbeddingIndex(root).all_vectors()[0] == [("old.md", 0)]
    np.testing.assert_array_equal(index.all_vectors()[1], vectors(1))
    conn = index._connect()
    assert conn.execute("SELECT COUNT(*) FROM embedding_build_runs").fetchone()[0] == 0
    if backend == "sqlite-vec":
        assert index._vec.try_load(conn)
        assert (
            "float[8]"
            in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'vec_chunks'").fetchone()[0]
        )
        assert conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 1
    conn.close()


def test_batch_failure_after_mirror_and_token_rolls_back_entire_parent_group(root, monkeypatch):
    from exomem import vecstore

    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "sqlite-vec")
    vecstore.reset_load_memo()
    index = EmbeddingIndex(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    before = token(index)
    original = sidecar_store.bump_generation_for_paths

    def fail(conn, *args):
        original(conn, *args)
        assert conn.in_transaction
        assert not index._lock._is_owned()
        assert EmbeddingIndex(root).all_vectors()[0] == [("old.md", 0)]
        raise sqlite3.OperationalError("after batch mirror")

    monkeypatch.setattr(sidecar_store, "bump_generation_for_paths", fail)
    with pytest.raises(sqlite3.OperationalError):
        index.upsert_batch(
            [("old.md", ["new"], vectors(1, 2), 2), ("new.md", ["new"], vectors(1, 3), 2)],
            identity=recall_space.current_identity(8),
        )
    assert token(index) == before
    np.testing.assert_array_equal(index.all_vectors()[1], vectors(1))
    conn = index._connect()
    assert index._vec.try_load(conn)
    assert conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 1
    conn.close()


def test_batch_invalidation_preserves_a_reader_at_committed_or_later_token(root, monkeypatch):
    index = EmbeddingIndex(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    index.all_vectors()
    invalidate = index._invalidate_before
    loaded = []

    def delayed(own_token):
        # Another reader loads after commit while this writer awaits the cache lock.
        loaded.append(index.all_vectors()[1])
        invalidate(own_token)
        assert index._cache.matrix is loaded[-1]
        index.upsert_batch(
            [("later.md", ["later"], vectors(1, 3), 3)], identity=recall_space.current_identity(8)
        )
        later = index.all_vectors()[1]
        invalidate(own_token)
        assert index._cache.matrix is later

    monkeypatch.setattr(index, "_invalidate_before", lambda own_token: invalidate(own_token))

    # Install the delayed hook for just the outer publication.
    def once(own_token):
        monkeypatch.setattr(index, "_invalidate_before", invalidate)
        delayed(own_token)

    monkeypatch.setattr(index, "_invalidate_before", once)
    index.upsert_batch(
        [("old.md", ["new"], vectors(1, 2), 2)], identity=recall_space.current_identity(8)
    )
    assert index.all_vectors()[1].shape == (2, 8)


def test_snapshot_uses_stored_width_read_after_begin(root, monkeypatch):
    index = EmbeddingIndex(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    connect = index._connect

    class Connection:
        def __init__(self):
            self.conn = connect()

        def execute(self, sql, *args):
            if sql == "BEGIN":
                writer = connect()
                with writer:
                    recall_space.write_identity(writer, recall_space.current_identity(12))
                    writer.execute("UPDATE chunks SET vector = ?", (vectors(1, 2, 12).tobytes(),))
                    sidecar_store.bump_generation_for_reset(writer, CHUNK_PATH_LOG)
                writer.close()
            return self.conn.execute(sql, *args)

        def rollback(self):
            self.conn.rollback()

        def close(self):
            self.conn.close()

    monkeypatch.setattr(index, "_connect", Connection)
    loaded = index._load_all_rows()
    assert loaded.matrix.shape == (1, 12)
    np.testing.assert_array_equal(loaded.matrix, vectors(1, 2, 12))


def test_empty_rebuild_is_a_noop_without_creating_sidecar(root):
    index = EmbeddingIndex(root)
    assert not index.path.exists()
    assert index.rebuild_all() == 0
    assert not index.path.exists()


def test_rebuild_encoding_failure_cleans_only_own_run_and_keeps_old_generation(root, monkeypatch):
    index = EmbeddingIndex(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    for i in range(3):
        page(root, str(i))
    before = token(index)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["chunk"])
    calls = 0

    def encode(texts, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            conn = index._connect()
            with conn:
                conn.execute(
                    "INSERT INTO embedding_build_runs(run_key, serving_token) VALUES ('other-run', '[]')"
                )
                conn.execute(
                    "INSERT INTO embedding_build_chunks VALUES ('other-run', 'other.md', 0, 'other', ?, 1)",
                    (vectors(1).tobytes(),),
                )
            conn.close()
            return vectors(len(texts), 2)
        raise RuntimeError("encoding failed after staging")

    monkeypatch.setattr(embeddings, "embed_texts", encode)
    with pytest.raises(RuntimeError, match="after staging"):
        index.rebuild_all(batch_size=1)
    assert token(index) == before
    np.testing.assert_array_equal(index.all_vectors()[1], vectors(1))
    conn = index._connect()
    assert conn.execute("SELECT run_key FROM embedding_build_runs").fetchall() == [("other-run",)]
    assert conn.execute("SELECT run_key FROM embedding_build_chunks").fetchall() == [("other-run",)]
    conn.close()


def test_rebuild_source_drift_refuses_publication(root, monkeypatch):
    path = page(root)
    index = EmbeddingIndex(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    before = token(index)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["chunk"])

    def encode(texts, **_kwargs):
        path.write_text("# directly edited\n", encoding="utf-8")
        return vectors(len(texts), 2)

    monkeypatch.setattr(embeddings, "embed_texts", encode)
    assert index.rebuild_all(batch_size=1) == 0
    assert token(index) == before
    assert index.all_vectors()[0] == [("old.md", 0)]


def test_rebuild_refuses_changed_producer_across_encoding_batches(root, monkeypatch):
    for i in range(2):
        page(root, str(i))
    index = EmbeddingIndex(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    before = token(index)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["chunk"])
    calls = 0

    def encode(texts, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            monkeypatch.setattr(embeddings, "MODEL_NAME", "another-same-width-model")
        return vectors(len(texts), 2)

    monkeypatch.setattr(embeddings, "embed_texts", encode)
    with pytest.raises(recall_space.VectorSpaceMismatch, match="producer changed"):
        index.rebuild_all(batch_size=1)
    assert token(index) == before
    assert index.all_vectors()[0] == [("old.md", 0)]


def test_prepared_vectors_carry_actual_encoder_identity_after_process_model_changes(
    root, monkeypatch
):
    from exomem import embedding_backend

    model_name = embeddings.MODEL_NAME
    profile = embedding_backend.EncoderProfile(
        model=model_name,
        revision="test",
        artifact_digest="test",
        pooling="cls",
        max_seq=512,
        pad_token="[PAD]",
        query_prefix="",
        passage_prefix="",
    )

    class Encoder:
        device = "cpu"

        def encode(self, texts, **_kwargs):
            monkeypatch.setattr(embeddings, "MODEL_NAME", "other-model")
            monkeypatch.setattr(embeddings, "_MODEL", None)
            return vectors(len(texts), 2)

    encoder = Encoder()
    encoder.profile = profile
    monkeypatch.setattr(embeddings, "get_model", lambda: encoder)
    result, produced = embeddings._encode_prepared(lambda: embeddings.embed_texts(["chunk"]))
    assert produced == recall_space.SpaceIdentity(model_name, profile.fingerprint(), 8)
    np.testing.assert_array_equal(result, vectors(1, 2))


def test_live_batch_accepts_legacy_reused_units_with_new_profiled_chunks(root, monkeypatch):
    from exomem import embedding_backend, readiness, semantic_index

    path = root / "Knowledge Base/units.md"
    path.write_text(
        "---\ntype: insight\nexomem_id: 22222222-2222-4222-8222-222222222222\n---\n# Note\n- [config_rule] Keep rows ^unit\n",
        encoding="utf-8",
    )
    index = embeddings.get_embedding_index(root)
    state = semantic_index.build_parent_index_state(root, path)
    assert len([unit for unit in state.document.units if unit.unit_ref]) == 1
    index.upsert_file(state.path, ["old"], vectors(1), 1)
    index.upsert_semantic_units(state, vectors(1), 1)
    profile = embedding_backend.EncoderProfile(
        model=embeddings.MODEL_NAME,
        pooling="cls",
        max_seq=512,
        pad_token="[PAD]",
        query_prefix="",
        passage_prefix="",
        artifact_digest="test",
    )

    class Encoder:
        device = "cpu"

        def encode(self, texts, **_kwargs):
            return vectors(len(texts), 2)

    encoder = Encoder()
    encoder.profile = profile
    monkeypatch.setattr(embeddings, "get_model", lambda: encoder)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["old", "new"])
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(readiness, "defer", lambda *_args: False)
    result = embeddings.upsert_after_write_status(root, [path])
    assert result.status == "completed"
    assert index.identity.fingerprint == profile.fingerprint()


def test_batch_invalidation_releases_old_mask_when_reader_already_loaded_new_matrix(
    root, monkeypatch
):
    index = EmbeddingIndex(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    index.search(vectors(1)[0], 1, allowed_paths={"old.md"})
    old_mask = index._mask_cache
    assert old_mask is not None
    invalidate = index._invalidate_before

    def delayed(own_token):
        current = index.all_vectors()[1]
        assert index._mask_cache is old_mask
        invalidate(own_token)
        assert index._cache.matrix is current
        assert index._mask_cache is None

    monkeypatch.setattr(index, "_invalidate_before", delayed)
    index.upsert_batch(
        [("new.md", ["new"], vectors(1, 2), 2)], identity=recall_space.current_identity(8)
    )


@pytest.mark.parametrize("publication", ["batch", "rebuild"])
@pytest.mark.parametrize("warm", [False, True, None])
def test_late_publication_keeps_newer_serving_identity_even_with_cold_matrix(
    root, monkeypatch, publication, warm
):
    path = page(root)
    index = EmbeddingIndex(root)
    space_a = recall_space.current_identity(8)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    if warm is None:
        index.all_vectors()
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["chunk"])
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts, **_kwargs: vectors(len(texts), 2))
    connect = index._connect
    delayed = False
    retained = []

    def newer_publication():
        monkeypatch.setattr(embeddings, "MODEL_NAME", "newer-same-width-model")
        monkeypatch.setattr(
            embeddings, "embed_texts", lambda texts, **_kwargs: vectors(len(texts), 3)
        )
        newer = EmbeddingIndex(root)
        assert newer.rebuild_all(batch_size=1) == 1
        if warm:
            retained.append(index.all_vectors()[1])
        else:
            index.file_mtimes()  # observes the newer identity without loading its matrix
            if warm is False:
                assert index._cache is None

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __enter__(self):
            return self.conn.__enter__()

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        def close(self):
            self.conn.close()
            if publication == "batch":
                newer_publication()

        def commit(self):
            self.conn.commit()
            if publication == "rebuild" and self.publishing:
                newer_publication()

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                self.publishing = True
            return self.conn.execute(sql, *args)

        publishing = False

    def first_connection():
        nonlocal delayed
        conn = connect()
        if delayed:
            return conn
        delayed = True
        return Connection(conn)

    monkeypatch.setattr(index, "_connect", first_connection)
    if publication == "batch":
        index.upsert_batch(
            [(path.relative_to(root).as_posix(), ["chunk"], vectors(1, 2), 2)], identity=space_a
        )
    else:
        assert index.rebuild_all(batch_size=1) == 1
    assert index.identity == recall_space.SpaceIdentity("newer-same-width-model", None, 8)
    with pytest.raises(recall_space.VectorSpaceMismatch):
        recall_space.require_same_space(index, space_a, vectors(1)[0])
    if warm:
        assert index._cache.matrix is retained[0]
        np.testing.assert_array_equal(retained[0], vectors(1, 3))
    else:
        assert index._cache is None


def test_previous_encode_and_identity_use_one_pinned_encoder(root, monkeypatch):
    from exomem import embedding_backend

    previous_model = "previous-model"

    class Encoder:
        device = "cpu"

        def __init__(self, digest, value):
            self.profile = embedding_backend.EncoderProfile(
                model=previous_model,
                pooling="cls",
                max_seq=512,
                pad_token="[PAD]",
                query_prefix="query:",
                passage_prefix="passage:",
                artifact_digest=digest,
            )
            self.value = value
            self.calls = []

        def encode(self, texts, **_kwargs):
            self.calls.append(texts)
            return vectors(len(texts), self.value)

    encoder_a = Encoder("a", 1)
    encoder_b = Encoder("b", 2)
    calls = 0

    def resident(model):
        nonlocal calls
        assert model == previous_model
        calls += 1
        return encoder_a if calls == 1 else encoder_b

    monkeypatch.setattr(recall_space, "previous_resident", resident)
    with recall_space.selecting(previous_model):
        result, identity = embeddings._encode_prepared(lambda: embeddings.embed_texts(["chunk"]))
    assert calls == 1
    assert encoder_a.calls == [["passage:chunk"]]
    assert encoder_b.calls == []
    np.testing.assert_array_equal(result, vectors(1, 1))
    assert identity.fingerprint == encoder_a.profile.fingerprint()


def test_late_unit_publication_cannot_undo_newer_identity_at_same_chunk_token(root, monkeypatch):
    from exomem import semantic_index

    path = root / "Knowledge Base/units.md"
    path.write_text(
        "---\ntype: insight\nexomem_id: 22222222-2222-4222-8222-222222222222\n---\n# Note\n- [config_rule] Keep rows ^unit\n",
        encoding="utf-8",
    )
    state = semantic_index.build_parent_index_state(root, path)
    index = EmbeddingIndex(root)
    index.upsert_file("old.md", ["old"], vectors(1), 1)
    initial = token(index)
    legacy = index.identity
    newer_identity = recall_space.SpaceIdentity(legacy.model, "new-profile", 8)
    connect = index._connect
    intercepted = False

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def close(self):
            self.conn.close()
            writer = EmbeddingIndex(root)
            writer.upsert_batch([], [(state, vectors(1, 3), 3)], identity=newer_identity)
            index.file_mtimes()
            assert index._cache is None

    def first():
        nonlocal intercepted
        conn = connect()
        if intercepted:
            return conn
        intercepted = True
        return Connection(conn)

    monkeypatch.setattr(index, "_connect", first)
    index.upsert_batch([], [(state, vectors(1, 2), 2)], identity=legacy)
    assert index.identity == newer_identity
    assert token(index)[:3] == initial[:3]
    with pytest.raises(recall_space.VectorSpaceMismatch):
        recall_space.require_same_space(index, legacy, vectors(1)[0])
