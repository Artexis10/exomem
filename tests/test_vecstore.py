"""vec0 backend: schema/sync/migration, dual-write lockstep, KNN parity, quantized rescore.

Everything here runs with RANDOM normalized vectors through the low-level index APIs
(`upsert_file`, `ClipIndex.upsert*`) — no torch, no model download. The one model seam
(`rebuild_all`) is monkeypatched to deterministic random vectors, because what is under
test is the dual-write wiring, not the embedding.

Requires sqlite-vec (embeddings extra); skips cleanly on lean installs — the c112b5a rule:
the marker alone is not enough, the lean CI matrix collects every module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

sqlite_vec = pytest.importorskip("sqlite_vec")

from exomem import embeddings as embeddings_module  # noqa: E402
from exomem import vecstore  # noqa: E402
from exomem.embeddings import CLIP_DIM, VECTOR_DIM, ClipIndex, EmbeddingIndex  # noqa: E402

# ---------------------------------------------------------------- helpers


def _unit_rows(rng: np.random.Generator, n: int, dim: int) -> np.ndarray:
    """(n, dim) float32, L2-normalized — the shape embed_texts produces."""
    m = rng.standard_normal((n, dim)).astype(np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    return m


def _vec_conn(path: Path) -> sqlite3.Connection:
    """A raw connection with the extension loaded, for asserting on vec tables."""
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _build_text_index(
    root: Path, rng: np.random.Generator, files: int = 8, chunks_per_file: int = 4
) -> tuple[EmbeddingIndex, dict[str, np.ndarray]]:
    """Populate a fresh EmbeddingIndex under `root` with random vectors."""
    idx = EmbeddingIndex(root)
    vecs_by_file: dict[str, np.ndarray] = {}
    for f in range(files):
        rel = f"Knowledge Base/Notes/note-{f:03d}.md"
        vecs = _unit_rows(rng, chunks_per_file, VECTOR_DIM)
        chunks = [f"chunk {f}-{i} text" for i in range(chunks_per_file)]
        idx.upsert_file(rel, chunks, vecs, mtime=1000.0 + f)
        vecs_by_file[rel] = vecs
    return idx, vecs_by_file


@pytest.fixture(autouse=True)
def _fresh_vec_state(monkeypatch: pytest.MonkeyPatch):
    """Every test starts from a clean process-global load memo and default env."""
    vecstore.reset_load_memo()
    monkeypatch.delenv("EXOMEM_VEC_BACKEND", raising=False)
    monkeypatch.delenv("EXOMEM_VEC_QUANT", raising=False)
    yield
    vecstore.reset_load_memo()


# (env-reader tests live in tests/test_vec_backend_fallback.py — they must run lean)

# ---------------------------------------------------------------- schema + sync


def test_writes_keep_vec_table_in_lockstep(tmp_path):
    rng = np.random.default_rng(7)
    idx, _ = _build_text_index(tmp_path, rng, files=4, chunks_per_file=3)
    conn = _vec_conn(idx.path)
    try:
        assert _count(conn, "chunks") == 12
        assert _count(conn, "vec_chunks") == 12
        # Replace one file with a DIFFERENT chunk count.
        idx.upsert_file(
            "Knowledge Base/Notes/note-001.md",
            ["a", "b", "c", "d", "e"],
            _unit_rows(rng, 5, VECTOR_DIM),
            mtime=2000.0,
        )
        assert _count(conn, "chunks") == 14
        assert _count(conn, "vec_chunks") == 14
        idx.delete_file("Knowledge Base/Notes/note-000.md")
        assert _count(conn, "chunks") == 11
        assert _count(conn, "vec_chunks") == 11
    finally:
        conn.close()


def test_schema_creation_is_idempotent(tmp_path):
    rng = np.random.default_rng(8)
    idx, _ = _build_text_index(tmp_path, rng, files=2, chunks_per_file=2)
    # Re-open and write again: CREATE IF NOT EXISTS + sync must not duplicate rows.
    idx2 = EmbeddingIndex(tmp_path)
    idx2.upsert_file(
        "Knowledge Base/Notes/extra.md", ["x"], _unit_rows(rng, 1, VECTOR_DIM), 3000.0
    )
    conn = _vec_conn(idx.path)
    try:
        assert _count(conn, "chunks") == 5
        assert _count(conn, "vec_chunks") == 5
    finally:
        conn.close()


def test_legacy_blobs_only_sidecar_is_backfilled_on_first_use(tmp_path, monkeypatch):
    """A sidecar written with the kill switch on (blob rows only) gains vec rows —
    with correct search results — the first time a vec-aware instance touches it."""
    rng = np.random.default_rng(9)
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    legacy, vecs_by_file = _build_text_index(tmp_path, rng, files=5, chunks_per_file=4)
    plain = sqlite3.connect(legacy.path)
    try:
        assert not _table_exists(plain, "vec_chunks")
    finally:
        plain.close()

    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "auto")
    idx = EmbeddingIndex(tmp_path)
    query = vecs_by_file["Knowledge Base/Notes/note-002.md"][1]
    hits = idx.search(query, k=3)
    assert hits[0][0] == "Knowledge Base/Notes/note-002.md"
    assert hits[0][1] == 1
    assert hits[0][3] == pytest.approx(1.0, abs=1e-5)
    conn = _vec_conn(idx.path)
    try:
        assert _count(conn, "vec_chunks") == _count(conn, "chunks") == 20
    finally:
        conn.close()


def test_vec_row_drift_self_heals(tmp_path):
    """Manually broken vec rows (count mismatch) are rebuilt from blobs by the next
    fresh instance's sync check."""
    rng = np.random.default_rng(10)
    idx, vecs_by_file = _build_text_index(tmp_path, rng, files=3, chunks_per_file=4)
    conn = _vec_conn(idx.path)
    try:
        conn.execute("DELETE FROM vec_chunks WHERE rowid IN (SELECT rowid FROM vec_chunks LIMIT 5)")
        conn.commit()
        assert _count(conn, "vec_chunks") == 7
    finally:
        conn.close()

    healed = EmbeddingIndex(tmp_path)
    query = vecs_by_file["Knowledge Base/Notes/note-000.md"][0]
    hits = healed.search(query, k=1)
    assert hits[0][0] == "Knowledge Base/Notes/note-000.md"
    conn = _vec_conn(idx.path)
    try:
        assert _count(conn, "vec_chunks") == _count(conn, "chunks") == 12
    finally:
        conn.close()


