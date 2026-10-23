"""Selected passages keep their scored publication without hydrating the pool."""

from __future__ import annotations

import numpy as np
import pytest
from test_embedding_matrix_cache import _make_legacy_sidecar
from test_find_transcript_match import _FakeImg, _write_video_sidecar

from exomem import cloud_cell, embedding_index, embeddings, recall_space, scene_frames, vecstore
from exomem import find as find_module
from exomem.governance.principal import library_scope


def _vector(score: float) -> np.ndarray:
    vector = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    vector[0] = score
    return vector


@pytest.mark.parametrize("disk", [False, True])
def test_unrelated_publication_keeps_selected_text_without_reading_other_chunks(
    tmp_path,
    monkeypatch,
    disk,
) -> None:
    """A write to another page must neither force eager reads nor lose a passage."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    monkeypatch.setattr(cloud_cell, "resource_policy", lambda: "service-v1" if disk else "legacy")
    index = embeddings.EmbeddingIndex(tmp_path)
    index.upsert_file("a.md", ["winner", "unused"], np.stack([_vector(1), _vector(0.8)]), 1)
    index.upsert_file("b.md", ["other"], np.stack([_vector(0.5)]), 1)
    original_texts = index._texts_for
    reads = []

    def texts(pairs, **kwargs):
        reads.extend(pairs)
        return original_texts(pairs, **kwargs)

    monkeypatch.setattr(index, "_texts_for", texts)
    hits = index.select_chunks(_vector(1), 3)
    assert reads == []
    ref = hits[0][2]
    assert isinstance(ref, embedding_index.ChunkRef)
    embeddings.EmbeddingIndex(tmp_path).upsert_file(
        "b.md", ["updated"], np.stack([_vector(0.4)]), 2
    )

    assert index._texts_for([(ref.path, ref.chunk)], proof=ref.proof) == {("a.md", 0): "winner"}
    assert reads == [("a.md", 0)]


def test_changed_selected_path_is_missing_but_present_empty_text_is_not(
    tmp_path, monkeypatch
) -> None:
    """A current replacement cannot be mistaken for the passage that was scored."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    index = embeddings.EmbeddingIndex(tmp_path)
    index.upsert_file("a.md", [""], np.stack([_vector(1)]), 1)
    ref = index.select_chunks(_vector(1), 1)[0][2]
    assert isinstance(ref, embedding_index.ChunkRef)
    assert index._texts_for([("a.md", 0)], proof=ref.proof) == {("a.md", 0): ""}

    embeddings.EmbeddingIndex(tmp_path).upsert_file(
        "a.md", ["replacement"], np.stack([_vector(0.5)]), 2
    )

    assert index._texts_for([("a.md", 0)], proof=ref.proof) == {}


def test_cache_swap_cannot_relabel_the_matrix_that_was_scored(tmp_path, monkeypatch) -> None:
    """A concurrent publication must not pair old scores with its replacement text."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    index = embeddings.EmbeddingIndex(tmp_path)
    index.upsert_file("a.md", ["original"], np.stack([_vector(1)]), 1)
    snapshot = index._all_vector_snapshot

    def swapped():
        scored = snapshot()
        embeddings.EmbeddingIndex(tmp_path).upsert_file(
            "a.md", ["replacement"], np.stack([_vector(0.25)]), 2
        )
        index._cache = index._load_all_rows()
        return scored

    monkeypatch.setattr(index, "_all_vector_snapshot", swapped)
    hit = index.select_chunks(_vector(1), 1)[0]
    ref = hit[2]
    assert isinstance(ref, embedding_index.ChunkRef)
    assert hit[3] == pytest.approx(1)
    assert index._texts_for([("a.md", 0)], proof=ref.proof) == {}


def test_proof_and_text_share_one_read_snapshot(tmp_path, monkeypatch) -> None:
    """Publication after the proof cannot swap text before the point lookup."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    index = embeddings.EmbeddingIndex(tmp_path)
    index.upsert_file("a.md", ["original"], np.stack([_vector(1)]), 1)
    ref = index.select_chunks(_vector(1), 1)[0][2]
    external = embeddings.EmbeddingIndex(tmp_path)
    connect = index._connect
    published = False
    completed = []

    def connected():
        conn = connect()

        def before_text(sql):
            nonlocal published
            if sql.startswith("SELECT file_path, chunk_idx, chunk_text") and not published:
                published = True
                external.upsert_file("a.md", ["replacement"], np.stack([_vector(0.5)]), 2)
                completed.append(True)

        conn.set_trace_callback(before_text)
        return conn

    monkeypatch.setattr(index, "_connect", connected)

    assert index._texts_for([("a.md", 0)], proof=ref.proof) == {("a.md", 0): "original"}
    assert published
    assert completed == [True]
    assert external.stored_chunks_for("a.md")[0] == ["replacement"]


