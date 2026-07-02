"""Process-lifetime, incremental embedding-matrix cache.

OpenSpec: incremental-embedding-matrix-cache (capability find-recall-efficiency).

These lock the behavior that decouples find latency from sidecar write churn:
the shared per-vault index loads the matrix ONCE and reuses it across calls, and
an in-process write patches the in-memory matrix in place instead of forcing a
full O(vault) reload. Assertions count genuine full reloads (`_load_all_rows`),
never wall-clock — deterministic in CI. All fabricated vectors: no torch/model.
"""

from __future__ import annotations

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
    would make the reuse assertion pass for the wrong reason."""
    from exomem import find as find_module
    from exomem import readiness

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")  # bypass the hot result cache
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


def _bump_mtime(path: Path) -> None:
    """Push a sidecar's mtime clearly forward so the mtime gate can't miss it
    (Windows st_mtime resolution is coarse). Mirrors tests/test_find_hot_cache.py."""
    st = path.stat()
    import os

    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
