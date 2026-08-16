"""Bounded read-side catch-up for the embedding matrix cache (GitHub #531, H4).

Before this change, a warm matrix cache that fell even ONE generation behind the
sidecar was thrown away wholesale: `sidecar_store.cache_is_fresh` demands exact
generation equality, `try_serve_cached` then returns None, and
`EmbeddingIndex.all_vectors()` falls through to `_load_all_rows()` -- the
O(vault) `SELECT` + `np.stack`. Production telemetry (0.52.2) showed that firing
on deltas of 2 and 11 generations over 60k rows, ~21 s per reload, every ~26-44
minutes.

These tests lock the bounded catch-up: a SMALL delta patches only the rows whose
paths actually changed, while epoch/instance changes, wide deltas, and legacy
(generation 0) sidecars still take the full reload. Every assertion counts
`_load_all_rows` calls (the named full-reload seam) and compares the served
matrix against a genuinely-reloaded one -- never wall-clock.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from exomem import embedding_index, embeddings, sidecar_store


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


def _count_loads(monkeypatch: pytest.MonkeyPatch, idx) -> dict[str, int]:
    """Wrap idx._load_all_rows to count genuine full reloads."""
    calls = {"n": 0}
    orig = idx._load_all_rows

    def wrapped():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(idx, "_load_all_rows", wrapped)
    return calls


def _ground_truth(vault: Path) -> tuple[list[tuple[str, int]], np.ndarray]:
    """What a cold instance loads straight from the sidecar right now."""
    cold = embeddings.EmbeddingIndex(vault)
    return cold.all_vectors()


def _seed(idx, count: int = 4) -> None:
    for i in range(count):
        idx.upsert_file(f"f{i:02d}.md", ["c"], _mat([float(i), 0.0]), float(i))


def _skew_write(path: Path, rel_path: str, vectors: np.ndarray) -> int:
    """Write one path exactly as a generation-AWARE 0.52.2 binary does on the wire.

    Replaces the path's rows and bumps `meta.generation` in one transaction, and
    writes NO change-log row -- that binary has never heard of one. This is not
    hypothetical: `scripts/upgrade.ps1` restarts the service BEFORE calling
    `Sync-ExomemUvCli` (upgrade.ps1:144), and `-SkipRestart` / `-CliSync never`
    skip that sync outright, so "new service, old CLI, one vault" is a supported
    reachable state; `docs/deployment.md:377-379` documents direct CLI
    maintenance against that same vault.

    On origin/main such a write ALWAYS invalidated the warm matrix, because any
    generation bump did. The catch-up must not regress that.
    """
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
            conn.executemany(
                "INSERT INTO chunks (file_path, chunk_idx, chunk_text, vector, file_mtime) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (rel_path, i, "t", vectors[i].astype(np.float32).tobytes(), 9.0)
                    for i in range(vectors.shape[0])
                ],
            )
            return sidecar_store.bump_meta(conn, "generation")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The fire: a 2-generation drift must NOT cost a full O(vault) reload
# --------------------------------------------------------------------------- #


def test_small_generation_delta_patches_instead_of_full_reloading(tmp_path, monkeypatch):
    """RED before the fix: a cache 2 generations stale full-reloads everything.

    Reproduces the production shape exactly -- writes land through an instance
    that is NOT the one holding the warm matrix, so the warm cache drifts a
    couple of generations behind the sidecar without any epoch or instance
    change. The catch-up must serve the correct, current matrix having read only
    the changed paths' rows.
    """
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    _seed(idx)
    idx.all_vectors()  # warm
    warm_gen = idx._cache.generation

    count = _count_loads(monkeypatch, idx)

    external = embeddings.EmbeddingIndex(vault)
    external.upsert_file("f01.md", ["c"], _mat([9.0, 9.0]), 10.0)  # update
    external.upsert_file("z.md", ["z1", "z2"], _mat([0.0, 1.0], [1.0, 1.0]), 11.0)  # insert

    metadata, matrix = idx.all_vectors()

    assert idx._cache.generation == warm_gen + 2  # exactly the observed drift
    assert count["n"] == 0, "a 2-generation delta still paid a full O(vault) reload"

    truth_meta, truth_matrix = _ground_truth(vault)
    assert metadata == truth_meta
    assert np.array_equal(matrix, truth_matrix)


def test_catchup_matrix_is_identical_to_a_full_reload_across_edit_kinds(
    tmp_path, monkeypatch
):
    """Insert, grow, shrink, and delete inside one delta window: the patched
    matrix must be byte-identical to what a from-scratch reload produces."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    _seed(idx)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)

    external = embeddings.EmbeddingIndex(vault)
    external.upsert_file("aaa.md", ["a1", "a2", "a3"], _mat([1, 0], [2, 0], [3, 0]), 5.0)
    external.upsert_file("f00.md", ["only"], _mat([7, 7]), 6.0)  # shrink f00 to 1 row
    external.upsert_file("f02.md", ["g1", "g2"], _mat([4, 4], [5, 5]), 7.0)  # grow
    external.delete_file("f03.md")

    metadata, matrix = idx.all_vectors()
    assert count["n"] == 0

    truth_meta, truth_matrix = _ground_truth(vault)
    assert metadata == truth_meta
    assert np.array_equal(matrix, truth_matrix)
    assert [m[0] for m in metadata] == [
        "aaa.md",
        "aaa.md",
        "aaa.md",
        "f00.md",
        "f01.md",
        "f02.md",
        "f02.md",
    ]  # inserted block sorts first, f00 shrank, f02 grew, f03 is gone


