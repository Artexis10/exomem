"""Guards the embedding-index shared-cache contract (GitHub #531 H2).

These lock three things:

1. `EmbeddingIndex` has exactly ONE legal construction site in production
   code: `embeddings.get_embedding_index`, the process-shared accessor
   documented at embeddings.py:952-958. A second, throwaway construction
   bypasses the shared in-memory matrix cache -- its own `_cache` starts (and
   stays) `None`, so any purge/patch dispatched through it silently never
   reaches the instance the rest of the process reads from, guaranteeing a
   full O(vault) reload on the next read (H2).
2. `index_sync.purge_semantic_only` -- the entry point `reconcile.py:351`
   also calls -- purges the SHARED cache in place, never stranding it.
3. A non-contiguous cache write (patch OR purge) was refused silently before
   this change; Probes A/B make that refusal, and the size of a genuine full
   reload's staleness gap, observable via greppable `log.info` lines.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from exomem import embedding_index, embeddings, index_sync, sidecar_store


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


# --------------------------------------------------------------------------- #
# Guard test: exactly one legal EmbeddingIndex(...) construction site
# --------------------------------------------------------------------------- #


def _function_line_range(tree: ast.Module, name: str) -> tuple[int, int] | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node.lineno, node.end_lineno
    return None


def test_no_direct_embedding_index_construction_outside_the_shared_accessor() -> None:
    """`EmbeddingIndex(...)` must be constructed exactly once in production
    code: inside `embeddings.get_embedding_index`. Any OTHER direct
    construction bypasses the shared in-memory matrix cache and reproduces
    the H2 strand -- a throwaway instance whose purge/patch never reaches the
    instance the rest of the process reads from.

    RED on the unfixed tree: index_sync.py:471 constructs a throwaway
    `embedding_index.EmbeddingIndex(vault_root)` directly.
    """
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "exomem"
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "embedding_index.py":
            continue  # the class's own module may reference itself freely
        text = path.read_text(encoding="utf-8")
        if "EmbeddingIndex(" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        allowed_range = (
            _function_line_range(tree, "get_embedding_index")
            if path.name == "embeddings.py"
            else None
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name != "EmbeddingIndex":
                continue
            if allowed_range and allowed_range[0] <= node.lineno <= allowed_range[1]:
                continue
            offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "direct EmbeddingIndex(...) construction outside "
        f"embeddings.get_embedding_index: {offenders}"
    )


# --------------------------------------------------------------------------- #
# H2: purge_semantic_only must purge the SHARED cache, never a throwaway one
# --------------------------------------------------------------------------- #


def test_purge_semantic_only_absorbs_generation_bump_into_shared_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED on the unfixed index_sync.py:471 (H2): `purge_semantic_only` builds
    a THROWAWAY `EmbeddingIndex(vault_root)` and purges through it. The
    sidecar's on-disk write generation bumps for real, but
    `_purge_cache_paths` runs against the THROWAWAY's own (always-cold)
    `_cache`, so it returns immediately having touched nothing -- the SHARED
    instance (what find(), warm-up, and every other production caller
    actually reads) is left resident at its stale pre-purge generation with
    the purged rows still in memory. The next `all_vectors()` on the shared
    instance detects the generation gap and pays a full O(vault) reload --
    the strand this fixes.

    `reconcile.py:351` calls this SAME function, so this test also covers
    that call site: there is no second fix location.
    """
    vault = _fresh_vault(tmp_path)
    shared = embeddings.get_embedding_index(vault)
    shared.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    shared.upsert_file("b.md", ["b"], _mat([0, 1]), 2.0)
    shared.all_vectors()  # warm the shared cache
    warm_gen = shared._cache.generation

    count = _count_loads(monkeypatch, shared)

    assert index_sync.purge_semantic_only(vault, ["b.md"])

    # The shared instance must have absorbed the purge in place -- patched,
    # never stranded at its pre-purge generation.
    assert shared._cache is not None, "shared cache was dropped instead of patched"
    assert shared._cache.generation != warm_gen, (
        "shared cache generation is stranded behind the sidecar's bump"
    )
    assert [m[0] for m in shared._cache.metadata] == ["a.md"]
    assert count["n"] == 0  # patched in place -- no full reload was needed

    metadata, matrix = shared.all_vectors()
    assert [m[0] for m in metadata] == ["a.md"]
    assert matrix.shape[0] == 1
    assert count["n"] == 0  # confirms the cache already reflected the purge


# --------------------------------------------------------------------------- #
# Probe A: a refused non-contiguous cache write is now logged, not silent
# --------------------------------------------------------------------------- #