def test_search_on_missing_sidecar_returns_empty_without_creating_it(tmp_path):
    idx = EmbeddingIndex(tmp_path)
    rng = np.random.default_rng(11)
    assert idx.search(_unit_rows(rng, 1, VECTOR_DIM)[0], k=5) == []
    assert not idx.path.exists()


def test_rebuild_all_repopulates_vec_table(tmp_path, monkeypatch):
    """rebuild_all wipes and repopulates BOTH table families (model monkeypatched)."""
    kb = tmp_path / "Knowledge Base" / "Notes"
    kb.mkdir(parents=True)
    for i in range(3):
        (kb / f"page-{i}.md").write_text(
            f"---\ntype: insight\ntitle: Page {i}\n---\n\n# Page {i}\n\nBody text {i}.\n",
            encoding="utf-8",
        )
    rng = np.random.default_rng(12)

    def _fake_embed(texts, is_query=False):
        return _unit_rows(rng, len(texts), VECTOR_DIM)

    monkeypatch.setattr(embeddings_module, "embed_texts", _fake_embed)
    idx = EmbeddingIndex(tmp_path)
    total = idx.rebuild_all()
    assert total > 0
    conn = _vec_conn(idx.path)
    try:
        assert _count(conn, "vec_chunks") == _count(conn, "chunks") == total
    finally:
        conn.close()


# ---------------------------------------------------------------- f32 parity


def test_f32_search_parity_with_numpy(tmp_path, monkeypatch):
    """The vec0 full-precision backend is EXACT: same ordered (path, chunk) top-k and
    matching scores as the numpy scan, over the same sidecar."""
    rng = np.random.default_rng(13)
    idx, _ = _build_text_index(tmp_path, rng, files=25, chunks_per_file=20)  # 500 rows
    queries = _unit_rows(rng, 20, VECTOR_DIM)

    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "sqlite-vec")
    vec_idx = EmbeddingIndex(tmp_path)
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    np_idx = EmbeddingIndex(tmp_path)

    for q in queries:
        monkeypatch.setenv("EXOMEM_VEC_BACKEND", "sqlite-vec")
        vec_hits = vec_idx.search(q, k=10)
        monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
        np_hits = np_idx.search(q, k=10)
        assert [(h[0], h[1]) for h in vec_hits] == [(h[0], h[1]) for h in np_hits]
        assert [h[2] for h in vec_hits] == [h[2] for h in np_hits]  # chunk_text joined back
        np.testing.assert_allclose(
            [h[3] for h in vec_hits], [h[3] for h in np_hits], atol=1e-5
        )