def test_delete_only_delta_is_caught_up(tmp_path, monkeypatch):
    """A pure removal leaves no row to read -- the tombstone must come from the
    per-path change log, not from the surviving rows."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    _seed(idx)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)
    embeddings.EmbeddingIndex(vault).delete_file("f01.md")

    metadata, matrix = idx.all_vectors()
    assert count["n"] == 0
    assert [m[0] for m in metadata] == ["f00.md", "f02.md", "f03.md"]
    truth_meta, truth_matrix = _ground_truth(vault)
    assert metadata == truth_meta
    assert np.array_equal(matrix, truth_matrix)


def test_catchup_emits_a_distinct_reason_tag(tmp_path, caplog):
    """Production verification greps for `reason=genuine`; a patched catch-up
    must therefore log under its OWN reason so the two never blur."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    _seed(idx)
    idx.all_vectors()  # warm
    warm_gen = idx._cache.generation

    caplog.set_level(logging.INFO, logger="exomem.embedding_index")
    embeddings.EmbeddingIndex(vault).upsert_file("z.md", ["z"], _mat([0, 1]), 9.0)
    idx.all_vectors()

    messages = [r.getMessage() for r in caplog.records]
    assert not [m for m in messages if "reason=genuine" in m], messages
    catchups = [m for m in messages if "reason=catchup" in m]
    assert len(catchups) == 1, messages
    line = catchups[0]
    assert re.search(r"\bpaths=1\b", line), line
    assert re.search(r"\bdelta=1\b", line), line
    assert re.search(rf"\bcached_gen={warm_gen}\b", line), line
    assert re.search(rf"\bgen={warm_gen + 1}\b", line), line


# --------------------------------------------------------------------------- #
# The bounds: everything the catch-up must still refuse
# --------------------------------------------------------------------------- #


def test_wide_delta_falls_back_to_a_full_reload(tmp_path, monkeypatch, caplog):
    """Past the bound, a full reload is the cheaper and simpler answer -- and it
    must still be tagged `reason=genuine`."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    _seed(idx)
    idx.all_vectors()  # warm

    monkeypatch.setattr(embedding_index, "CATCHUP_MAX_PATHS", 1)
    count = _count_loads(monkeypatch, idx)

    external = embeddings.EmbeddingIndex(vault)
    external.upsert_file("y.md", ["y"], _mat([1, 1]), 8.0)
    external.upsert_file("z.md", ["z"], _mat([0, 1]), 9.0)

    caplog.set_level(logging.INFO, logger="exomem.embedding_index")
    metadata, matrix = idx.all_vectors()

    assert count["n"] == 1
    assert [m[0] for m in metadata][-2:] == ["y.md", "z.md"]
    assert [m for m in (r.getMessage() for r in caplog.records) if "reason=genuine" in m]


def test_generation_delta_bound_forces_a_full_reload(tmp_path, monkeypatch):
    """A cache left behind for many generations is not a catch-up candidate."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    _seed(idx)
    idx.all_vectors()  # warm

    monkeypatch.setattr(embedding_index, "CATCHUP_MAX_GENERATIONS", 1)
    count = _count_loads(monkeypatch, idx)

    external = embeddings.EmbeddingIndex(vault)
    external.upsert_file("z.md", ["z"], _mat([0, 1]), 9.0)
    external.upsert_file("z.md", ["z", "z2"], _mat([0, 1], [1, 1]), 10.0)

    metadata, _ = idx.all_vectors()
    assert count["n"] == 1
    assert [m[0] for m in metadata][-2:] == ["z.md", "z.md"]