def test_patch_refusal_on_non_contiguous_write_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-contiguous `_patch_cache` call is silently refused today
    (embedding_index.py:419-420): the splice never happens, the label never
    advances, and nothing says so. This makes the refusal observable: one
    `log.info` line naming the rel_path, the write's own (epoch, gen,
    instance), the cache's (epoch, generation, instance), and the gap
    between them.

    RED on the unfixed tree: no such log line exists yet.
    """
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)  # sidecar + cache generation 1
    idx.all_vectors()  # warm
    own_epoch = idx._cache.epoch
    own_instance = idx._cache.instance
    assert idx._cache.generation == 1

    # Bump the sidecar's generation TWICE with no matching patch -- a stand-in
    # for two writers whose patches this instance never received. The cache
    # stays resident at generation 1 while the on-disk token races to 3.
    conn = sqlite3.connect(idx.path)
    try:
        with conn:
            sidecar_store.bump_meta(conn, "generation")
            sidecar_store.bump_meta(conn, "generation")
    finally:
        conn.close()

    caplog.set_level(logging.INFO, logger="exomem.embedding_index")
    # A patch arrives claiming generation 3 -- not contiguous with the cache's
    # still-resident generation 1 (contiguity needs own_gen == cached.gen + 1).
    idx._patch_cache("z.md", [("z.md", 0)], _mat([9, 0]), own_epoch, 3, own_instance)

    assert idx._cache.generation == 1  # refused: label did not advance
    assert "z.md" not in [m[0] for m in idx._cache.metadata]  # content not spliced

    refusals = [
        r for r in caplog.records if "embedding matrix patch refused" in r.getMessage()
    ]
    assert len(refusals) == 1, caplog.text
    message = refusals[0].getMessage()
    assert "z.md" in message
    assert "own=(epoch=" in message and "gen=3" in message
    assert "cached=(epoch=" in message and "gen=1" in message
    assert "delta=2" in message


def test_purge_refusal_on_non_contiguous_write_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Mirrors the patch-gate refusal log for `_purge_cache_paths`'s
    equivalent non-contiguous guard (embedding_index.py:280-284): a purge
    whose own generation is not exactly one past the cache's own generation
    invalidates the cache (rather than splicing) but was, until now, equally
    silent about it.
    """
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm
    own_epoch = idx._cache.epoch
    own_instance = idx._cache.instance
    assert idx._cache.generation == 1

    conn = sqlite3.connect(idx.path)
    try:
        with conn:
            sidecar_store.bump_meta(conn, "generation")
            sidecar_store.bump_meta(conn, "generation")
    finally:
        conn.close()

    caplog.set_level(logging.INFO, logger="exomem.embedding_index")
    idx._purge_cache_paths({"a.md"}, own_epoch, 3, own_instance)

    assert idx._cache is None  # non-contiguous purge invalidates rather than splicing

    refusals = [
        r for r in caplog.records if "embedding matrix purge refused" in r.getMessage()
    ]
    assert len(refusals) == 1, caplog.text
    message = refusals[0].getMessage()
    assert "own=(epoch=" in message and "gen=3" in message
    assert "cached=(epoch=" in message and "gen=1" in message
    assert "delta=2" in message


# --------------------------------------------------------------------------- #
# Probe B: a genuine full reload now names the cache generation it left behind
# --------------------------------------------------------------------------- #


def test_full_load_log_names_the_stale_cache_generation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full-load log line (embedding_index.py:489-495) now carries
    `cached_gen=<old cache generation or -1>` so a production
    `reason=genuine` entry names the exact gap that forced the reload.

    The warm-but-stale half pins `CATCHUP_MAX_PATHS` to 0: since #531 H4 a small
    external delta is absorbed by the bounded catch-up (logged under
    `reason=catchup`), so a GENUINE full reload has to be provoked by putting the
    delta out of catch-up range — which is exactly what `reason=genuine` is
    supposed to mean now."""
    vault = _fresh_vault(tmp_path)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["a"], _mat([1, 0]), 1.0)
    idx.all_vectors()  # warm at generation 1
    assert idx._cache.generation == 1

    caplog.set_level(logging.INFO, logger="exomem.embedding_index")
    with idx._lock:
        idx._cache = None  # a genuinely cold cache -> no prior generation to name
    idx.all_vectors()
    cold_loads = [r for r in caplog.records if "embedding matrix full load" in r.getMessage()]
    assert cold_loads and "cached_gen=-1" in cold_loads[-1].getMessage()

    caplog.clear()
    stale_gen = idx._cache.generation

    # An external instance bumps the sidecar without the shared instance's
    # knowledge -- the next read is a genuine reload of a WARM-but-stale cache.
    external = embedding_index.EmbeddingIndex(vault)
    external.upsert_file("c.md", ["c"], _mat([0, 0, 1]), 3.0)

    monkeypatch.setattr(embedding_index, "CATCHUP_MAX_PATHS", 0)  # out of catch-up range
    idx.all_vectors()
    warm_loads = [r for r in caplog.records if "embedding matrix full load" in r.getMessage()]
    assert warm_loads
    assert f"cached_gen={stale_gen}" in warm_loads[-1].getMessage()
