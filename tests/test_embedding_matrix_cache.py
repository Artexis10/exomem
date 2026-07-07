"""Process-lifetime, incremental embedding-matrix cache.

OpenSpec: incremental-embedding-matrix-cache (capability find-recall-efficiency).

These lock the behavior that decouples find latency from sidecar write churn:
the shared per-vault index loads the matrix ONCE and reuses it across calls, and
an in-process write patches the in-memory matrix in place instead of forcing a
full O(vault) reload. Assertions count genuine full reloads (`_load_all_rows`),
never wall-clock — deterministic in CI. All fabricated vectors: no torch/model.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import numpy as np
import pytest

from exomem import embeddings


@pytest.fixture(autouse=True)
def _clean_memo() -> None:
    """Each test starts with an empty shared-index memo."""
    embeddings.clear_embedding_indexes()
    yield
    embeddings.clear_embedding_indexes()


def _fresh_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    return vault


def _pad(vals: list[float]) -> np.ndarray:
    out = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    out[: len(vals)] = vals
    return out


def _mat(*rows: list[float]) -> np.ndarray:
    return np.stack([_pad(r) for r in rows], axis=0)


def _cpad(vals: list[float]) -> np.ndarray:
    out = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
    out[: len(vals)] = vals
    return out


def _count_loads(monkeypatch: pytest.MonkeyPatch, idx) -> dict[str, int]:
    """Wrap idx._load_all_rows to count genuine full reloads."""
    calls = {"n": 0}
    orig = idx._load_all_rows

    def wrapped():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(idx, "_load_all_rows", wrapped)
    return calls


# --------------------------------------------------------------------------- #
# EmbeddingIndex (bge text matrix)
# --------------------------------------------------------------------------- #


def test_matrix_loads_once_and_is_reused(tmp_path, monkeypatch):
    """A quiescent vault loads the matrix once, then every find reuses it."""
    vault = _fresh_vault(tmp_path)
    seed = embeddings.EmbeddingIndex(vault)
    seed.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    seed.upsert_file("b.md", ["b"], _mat([0, 1]), 2.0)

    idx = embeddings.get_embedding_index(vault)
    assert idx is embeddings.get_embedding_index(vault)  # shared instance
    count = _count_loads(monkeypatch, idx)

    metadata = matrix = None
    for _ in range(5):
        metadata, matrix = idx.all_vectors()

    assert count["n"] == 1  # loaded once, not per call
    assert matrix.shape[0] == 2
    assert [m[0] for m in metadata] == ["a.md", "b.md"]


def test_index_cache_status_and_unload_are_non_destructive(tmp_path):
    vault = _fresh_vault(tmp_path)
    emb = embeddings.get_embedding_index(vault)
    clip = embeddings.get_clip_index(vault)

    cold = embeddings.index_cache_status()
    assert cold["embedding"]["indexes"] == 1
    assert cold["embedding"]["loaded"] == 0
    assert cold["clip"]["indexes"] == 1
    assert cold["clip"]["loaded"] == 0

    emb.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    clip.upsert("a.png", _cpad([1, 0]), 1.0)
    emb_meta, emb_matrix = emb.all_vectors()
    clip_paths, clip_ts, clip_matrix = clip.all_vectors()
    assert emb_meta == [("a.md", 0)]
    assert clip_paths == ["a.png"] and clip_ts == [None]

    warm = embeddings.index_cache_status()
    assert warm["embedding"]["loaded"] == 1
    assert warm["embedding"]["rows"] == 1
    assert warm["embedding"]["bytes"] == emb_matrix.nbytes
    assert warm["clip"]["loaded"] == 1
    assert warm["clip"]["rows"] == 1
    assert warm["clip"]["bytes"] == clip_matrix.nbytes

    assert embeddings.unload_index_caches() == {"embedding": 1, "clip": 1}
    assert embeddings.index_cache_status()["embedding"]["loaded"] == 0
    assert embeddings.index_cache_status()["clip"]["loaded"] == 0

    # The sidecars remain intact and lazily reload on next use.
    assert emb.all_vectors()[0] == [("a.md", 0)]
    assert clip.all_vectors()[0] == ["a.png"]


def test_in_process_writes_never_force_reload(tmp_path, monkeypatch):
    """Warm cache + a stream of in-process writes → reads reload zero times.

    This is the core write-churn fix: an upsert/delete through the shared index
    patches the in-memory matrix, so a concurrent find pays no O(vault) reload.
    """
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)

    # New file (2 chunks) → spliced in.
    idx.upsert_file("b.md", ["b1", "b2"], _mat([0, 1], [1, 1]), 2.0)
    metadata, matrix = idx.all_vectors()
    assert matrix.shape[0] == 3
    assert [m[0] for m in metadata] == ["a.md", "b.md", "b.md"]

    # Grow a.md to 3 chunks, then shrink to 1 — block length tracks.
    idx.upsert_file("a.md", ["a1", "a2", "a3"], _mat([1, 0], [2, 0], [3, 0]), 3.0)
    assert idx.all_vectors()[1].shape[0] == 5
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 4.0)
    assert idx.all_vectors()[1].shape[0] == 3

    # Delete b.md → its rows vanish, a.md intact.
    idx.delete_file("b.md")
    metadata, matrix = idx.all_vectors()
    assert [m[0] for m in metadata] == ["a.md"]
    assert matrix.shape[0] == 1

    assert count["n"] == 0  # not one full reload across all of it


def test_spliced_matrix_is_searchable_and_correct(tmp_path):
    """After an in-place splice, search returns the freshly-written file."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm
    idx.upsert_file("b.md", ["b"], _mat([0, 1]), 2.0)

    hits = idx.search(_pad([0, 1]), k=1)
    assert hits[0][0] == "b.md"
    hits = idx.search(_pad([1, 0]), k=1)
    assert hits[0][0] == "a.md"