def test_epoch_bump_still_forces_a_full_reload(tmp_path, monkeypatch):
    """A re-embed (rebuild_all) replaces every vector; only the epoch says so,
    and the per-path log cannot describe it. Catch-up must refuse."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    _seed(idx)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)
    conn = sqlite3.connect(idx.path)
    try:
        with conn:
            sidecar_store.bump_meta(conn, "epoch")
    finally:
        conn.close()

    idx.all_vectors()
    assert count["n"] == 1


def test_recreated_sidecar_instance_change_still_forces_a_full_reload(
    tmp_path, monkeypatch
):
    """F3's ABA guard survives the catch-up: a deleted-and-recreated sidecar
    carries a new instance nonce, so no delta may be applied to the old rows."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("old.md", ["old"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm at generation 1
    old_gen = idx._cache.generation

    count = _count_loads(monkeypatch, idx)
    for suffix in ("", "-wal", "-shm"):
        p = idx.path.with_name(idx.path.name + suffix)
        if p.exists():
            p.unlink()
    embeddings.EmbeddingIndex(vault).upsert_file("new.md", ["new"], _mat([0, 1]), 5.0)

    _epoch, new_gen, _instance = sidecar_store.peek_sidecar_token(idx.path)
    assert new_gen == old_gen  # (epoch, gen) alone would look catch-up-eligible

    metadata, _ = idx.all_vectors()
    assert count["n"] == 1
    assert [m[0] for m in metadata] == ["new.md"]


def test_rebuild_all_is_never_caught_up(tmp_path, monkeypatch):
    """An external rebuild wipes and re-embeds everything; the next read must be
    a genuine full reload serving the NEW vectors, never a patched old matrix."""
    vault = _fresh_vault(tmp_path)
    kb = vault / "Knowledge Base"
    (kb / "one.md").write_text("---\ntype: note\n---\n# One\nalpha beta\n", encoding="utf-8")

    seq = {"n": 0}

    def fake_embed(texts, *, is_query=False):
        seq["n"] += 1
        return np.stack([_pad([float(seq["n"]), 0.0]) for _ in texts], axis=0)

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)

    idx = embeddings.get_embedding_index(vault)
    idx.rebuild_all()
    first = idx.all_vectors()[1][0].copy()

    count = _count_loads(monkeypatch, idx)
    embeddings.EmbeddingIndex(vault).rebuild_all()

    second = idx.all_vectors()[1][0]
    assert count["n"] == 1
    assert not np.array_equal(second, first)