def test_vec_gate_commits_before_the_scoring_snapshot(tmp_path, monkeypatch) -> None:
    """A mirror synchronization commit must not end the KNN proof's snapshot."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    index = embeddings.EmbeddingIndex(tmp_path)
    index.upsert_file("a.md", ["original"], np.stack([_vector(1)]), 1)
    observed = []

    def gate(_index, conn):
        conn.commit()
        return True

    class Mirror:
        dim = embeddings.VECTOR_DIM

        def knn(self, conn, query, k, *, quant):
            observed.append(conn.in_transaction)
            return [(conn.execute("SELECT rowid FROM chunks").fetchone()[0], 1.0)]

    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "sqlite-vec")
    monkeypatch.setattr(vecstore, "load_failed", lambda: False)
    monkeypatch.setattr(embedding_index, "vec_gate", gate)
    index._vec = Mirror()

    ref = index.select_chunks(_vector(1), 1)[0][2]

    assert observed == [True]
    assert isinstance(ref, embedding_index.ChunkRef)
    assert index._texts_for([("a.md", 0)], proof=ref.proof) == {("a.md", 0): "original"}


def test_generation_zero_keeps_the_existing_eager_numpy_ties(tmp_path, monkeypatch) -> None:
    """Legacy rows cannot silently switch scorers while their proof is absent."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    index = embeddings.EmbeddingIndex(tmp_path)
    _make_legacy_sidecar(index.path, [(f"p{number}.md", 0, [1, 0], 1) for number in range(12)])

    expected = index.search(_vector(1), 5)
    actual = index.select_chunks(_vector(1), 5)

    assert actual == expected
    assert all(isinstance(text, str) for _path, _chunk, text, _score in actual)


@pytest.mark.parametrize("seeded", [False, True])
def test_first_producer_or_refined_fingerprint_cannot_strand_the_dense_lane(
    tmp_path,
    monkeypatch,
    seeded,
) -> None:
    """A warm matrix must recover when its space changes during publication."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    index = embeddings.EmbeddingIndex(tmp_path)
    index._connect().close()
    vector = np.zeros(recall_space.current_dim(), dtype=np.float32)
    vector[0] = 1
    if seeded:
        index.upsert_file("old.md", ["old"], np.stack([vector * 0.1]), 1)
    index.all_vectors()
    if seeded:
        monkeypatch.setattr(recall_space, "resident_fingerprint", lambda _model: "profile-v2")
    index.upsert_file("winner.md", ["winner"], np.stack([vector]), 2)

    ref = index.select_chunks(vector, 1, encoded_for=index.identity)[0][2]

    assert isinstance(ref, embedding_index.ChunkRef)
    assert ref.path == "winner.md"
    assert index._texts_for([(ref.path, ref.chunk)], proof=ref.proof) == {
        ("winner.md", 0): "winner"
    }


def test_late_background_load_cannot_install_the_space_before_refinement(
    tmp_path, monkeypatch
) -> None:
    """An old off-lock load must not make a current token hide an obsolete space."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    index = embeddings.EmbeddingIndex(tmp_path)
    index.upsert_file("a.md", ["passage"], np.stack([_vector(1)]), 1)
    old = index._load_all_rows()
    monkeypatch.setattr(recall_space, "resident_fingerprint", lambda _model: "profile-v2")
    # The existing writer records a first fingerprint before its row transaction.
    # A failed later write can leave that refinement at the same chunk token.
    conn = index._connect()
    try:
        index._admit(conn, embeddings.VECTOR_DIM)
    finally:
        conn.close()
    load = index._load_all_rows
    pending = [old]
    monkeypatch.setattr(index, "_load_all_rows", lambda: pending.pop() if pending else load())

    index._load_corpus_matrix()
    ref = index.select_chunks(_vector(1), 1, encoded_for=index.identity)[0][2]

    assert isinstance(ref, embedding_index.ChunkRef)
    assert index._texts_for([("a.md", 0)], proof=ref.proof) == {("a.md", 0): "passage"}