def test_delete_to_empty_keeps_zero_row_shape(tmp_path):
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm
    idx.delete_file("a.md")
    metadata, matrix = idx.all_vectors()
    assert metadata == []
    assert matrix.shape == (0, embeddings.VECTOR_DIM)
    assert idx.search(_pad([1, 0]), k=3) == []


def test_incremental_matches_full_reload(tmp_path, monkeypatch):
    """The spliced cache is byte-identical to a from-scratch reload."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm
    idx.upsert_file("c.md", ["c"], _mat([0, 0, 1]), 3.0)
    idx.upsert_file("b.md", ["b1", "b2"], _mat([0, 1], [1, 1]), 2.0)
    spliced_meta, spliced_matrix = idx.all_vectors()

    # Force a genuine full reload and compare.
    with idx._lock:
        idx._cache = None
    reload_meta, reload_matrix = idx.all_vectors()

    assert spliced_meta == reload_meta  # same order (sorted by file_path)
    assert np.array_equal(spliced_matrix, reload_matrix)


def test_external_writer_triggers_exactly_one_reload(tmp_path, monkeypatch):
    """An out-of-band sidecar change (a writer that bypassed the shared instance)
    is caught by the mtime gate: exactly one reload, reflecting the new content."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)

    # A separate instance writes the sidecar; the shared idx never saw it.
    external = embeddings.EmbeddingIndex(vault)
    external.upsert_file("b.md", ["b"], _mat([0, 1]), 2.0)
    _bump_mtime(idx.path)  # guarantee a distinct mtime for the gate

    metadata, matrix = idx.all_vectors()
    assert count["n"] == 1
    assert [m[0] for m in metadata] == ["a.md", "b.md"]
    # A second read reuses; no further reload.
    idx.all_vectors()
    assert count["n"] == 1


def test_utime_bump_alone_does_not_reload(tmp_path, monkeypatch):
    """A sidecar mtime bump with NO content change must not invalidate the cache.

    This is the WAL-checkpoint symptom in the small: a checkpoint (fired by a
    pure reader closing last) moves the main file's mtime without any row change,
    so the OLD mtime-keyed cache spuriously full-reloads. The generation-keyed
    cache ignores a bare mtime move. RED on the mtime-keyed implementation.
    """
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm; cache keyed on generation

    count = _count_loads(monkeypatch, idx)
    _bump_mtime(idx.path)  # move mtime WITHOUT changing content (checkpoint symptom)
    idx.all_vectors()
    idx.all_vectors()
    assert count["n"] == 0  # generation unchanged -> served from cache, no reload