def test_legacy_generation_zero_sidecar_is_never_caught_up(tmp_path, monkeypatch):
    """Generation 0 means the mtime fallback is in force; there is no per-path
    log history to catch up from."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    path = idx.path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE chunks (file_path TEXT NOT NULL, chunk_idx INTEGER NOT NULL, "
            "chunk_text TEXT NOT NULL, vector BLOB NOT NULL, file_mtime REAL NOT NULL, "
            "PRIMARY KEY (file_path, chunk_idx))"
        )
        conn.execute(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
            ("a.md", 0, "t", _pad([1, 0]).tobytes(), 1.0),
        )
        conn.commit()
    finally:
        conn.close()

    count = _count_loads(monkeypatch, idx)
    assert [m[0] for m in idx.all_vectors()[0]] == ["a.md"]
    assert idx._cache.generation == 0
    assert count["n"] == 1

    st = path.stat()
    import os

    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    idx.all_vectors()
    assert count["n"] == 2  # mtime fallback, not a catch-up


# --------------------------------------------------------------------------- #
# The regression guard: an UNLOGGED generation bump must never be absorbed
# --------------------------------------------------------------------------- #


def test_unlogged_bump_with_unchanged_row_count_forces_a_full_reload(
    tmp_path, monkeypatch
):
    """The case no row-count cross-check can see.

    A generation-aware older binary replaces one chunk's vector in place: same
    path, same chunk count, different bytes, `meta.generation` bumped, no change
    -log row. `COUNT(*)` is identical before and after, so the count check is
    blind to it. Only a per-generation assertion that the log actually covers
    the whole delta window can refuse this -- and refuse it it must, because on
    origin/main this write invalidated the matrix and served the new vector.
    """
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.upsert_file("b.md", ["b"], _mat([0, 1]), 2.0)
    idx.all_vectors()  # warm
    warm_rows = len(idx._cache.metadata)

    count = _count_loads(monkeypatch, idx)
    _skew_write(idx.path, "a.md", _mat([7, 7]))  # same row count, new vector

    metadata, matrix = idx.all_vectors()

    assert len(metadata) == warm_rows  # the row count genuinely did not move
    assert count["n"] == 1, "an unlogged generation bump was absorbed as a catch-up"
    row = matrix[[m[0] for m in metadata].index("a.md")]
    assert np.array_equal(row[:2], [7, 7]), "served the stale pre-skew vector"
    truth_meta, truth_matrix = _ground_truth(vault)
    assert metadata == truth_meta
    assert np.array_equal(matrix, truth_matrix)


def test_unlogged_bump_is_not_papered_over_by_later_logged_writes(tmp_path, monkeypatch):
    """The permanence case, and the one a "last bump was logged" check misses.

    The skew write is followed by five ordinary logged writes with NO read in
    between, so by the time the reader looks, the most recent generation IS
    logged. Asserting only that would wave the whole window through and leave
    the stale block resident indefinitely -- its log row sits behind the cache
    generation, so no later delta ever revisits it. The log must be trusted only
    across an unbroken run of logged generations.
    """
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.upsert_file("b.md", ["b"], _mat([0, 1]), 2.0)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)
    _skew_write(idx.path, "a.md", _mat([7, 7]))

    external = embeddings.EmbeddingIndex(vault)
    for i in range(5):
        external.upsert_file(f"later{i}.md", ["x"], _mat([0, 0, float(i)]), float(20 + i))

    metadata, matrix = idx.all_vectors()

    assert count["n"] == 1
    row = matrix[[m[0] for m in metadata].index("a.md")]
    assert np.array_equal(row[:2], [7, 7]), "stale block survived behind later writes"
    truth_meta, truth_matrix = _ground_truth(vault)
    assert metadata == truth_meta
    assert np.array_equal(matrix, truth_matrix)


def test_catchup_resumes_after_an_unlogged_bump_heals(tmp_path, monkeypatch):
    """The refusal is scoped, not permanent: once the reader has full-loaded past
    the gap, a fresh contiguous run of logged writes is catch-up-eligible again."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)
    _skew_write(idx.path, "a.md", _mat([7, 7]))
    idx.all_vectors()  # heals via one full reload
    assert count["n"] == 1

    embeddings.EmbeddingIndex(vault).upsert_file("c.md", ["c"], _mat([0, 1]), 3.0)
    metadata, matrix = idx.all_vectors()

    assert count["n"] == 1  # back to patching
    truth_meta, truth_matrix = _ground_truth(vault)
    assert metadata == truth_meta
    assert np.array_equal(matrix, truth_matrix)


def test_dropped_change_log_table_is_not_trusted_from_its_surviving_markers(
    tmp_path, monkeypatch
):
    """Recovery gap: a repair that drops the log table while `meta` survives must
    not let an empty log read as "nothing changed"."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.upsert_file("b.md", ["b"], _mat([0, 1]), 2.0)
    idx.all_vectors()  # warm

    count = _count_loads(monkeypatch, idx)
    conn = sqlite3.connect(idx.path)
    try:
        with conn:
            conn.execute("DROP TABLE chunk_path_log")
            conn.execute("DELETE FROM chunks WHERE file_path = 'b.md'")
            sidecar_store.bump_meta(conn, "generation")
    finally:
        conn.close()

    metadata, _ = idx.all_vectors()
    assert count["n"] == 1
    assert [m[0] for m in metadata] == ["a.md"]


# --------------------------------------------------------------------------- #
# Structural guard: a generation bump must always declare what it changed
# --------------------------------------------------------------------------- #


def test_no_bare_generation_bump_in_the_embedding_sidecar() -> None:
    """Keep in-tree bumps on the logging helpers, so this module's own writes stay
    catch-up-eligible instead of silently degrading every reader to a full reload.

    This is a lint, not the safety guarantee: correctness rests on the runtime
    invariant (`catchup_is_eligible` refuses unless the log's contiguous run
    covers the whole delta window), which is what protects against writers this
    grep cannot see -- other modules, wrapped calls, older binaries.
    """
    source = (
        Path(__file__).resolve().parent.parent / "src" / "exomem" / "embedding_index.py"
    ).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"bump_meta\(\s*conn\s*,\s*[\"']generation[\"']", line)
    ]
    assert offenders == [], (
        "bump the embedding sidecar's generation through "
        "sidecar_store.bump_generation_for_paths / bump_generation_for_reset "
        f"so the per-path change log stays authoritative: {offenders}"
    )