@library_scope()
@pytest.mark.parametrize("rerank", [False, True])
def test_find_hydrates_only_the_selected_prefix_with_identical_passages(
    vault,
    warm_managed_cell,
    monkeypatch,
    rerank,
) -> None:
    """Returning two hits must not fetch every scored chunk's passage."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    index = embeddings.get_embedding_index(vault)
    for number in range(12):
        path = vault / f"Knowledge Base/Notes/dense-{number:02d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = f"dense probe passage {number}"
        path.write_text(f"---\ntype: insight\n---\n\n{body}\n", encoding="utf-8")
        index.upsert_file(
            path.relative_to(vault).as_posix(),
            [body, "unused passage"],
            np.stack([_vector(1 - number / 100), _vector(0.1)]),
            1,
        )
    warm_managed_cell(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    monkeypatch.setattr(embeddings, "embed_texts", lambda *_args, **_kwargs: np.stack([_vector(1)]))
    index.all_vectors()
    passages = []

    def score(_query, texts):
        passages.append(list(texts))
        return [float(len(texts) - number) for number in range(len(texts))]

    monkeypatch.setattr(embeddings, "rerank_pairs", score)
    kwargs = dict(
        query="dense probe",
        mode="vector",
        scope="kb-only",
        limit=2,
        graph=False,
        temporal=False,
        rerank=rerank,
    )
    with monkeypatch.context() as oracle:
        oracle.setattr(index, "select_chunks", index.search)
        expected = find_module.find(vault, **kwargs)
    expected_passages = list(passages)
    passages.clear()
    find_module.clear_cache()
    original_texts = index._texts_for
    read_pairs = []

    def texts(pairs, **options):
        read_pairs.extend(pairs)
        return original_texts(pairs, **options)

    monkeypatch.setattr(index, "_texts_for", texts)
    actual = find_module.find(vault, **kwargs)

    assert [hit.as_dict() for hit in actual] == [hit.as_dict() for hit in expected]
    assert passages == expected_passages
    assert len(read_pairs) == (6 if rerank else 2)
    assert all(chunk == 0 for _path, chunk in read_pairs)


@library_scope()
def test_find_does_not_refill_or_cache_after_selected_chunk_proof_loss(
    vault,
    warm_managed_cell,
    monkeypatch,
) -> None:
    """A vanished top passage cannot cause a lower fused page to fill its slot."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    index = embeddings.get_embedding_index(vault)
    for number in range(2):
        page = vault / f"Knowledge Base/Notes/selected-{number}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\ntype: insight\n---\n\ndense probe\n", encoding="utf-8")
        index.upsert_file(
            page.relative_to(vault).as_posix(),
            ["dense probe"],
            np.stack([_vector(1 - number / 10)]),
            1,
        )
    warm_managed_cell(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    monkeypatch.setattr(embeddings, "embed_texts", lambda *_args, **_kwargs: np.stack([_vector(1)]))
    index.all_vectors()
    find_module.clear_cache()
    original_texts = index._texts_for
    reads = []

    def changed(pairs, **options):
        reads.extend(pairs)
        index.upsert_file(pairs[0][0], ["replacement"], np.stack([_vector(0.5)]), 2)
        return original_texts(pairs, **options)

    monkeypatch.setattr(index, "_texts_for", changed)
    degraded = []
    hits = find_module.find(
        vault,
        query="dense probe",
        scope="kb-only",
        mode="vector",
        limit=1,
        graph=False,
        temporal=False,
        rerank=False,
        degraded_out=degraded,
    )

    assert hits == []
    assert len(reads) == 1
    assert "recall_projection" in degraded
    assert not find_module._FIND_CACHE


@library_scope()
@pytest.mark.parametrize("frame_source", [False, True])
def test_selected_transcript_and_frame_keep_passages_and_source_cache_guards(
    vault,
    warm_managed_cell,
    monkeypatch,
    frame_source,
) -> None:
    """Chunk localization and a collapsed child's cached evidence stay bound."""
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    video = _write_video_sidecar(vault)
    frames = scene_frames.write_scene_frames(
        vault,
        video,
        [(embeddings.Scene(3000, 3200, 3100, 0.7), _FakeImg())],
    )
    parent = video.relative_to(vault).as_posix() + ".md"
    frame = frames[0][1].relative_to(vault).as_posix()
    source = frame if frame_source else parent
    index = embeddings.get_embedding_index(vault)
    passage = "[51:20] a dense semantic passage"
    index.upsert_file(source, [passage], np.stack([_vector(1)]), 1)
    warm_managed_cell(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    monkeypatch.setattr(embeddings, "embed_texts", lambda *_args, **_kwargs: np.stack([_vector(1)]))
    index.all_vectors()
    find_module.clear_cache()
    kwargs = dict(
        query="dense semantic",
        mode="vector",
        scope="kb-only",
        limit=1,
        graph=False,
        temporal=False,
        rerank=False,
    )
    with monkeypatch.context() as oracle:
        oracle.setattr(index, "select_chunks", index.search)
        expected = find_module.find(vault, **kwargs)
    find_module.clear_cache()

    actual = find_module.find(vault, **kwargs)

    assert len(actual) == 1
    assert [hit.as_dict() for hit in actual] == [hit.as_dict() for hit in expected]
    assert actual[0].path == parent
    assert actual[0].transcript_ts == 3080
    assert actual[0].scene_frame_ts == 3100
    assert {parent, frame} <= actual[0].evidence_sources.keys()
    assert "evidence_sources" not in actual[0].as_dict()
    assert find_module._FIND_CACHE
    changed = vault / frame
    changed.write_text(
        changed.read_text(encoding="utf-8") + "\nchanged evidence\n", encoding="utf-8"
    )

    assert find_module.find(vault, **kwargs) == []