def test_rebuild_all_does_not_splice_per_file(tmp_path, monkeypatch):
    """rebuild_all writes in one transaction and leaves a cold cache (one reload
    on next read) — never O(N) per-file splices."""
    vault = _fresh_vault(tmp_path)
    kb = vault / "Knowledge Base"
    (kb / "one.md").write_text("---\ntype: note\n---\n# One\nalpha beta\n", encoding="utf-8")
    (kb / "two.md").write_text("---\ntype: note\n---\n# Two\ngamma delta\n", encoding="utf-8")

    # Stub the model so rebuild_all embeds without torch.
    def fake_embed(texts, *, is_query=False):
        return np.stack([_pad([float(len(t))]) for t in texts], axis=0)

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)

    idx = embeddings.get_embedding_index(vault)
    total = idx.rebuild_all()
    assert total >= 2
    with idx._lock:
        assert idx._cache is None  # left cold; next read does one full load
    _, matrix = idx.all_vectors()
    assert matrix.shape[0] == total


def test_sidecar_uses_wal(tmp_path):
    """WAL is what lets a reader proceed without blocking a concurrent writer."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.EmbeddingIndex(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)  # creates the sidecar
    conn = sqlite3.connect(idx.path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_concurrent_readers_and_writer_stay_correct(tmp_path):
    """Stress the RLock + copy-on-write swap: readers never see a torn cache and
    the final state is correct. (Reload COUNT is racy under threads, so this
    asserts correctness, not the counter — the deterministic count claims live in
    the single-threaded tests above.)"""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    for i in range(20):
        idx.upsert_file(f"f{i:02d}.md", ["c"], _mat([i, 0]), float(i))
    idx.all_vectors()  # warm

    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                meta, matrix = idx.all_vectors()
                assert len(meta) == matrix.shape[0]  # never torn
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def writer():
        try:
            for j in range(200):
                idx.upsert_file("f00.md", ["c"], _mat([j % 7, 1]), float(100 + j))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    w = threading.Thread(target=writer)
    for t in readers:
        t.start()
    w.start()
    w.join()
    stop.set()
    for t in readers:
        t.join()

    assert not errors
    meta, matrix = idx.all_vectors()
    assert len(meta) == matrix.shape[0] == 20  # same file set, no leaks/dupes
    assert sorted({m[0] for m in meta}) == [f"f{i:02d}.md" for i in range(20)]


def test_find_vector_lane_reuses_shared_matrix(tmp_path, monkeypatch):
    """End-to-end through the REAL `find()` entry point (deterministic fake
    embedder, no torch): three distinct finds share ONE matrix load, and the
    vector lane genuinely ran — guarding against a silent BM25 fallback that
    would make the reuse assertion pass for the wrong reason.

    Pinned to the in-memory scan (`EXOMEM_VEC_BACKEND=numpy`): the shared-matrix
    contract this asserts is scoped to that backend — when the vec0 backend
    serves search the matrix deliberately never loads (asserted in
    tests/test_vecstore.py), which would make the one-load count read 0 wherever
    sqlite-vec happens to be installed."""
    from exomem import find as find_module
    from exomem import readiness

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")  # bypass the hot result cache
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    readiness.reset()

    vault = _fresh_vault(tmp_path)
    rel_a = "Knowledge Base/Notes/Insights/alpha.md"
    rel_b = "Knowledge Base/Notes/Insights/beta.md"
    for rel, title in ((rel_a, "Alpha"), (rel_b, "Beta")):
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"---\ntype: insight\nstatus: active\ncreated: 2026-01-01\n"
            f"updated: 2026-01-01\n---\n\n# {title}\n\nbody text {title}\n",
            encoding="utf-8",
        )

    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file(rel_a, ["alpha chunk"], _mat([1, 0]), 1.0)
    idx.upsert_file(rel_b, ["beta chunk"], _mat([0, 1]), 2.0)

    # Query embedder → deterministic vector aligned to note A (no model needed).
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query=False: np.stack([_pad([1, 0]) for _ in texts]),
    )

    find_module.clear_cache()
    count = _count_loads(monkeypatch, idx)

    first_hits = None
    for q in ("first query", "second query", "third query"):
        timings = find_module.FindTimings()
        hits = find_module.find(vault, query=q, mode="vector", limit=5, timings=timings)
        stage = timings.as_dict()["stages"].get("vector", {})
        assert stage and not stage.get("skipped") and not stage.get("error"), (
            f"vector lane did not run cleanly: {stage!r}"
        )
        if first_hits is None:
            first_hits = hits

    assert first_hits and first_hits[0].path == rel_a  # query-aligned note ranks first
    assert count["n"] == 1  # one sqlite load served all three distinct finds


# --------------------------------------------------------------------------- #
# Generation keying (the WAL-checkpoint/mtime bug fix)
# --------------------------------------------------------------------------- #


def _make_legacy_sidecar(
    path: Path, rows: list[tuple[str, int, list[float], float]]
) -> None:
    """Write an OLD sidecar: the `chunks` table only, NO `meta` table — exactly
    what a pre-generation binary left on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE chunks (file_path TEXT NOT NULL, chunk_idx INTEGER NOT NULL, "
            "chunk_text TEXT NOT NULL, vector BLOB NOT NULL, file_mtime REAL NOT NULL, "
            "PRIMARY KEY (file_path, chunk_idx))"
        )
        conn.executemany(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
            [(fp, i, "t", _pad(v).tobytes(), m) for fp, i, v, m in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _meta_exists(path: Path) -> bool:
    conn = sqlite3.connect(path)
    try:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_writer_under_held_reader_txn_no_reload(tmp_path, monkeypatch):
    """The proven WAL race: a reader holds an open txn while a writer commits, so
    the reader closes LAST and the checkpoint (and the main-file mtime move) lands
    after the writer's _patch_cache. Old mtime keying spuriously reloads; the
    generation-keyed cache does not."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm at generation 1

    count = _count_loads(monkeypatch, idx)

    # A separate connection opens a read txn and holds it — this is what defers
    # the WAL checkpoint to whichever connection closes last.
    reader = sqlite3.connect(idx.path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM chunks").fetchone()
        # Writer commits through the shared index while the reader txn is open.
        idx.upsert_file("b.md", ["b"], _mat([0, 1]), 2.0)  # bumps gen, patches cache
        metadata, matrix = idx.all_vectors()
        assert [m[0] for m in metadata] == ["a.md", "b.md"]
        assert matrix.shape[0] == 2
    finally:
        reader.close()  # reader closes LAST -> the deferred checkpoint fires now

    # Stand in for that checkpoint's observable effect: the mtime moves with the
    # content unchanged. Old mtime keying reloads here; generation keying does not.
    _bump_mtime(idx.path)
    idx.all_vectors()
    idx.all_vectors()
    assert count["n"] == 0  # generation stayed at 2 the whole time -> no reload


def test_external_writer_detected_via_generation(tmp_path, monkeypatch):
    """A second instance writing the sidecar bumps the on-disk generation; the
    shared index detects it and serves the new rows — with NO mtime bump needed."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)
    external = embeddings.EmbeddingIndex(vault)
    external.upsert_file("b.md", ["b"], _mat([0, 1]), 2.0)  # bumps DB generation

    metadata, matrix = idx.all_vectors()
    assert count["n"] == 1
    assert [m[0] for m in metadata] == ["a.md", "b.md"]
    idx.all_vectors()
    assert count["n"] == 1  # second read reuses the reloaded cache


def test_external_delete_detected_via_generation(tmp_path, monkeypatch):
    """An external delete bumps the generation; the block is dropped on next read."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.upsert_file("b.md", ["b"], _mat([0, 1]), 2.0)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)
    external = embeddings.EmbeddingIndex(vault)
    external.delete_file("a.md")  # bumps DB generation

    metadata, matrix = idx.all_vectors()
    assert count["n"] == 1
    assert [m[0] for m in metadata] == ["b.md"]
    assert matrix.shape[0] == 1


def test_external_rebuild_serves_fresh_vectors_frozen_mtime(tmp_path, monkeypatch):
    """rebuild_all re-embeds with an UNCHANGED file mtime; the epoch/generation
    bump — not the sidecar mtime — is what invalidates. Freezing the sidecar mtime
    across the external rebuild proves the cache no longer depends on it."""
    vault = _fresh_vault(tmp_path)
    kb = vault / "Knowledge Base"
    (kb / "one.md").write_text(
        "---\ntype: note\n---\n# One\nalpha beta\n", encoding="utf-8"
    )

    seq = {"n": 0}

    def fake_embed(texts, *, is_query=False):
        seq["n"] += 1
        return np.stack([_pad([float(seq["n"]), 0.0]) for _ in texts], axis=0)

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)

    idx = embeddings.get_embedding_index(vault)
    idx.rebuild_all()
    first = idx.all_vectors()[1][0].copy()
    frozen = idx.path.stat()

    ext = embeddings.EmbeddingIndex(vault)
    ext.rebuild_all()  # re-embed -> different vectors; epoch + generation bumped
    os.utime(idx.path, ns=(frozen.st_atime_ns, frozen.st_mtime_ns))  # freeze mtime

    second = idx.all_vectors()[1][0]
    assert not np.array_equal(second, first)  # fresh vectors served despite frozen mtime


def test_epoch_bump_invalidates_without_gen_or_mtime(tmp_path, monkeypatch):
    """The epoch key catches a re-embed that neither moved a file mtime NOR is
    distinguishable by generation alone: bumping only epoch invalidates the cache."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm at (epoch 0, gen 1)
    frozen = idx.path.stat()

    count = _count_loads(monkeypatch, idx)
    # Bump ONLY the epoch (rebuild_all's re-embed marker), leave generation, and
    # freeze the mtime — isolating epoch as the sole invalidation signal.
    conn = sqlite3.connect(idx.path)
    try:
        with conn:
            embeddings._bump_meta(conn, "epoch")
    finally:
        conn.close()
    os.utime(idx.path, ns=(frozen.st_atime_ns, frozen.st_mtime_ns))

    idx.all_vectors()
    assert count["n"] == 1  # epoch moved -> reload despite unchanged gen and mtime


def test_out_of_order_patch_gen_gap_does_not_advance(tmp_path, monkeypatch):
    """A patch whose own generation skips ahead (a concurrent writer's rows this
    splice never saw) must not let the cache claim a generation it lacks rows
    for — NOR splice its content (F1: content and label are gated TOGETHER, never
    independently). The cache holds its generation; the next all_vectors()
    reload converges."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)  # DB + cache generation 1
    idx.all_vectors()
    assert idx._cache[1] == 1
    own_epoch = idx._cache.epoch
    own_instance = idx._cache.instance

    # Gap: own_gen 3 while the cache is at generation 1 -> refuse to advance AND
    # refuse to splice (z.md must not appear in the cache at all).
    idx._patch_cache("z.md", [("z.md", 0)], _mat([9, 0]), own_epoch, 3, own_instance)
    assert idx._cache[1] == 1  # stayed put — did not jump to 3
    assert "z.md" not in [m[0] for m in idx._cache.metadata]  # content NOT spliced

    # Contiguous: own_gen 2 == cached 1 + 1 -> advance AND splice.
    idx._patch_cache("b.md", [("b.md", 0)], _mat([0, 1]), own_epoch, 2, own_instance)
    assert idx._cache[1] == 2
    assert "b.md" in [m[0] for m in idx._cache.metadata]

    # The manual splices desynced the cache from the DB (still at generation 1);
    # the mismatch forces exactly one reload that converges on the DB's truth.
    count = _count_loads(monkeypatch, idx)
    metadata, _ = idx.all_vectors()
    assert count["n"] == 1
    assert [m[0] for m in metadata] == ["a.md"]


def test_delayed_patch_same_file_does_not_serve_stale_content(tmp_path, monkeypatch):
    """F1 corruption trace (a): writer A upserts file F, then stalls before
    calling `_patch_cache` (simulated: its token is captured but the patch call is
    delayed). Writer B — a later, real write through the SAME shared index —
    upserts the SAME F and patches immediately: contiguous, so B's rows land and
    the label advances. A then resumes and calls `_patch_cache` with its OWN
    (now-stale, non-contiguous) captured token and its OLDER rows. The delayed
    call must be rejected wholesale: no splice, no label move — B's genuinely
    current content must still be served, never A's stale rows."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("f.md", ["v0"], _mat([0, 0]), 0.0)  # generation 1
    idx.all_vectors()  # warm cache at generation 1
    stale_epoch = idx._cache.epoch
    stale_gen = idx._cache.generation
    stale_instance = idx._cache.instance

    # Writer B: a REAL upsert_file through the shared idx — DB write + patch both
    # happen now, advancing the cache to generation 2 with fresh content.
    idx.upsert_file("f.md", ["v2-new"], _mat([2, 2]), 2.0)
    assert idx._cache.generation == stale_gen + 1
    assert idx._cache.epoch == stale_epoch

    # Writer A resumes: its captured generation (stale_gen) is now BEHIND the
    # cache (B already advanced past it) — non-contiguous — carrying A's OLDER
    # rows for the SAME file.
    idx._patch_cache(
        "f.md", [("f.md", 0)], _mat([1, 1]), stale_epoch, stale_gen, stale_instance,
    )

    # Rejected wholesale: generation/content stay exactly what B produced.
    assert idx._cache.generation == stale_gen + 1
    metadata, matrix = idx.all_vectors()
    row = matrix[[m[0] for m in metadata].index("f.md")]
    assert np.array_equal(row[:2], [2, 2])  # B's fresh content still served
    assert not np.array_equal(row[:2], [1, 1])  # A's stale content never spliced in


def test_delayed_patch_after_rebuild_epoch_bump_does_not_serve_stale_content(
    tmp_path, monkeypatch
):
    """F1 corruption trace (b): same shape but racing rebuild_all's epoch bump. A
    patch captured BEFORE a rebuild (carrying the pre-rebuild epoch) must not be
    allowed to splice its stale rows into the freshly-reloaded post-rebuild cache
    — the epoch mismatch alone rejects it, independent of generation contiguity."""
    vault = _fresh_vault(tmp_path)
    kb = vault / "Knowledge Base"
    (kb / "f.md").write_text("---\ntype: note\n---\n# F\nalpha\n", encoding="utf-8")

    calls = {"n": 0}

    def fake_embed(texts, *, is_query=False):
        calls["n"] += 1
        return np.stack([_pad([float(calls["n"]), 0.0]) for _ in texts], axis=0)

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)

    idx = embeddings.get_embedding_index(vault)
    idx.rebuild_all()  # first embed pass -> vectors tagged 1.0
    idx.all_vectors()  # warm cache post-first-rebuild
    pre_epoch = idx._cache.epoch
    pre_gen = idx._cache.generation
    pre_instance = idx._cache.instance

    idx.rebuild_all()  # second embed pass -> vectors tagged 2.0; epoch AND gen bump
    idx.all_vectors()  # cache reloads fresh post-second-rebuild content

    # A delayed writer captured its (epoch, gen, instance) BEFORE the SECOND
    # rebuild and now tries to splice its stale (pre-rebuild) rows in.
    idx._patch_cache(
        "Knowledge Base/f.md", [("Knowledge Base/f.md", 0)], _mat([9, 9]),
        pre_epoch, pre_gen, pre_instance,
    )
    metadata, matrix = idx.all_vectors()
    row = matrix[[m[0] for m in metadata].index("Knowledge Base/f.md")]
    assert not np.array_equal(row[:2], [9, 9])  # stale patch rejected
    assert row[0] == 2.0  # genuinely current (2nd rebuild) content served


def test_token_read_failure_serves_warm_cache_without_raising(tmp_path, monkeypatch):
    """F2: a transient sqlite error on the freshness token read (e.g. a
    locked/corrupt sidecar) must not propagate out of `all_vectors()` — a WARM
    cache is served instead (stale-by-seconds under contention is acceptable,
    matching busy semantics). Pre-PR this path was a bare `stat()` that never
    raised this way; a caller like `audit.py` must not be taken down by it."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm

    real_connect = sqlite3.connect

    def boom(path, *a, **kw):
        if str(path) == str(idx.path):
            raise sqlite3.OperationalError("database is locked")
        return real_connect(path, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", boom)
    metadata, matrix = idx.all_vectors()  # must not raise
    assert [m[0] for m in metadata] == ["a.md"]  # warm cache served despite the failure


def test_sidecar_deleted_and_recreated_aba_detected_via_instance(tmp_path, monkeypatch):
    """F3 ABA guard: a sidecar file deleted and recreated from scratch restarts
    its (epoch, generation) counters, so a still-warm cache could otherwise
    coincidentally match the NEW file's tokens and serve the OLD file's stale
    rows forever (impossible with mtime, which a brand-new file can't collide
    with). The random `instance` nonce (unique per physical file) makes the
    mismatch detectable even when (epoch, gen) happen to coincide."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("old.md", ["old"], _mat([1, 0]), 1.0)  # generation 1
    idx.all_vectors()  # warm
    old_instance = idx._cache.instance
    old_gen = idx._cache.generation

    count = _count_loads(monkeypatch, idx)

    # Delete the sidecar (and its WAL siblings) and recreate it from scratch via
    # a second instance, driving it to the SAME generation value the old cache
    # already holds — the interesting collision case for F3.
    for suffix in ("", "-wal", "-shm"):
        p = idx.path.with_name(idx.path.name + suffix)
        if p.exists():
            p.unlink()
    fresh = embeddings.EmbeddingIndex(vault)
    fresh.upsert_file("new.md", ["new"], _mat([0, 1]), 5.0)  # new file's gen -> 1

    new_epoch, new_gen, new_instance = embeddings._peek_sidecar_token(idx.path)
    assert new_gen == old_gen  # (epoch, gen) ALONE would have looked "fresh"
    assert new_instance != old_instance  # the instance nonce catches it

    metadata, matrix = idx.all_vectors()
    assert count["n"] == 1  # detected via instance mismatch -> reloaded
    assert [m[0] for m in metadata] == ["new.md"]  # NEW file's rows, never stale "old.md"


def test_legacy_sidecar_migrates_and_mtime_fallback_invalidates(tmp_path, monkeypatch):
    """A pre-meta sidecar reads generation 0; the meta table is migrated in on
    first connect, the cache retains mtime-keyed invalidation (version-skew
    fallback) until a gen-bumping write, and an mtime bump still invalidates."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    _make_legacy_sidecar(idx.path, [("a.md", 0, [1, 0], 1.0)])
    assert not _meta_exists(idx.path)  # legacy: no meta table yet

    count = _count_loads(monkeypatch, idx)
    metadata, matrix = idx.all_vectors()  # migrates meta, loads at generation 0
    assert [m[0] for m in metadata] == ["a.md"]
    assert idx._cache[1] == 0  # legacy sidecar reads generation 0
    assert count["n"] == 1
    assert _meta_exists(idx.path)  # migration created the meta table

    # gen==0 fallback: a bare mtime bump STILL invalidates (today's semantics).
    _bump_mtime(idx.path)
    idx.all_vectors()
    assert count["n"] == 2


# --------------------------------------------------------------------------- #
# ClipIndex (visual matrix) — structural twin
# --------------------------------------------------------------------------- #


def test_clip_index_patches_in_place(tmp_path, monkeypatch):
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_clip_index(vault)
    idx.upsert("a.png", _cpad([1, 0]), 1.0)
    paths, ts, matrix = idx.all_vectors()
    assert paths == ["a.png"] and ts == [None] and matrix.shape[0] == 1

    count = _count_loads(monkeypatch, idx)

    # A video → per-keyframe rows, block-replaced and timestamp-ordered.
    idx.upsert_frames("v.mp4", [(5.0, _cpad([1, 1])), (0.0, _cpad([0, 1]))], 2.0)
    paths, ts, matrix = idx.all_vectors()
    assert paths == ["a.png", "v.mp4", "v.mp4"]
    assert ts == [None, 0.0, 5.0]
    assert matrix.shape[0] == 3

    # Re-upserting the image keeps the video keyframes (partial-delete mirror).
    idx.upsert("a.png", _cpad([2, 0]), 3.0)
    paths, ts, matrix = idx.all_vectors()
    assert paths == ["a.png", "v.mp4", "v.mp4"]
    assert matrix.shape[0] == 3

    # Delete the video → back to just the image.
    idx.delete("v.mp4")
    paths, ts, matrix = idx.all_vectors()
    assert paths == ["a.png"] and matrix.shape[0] == 1

    assert count["n"] == 0  # every step patched in place


def test_clip_sidecar_uses_wal(tmp_path):
    vault = _fresh_vault(tmp_path)
    idx = embeddings.ClipIndex(vault)
    idx.upsert("a.png", _cpad([1, 0]), 1.0)
    conn = sqlite3.connect(idx.path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_clip_utime_bump_alone_does_not_reload(tmp_path, monkeypatch):
    """ClipIndex mirror: a bare mtime bump (checkpoint symptom) does not reload."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_clip_index(vault)
    idx.upsert("a.png", _cpad([1, 0]), 1.0)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)
    _bump_mtime(idx.path)
    idx.all_vectors()
    idx.all_vectors()
    assert count["n"] == 0