def test_clip_f32_search_parity_with_numpy(tmp_path, monkeypatch):
    rng = np.random.default_rng(14)
    idx = ClipIndex(tmp_path)
    for i in range(30):
        idx.upsert(f"Sources/img-{i:03d}.png", _unit_rows(rng, 1, CLIP_DIM)[0], 100.0 + i)
    idx.upsert_frames(
        "Sources/video.mp4",
        [(float(ts), _unit_rows(rng, 1, CLIP_DIM)[0]) for ts in (1.0, 2.5, 9.0)],
        200.0,
    )
    queries = _unit_rows(rng, 10, CLIP_DIM)

    for q in queries:
        monkeypatch.setenv("EXOMEM_VEC_BACKEND", "sqlite-vec")
        vec_hits = ClipIndex(tmp_path).search(q, k=8)
        monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
        np_hits = ClipIndex(tmp_path).search(q, k=8)
        assert [(h[0], h[1]) for h in vec_hits] == [(h[0], h[1]) for h in np_hits]
        np.testing.assert_allclose(
            [h[2] for h in vec_hits], [h[2] for h in np_hits], atol=1e-5
        )


# ---------------------------------------------------------------- CLIP lockstep


def test_clip_writes_keep_vec_table_in_lockstep(tmp_path):
    rng = np.random.default_rng(15)
    idx = ClipIndex(tmp_path)
    idx.upsert("Sources/a.png", _unit_rows(rng, 1, CLIP_DIM)[0], 1.0)
    idx.upsert_frames(
        "Sources/v.mp4",
        [(float(ts), _unit_rows(rng, 1, CLIP_DIM)[0]) for ts in (0.0, 4.0)],
        2.0,
    )
    conn = _vec_conn(idx.path)
    try:
        assert _count(conn, "images") == 3
        assert _count(conn, "vec_images") == 3
        idx.delete("Sources/v.mp4")
        assert _count(conn, "images") == 1
        assert _count(conn, "vec_images") == 1
    finally:
        conn.close()


def test_clip_image_only_replace_keeps_frame_rows(tmp_path):
    """upsert() replaces ONLY the NULL-frame_ts row; a file's keyframe rows survive in
    both table families (the frame_ts IS NULL predicate must reach the vec delete)."""
    rng = np.random.default_rng(16)
    idx = ClipIndex(tmp_path)
    idx.upsert_frames(
        "Sources/clip.mp4",
        [(float(ts), _unit_rows(rng, 1, CLIP_DIM)[0]) for ts in (0.0, 3.0, 6.0)],
        1.0,
    )
    # A legacy-style whole-file poster row alongside the frames:
    idx.upsert("Sources/clip.mp4", _unit_rows(rng, 1, CLIP_DIM)[0], 2.0)
    idx.upsert("Sources/clip.mp4", _unit_rows(rng, 1, CLIP_DIM)[0], 3.0)  # replace poster
    conn = _vec_conn(idx.path)
    try:
        assert _count(conn, "images") == 4  # 3 frames + 1 poster
        assert _count(conn, "vec_images") == 4
        frames = conn.execute(
            "SELECT count(*) FROM images WHERE file_path = ? AND frame_ts IS NOT NULL",
            ("Sources/clip.mp4",),
        ).fetchone()[0]
        assert frames == 3
    finally:
        conn.close()


# ---------------------------------------------------------------- binary quantized mode


def test_binary_mode_recovers_planted_neighbors_with_exact_scores(tmp_path, monkeypatch):
    """Clustered corpus: quantized search must surface the planted near-duplicates in
    top-k, and the returned scores must be full-precision cosine, not Hamming."""
    rng = np.random.default_rng(17)
    idx = EmbeddingIndex(tmp_path)
    anchor = _unit_rows(rng, 1, VECTOR_DIM)[0]
    # 5 planted near-neighbors of the anchor...
    near = anchor + 0.05 * _unit_rows(rng, 5, VECTOR_DIM)
    near /= np.linalg.norm(near, axis=1, keepdims=True)
    idx.upsert_file("Knowledge Base/near.md", [f"n{i}" for i in range(5)], near, 1.0)
    # ...drowned in 300 random rows.
    for f in range(15):
        idx.upsert_file(
            f"Knowledge Base/noise-{f:02d}.md",
            [f"x{i}" for i in range(20)],
            _unit_rows(rng, 20, VECTOR_DIM),
            2.0 + f,
        )

    monkeypatch.setenv("EXOMEM_VEC_QUANT", "binary")
    quant_idx = EmbeddingIndex(tmp_path)
    hits = quant_idx.search(anchor, k=5)
    assert {h[0] for h in hits} == {"Knowledge Base/near.md"}
    # Scores are exact cosine (rescored from f32 blobs):
    expected = sorted((float(near[i] @ anchor) for i in range(5)), reverse=True)
    np.testing.assert_allclose([h[3] for h in hits], expected, atol=1e-5)
    # The bin table was synthesized from blobs on first quantized use:
    conn = _vec_conn(idx.path)
    try:
        assert _count(conn, "vec_chunks_bin") == _count(conn, "chunks")
    finally:
        conn.close()


def test_quant_off_never_touches_bin_tables(tmp_path):
    rng = np.random.default_rng(18)
    idx, _ = _build_text_index(tmp_path, rng, files=2, chunks_per_file=2)
    idx.search(_unit_rows(rng, 1, VECTOR_DIM)[0], k=2)
    conn = _vec_conn(idx.path)
    try:
        assert not _table_exists(conn, "vec_chunks_bin")
    finally:
        conn.close()


def test_knn_k_above_vec0_cap_is_clamped_not_fatal(tmp_path, monkeypatch):
    """vec0 hard-caps KNN k at 4096 and ERRORS above it. find() legitimately asks
    for k up to ~4000 (CLIP candidate over-fetch), and binary mode multiplies by 8 —
    an unclamped k would trip the failure ladder and silently retire the backend.
    Both modes must clamp and answer."""
    rng = np.random.default_rng(24)
    idx, vecs_by_file = _build_text_index(tmp_path, rng, files=3, chunks_per_file=2)
    query = vecs_by_file["Knowledge Base/Notes/note-001.md"][0]

    hits = idx.search(query, k=5000)  # > 4096: f32 MATCH must clamp
    assert len(hits) == 6  # every row, still served by vec (no fallback)
    assert hits[0][0] == "Knowledge Base/Notes/note-001.md"
    assert not idx._vec_failed

    monkeypatch.setenv("EXOMEM_VEC_QUANT", "binary")
    quant_idx = EmbeddingIndex(tmp_path)
    hits = quant_idx.search(query, k=1000)  # 1000*8 > 4096: over-fetch must clamp
    assert len(hits) == 6
    assert not quant_idx._vec_failed


def test_vec_backend_leaves_matrix_cold(tmp_path):
    """When vec0 serves search, the numpy matrix is never loaded — not holding
    the (N, dim) matrix resident in Python is the backend's memory win."""
    rng = np.random.default_rng(23)
    idx, vecs_by_file = _build_text_index(tmp_path, rng, files=4, chunks_per_file=3)
    idx._cache = None  # writes splice the cache; start cold like a fresh process
    hits = idx.search(vecs_by_file["Knowledge Base/Notes/note-003.md"][2], k=3)
    assert hits[0][0] == "Knowledge Base/Notes/note-003.md"
    assert idx._cache is None  # served by vec0; all_vectors() never ran


# ---------------------------------------------------------------- failure ladder


def test_extension_load_failure_memoizes_and_falls_back(tmp_path, monkeypatch):
    """A failed extension load: search still answers (numpy), no vec tables appear,
    and the failure is not retried per call (process-global memo)."""
    rng = np.random.default_rng(19)
    calls = {"n": 0}

    def _boom(conn):
        calls["n"] += 1
        raise sqlite3.OperationalError("extension loading disabled")

    monkeypatch.setattr(sqlite_vec, "load", _boom)
    vecstore.reset_load_memo()
    idx, vecs_by_file = _build_text_index(tmp_path, rng, files=3, chunks_per_file=2)
    query = vecs_by_file["Knowledge Base/Notes/note-001.md"][0]
    hits = idx.search(query, k=1)
    assert hits[0][0] == "Knowledge Base/Notes/note-001.md"
    assert calls["n"] == 1  # memoized after the first failure
    plain = sqlite3.connect(idx.path)
    try:
        assert not _table_exists(plain, "vec_chunks")
    finally:
        plain.close()


def test_runtime_knn_failure_falls_back_for_the_process(tmp_path, monkeypatch):
    """A vec KNN that raises at runtime: that call returns numpy results and later
    searches on the instance stop attempting the vec backend."""
    rng = np.random.default_rng(20)
    idx, vecs_by_file = _build_text_index(tmp_path, rng, files=3, chunks_per_file=2)

    knn_calls = {"n": 0}
    real_knn = vecstore.SqliteVecStore.knn

    def _flaky(self, conn, query_vec, k, quant=False):
        knn_calls["n"] += 1
        raise sqlite3.OperationalError("simulated vec failure")

    monkeypatch.setattr(vecstore.SqliteVecStore, "knn", _flaky)
    query = vecs_by_file["Knowledge Base/Notes/note-002.md"][1]
    hits = idx.search(query, k=1)
    assert hits[0][0] == "Knowledge Base/Notes/note-002.md"
    idx.search(query, k=1)
    assert knn_calls["n"] == 1  # second search did not re-attempt vec

    monkeypatch.setattr(vecstore.SqliteVecStore, "knn", real_knn)