def test_clip_external_writer_detected_via_generation(tmp_path, monkeypatch):
    """ClipIndex mirror: a second instance's write is caught via the generation."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_clip_index(vault)
    idx.upsert("a.png", _cpad([1, 0]), 1.0)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)
    ext = embeddings.ClipIndex(vault)
    ext.upsert("b.png", _cpad([0, 1]), 2.0)  # bumps DB generation

    paths, ts, matrix = idx.all_vectors()
    assert count["n"] == 1
    assert paths == ["a.png", "b.png"]
    idx.all_vectors()
    assert count["n"] == 1


def test_clip_external_delete_detected_via_generation(tmp_path, monkeypatch):
    """ClipIndex mirror: an external delete drops the block on next read."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_clip_index(vault)
    idx.upsert("a.png", _cpad([1, 0]), 1.0)
    idx.upsert("b.png", _cpad([0, 1]), 2.0)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)
    ext = embeddings.ClipIndex(vault)
    ext.delete("a.png")  # bumps DB generation

    paths, ts, matrix = idx.all_vectors()
    assert count["n"] == 1
    assert paths == ["b.png"]


def test_clip_out_of_order_patch_does_not_splice_stale_content(tmp_path, monkeypatch):
    """ClipIndex mirror of the F1 gate: a non-contiguous patch must not splice
    content OR advance the label — content and label are gated TOGETHER."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_clip_index(vault)
    idx.upsert("a.png", _cpad([1, 0]), 1.0)  # generation 1
    idx.all_vectors()
    own_epoch = idx._cache.epoch
    own_instance = idx._cache.instance
    assert idx._cache.generation == 1

    # Gap: own_gen 3 while cached generation is 1 -> reject splice AND label.
    idx._patch_cache(
        "z.png", ["z.png"], [None], _cpad([9, 0]).reshape(1, -1),
        own_epoch, 3, own_instance,
    )
    assert idx._cache.generation == 1
    assert "z.png" not in idx._cache.paths

    # Contiguous: own_gen 2 == cached 1 + 1 -> splice AND advance.
    idx._patch_cache(
        "b.png", ["b.png"], [None], _cpad([0, 1]).reshape(1, -1),
        own_epoch, 2, own_instance,
    )
    assert idx._cache.generation == 2
    assert "b.png" in idx._cache.paths


def _bump_mtime(path: Path) -> None:
    """Push a sidecar's mtime clearly forward so the mtime gate can't miss it
    (Windows st_mtime resolution is coarse). Mirrors tests/test_find_hot_cache.py."""
    st = path.stat()
    import os

    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
