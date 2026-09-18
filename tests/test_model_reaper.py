"""The idle reaper evicts caches nobody is using, not caches nobody has seen empty.

Measured 2026-09-18 on the personal service: the cache slots stamped only the
last time a tick observed the cache EMPTY. Requests refilled the find RAM
caches between every 60 s tick, so they were never observed empty, looked
stale forever once fifteen minutes had passed since process start, and were
evicted on every tick -- 48 times in four hours. Each eviction handed the next
recall the cold cliff: a 73k-row matrix reload, page re-parses, a BM25 corpus
rebuild. Idle now means unused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import find, find_corpus, model_reaper, readiness


@pytest.fixture(autouse=True)
def _evictable(monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import mode

    monkeypatch.setattr(mode, "retain_cpu_caches", lambda: False)
    monkeypatch.setattr(readiness, "is_warming", lambda: False)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _slot(clock: _Clock, activity=None, *, refilled: bool = True):
    unloads: list[float] = []
    state = {"loaded": True}

    def unload() -> bool:
        unloads.append(clock.now)
        state["loaded"] = False
        if refilled:
            state["loaded"] = True  # a request refills it before the next tick
        return True

    slot = model_reaper._quiet_cache_slot(
        "cache", lambda: state["loaded"], unload, activity, clock=clock
    )
    return slot, unloads


def _ticks(slot, clock: _Clock, *, seconds: float, threshold: float = 900.0, tick: float = 60.0):
    reaped = []
    end = clock.now + seconds
    while clock.now < end:
        clock.now += tick
        reaped += model_reaper._reap_once([slot], clock.now, threshold)
    return reaped


def test_a_cache_refilled_between_ticks_is_not_reaped_on_every_tick() -> None:
    """The reproduction: always loaded, never observed empty, no use signal."""
    clock = _Clock()
    slot, unloads = _slot(clock)
    reaped = _ticks(slot, clock, seconds=3600)
    # Once fifteen minutes after it was first seen loaded, then once per
    # fifteen minutes after each eviction -- never once per tick. Sixty
    # ticks used to mean up to forty-five evictions.
    assert len(unloads) == 3, unloads
    assert min(b - a for a, b in zip(unloads, unloads[1:], strict=False)) >= 900
    assert reaped.count("cache") == 3


def test_a_cache_in_use_is_never_reaped() -> None:
    clock = _Clock()
    counter = {"hits": 0}

    def activity():
        counter["hits"] += 1  # every tick sees new use
        return counter["hits"]

    slot, unloads = _slot(clock, activity)
    _ticks(slot, clock, seconds=4 * 3600)
    assert unloads == []


def test_a_cache_left_alone_is_reaped_once_the_threshold_after_its_last_use() -> None:
    clock = _Clock()
    counter = {"hits": 0}
    slot, unloads = _slot(clock, lambda: counter["hits"])
    _ticks(slot, clock, seconds=600)
    counter["hits"] += 1  # one more use at t+600
    _ticks(slot, clock, seconds=840)  # up to t+1440: 840 s since the last use
    assert unloads == []
    _ticks(slot, clock, seconds=120)  # crosses 900 s since the last use
    assert len(unloads) == 1


def test_the_page_cache_counts_the_pages_it_serves(tmp_path: Path) -> None:
    page = tmp_path / "Knowledge Base" / "Notes" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntitle: A\n---\n\nbody\n", encoding="utf-8")
    cache = find_corpus.FrontmatterCache()
    assert cache.get(page, tmp_path) is not None
    assert cache.hits == 0
    assert cache.get(page, tmp_path) is not None
    assert cache.hits == 1


def test_find_reports_a_use_fingerprint_that_moves_with_use(tmp_path: Path) -> None:
    page = tmp_path / "Knowledge Base" / "Notes" / "b.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntitle: B\n---\n\nbody\n", encoding="utf-8")
    find.clear_cache()
    before = find.cache_activity()
    find._CACHE.get(page, tmp_path)
    find._CACHE.get(page, tmp_path)
    after = find.cache_activity()
    assert after != before
    assert find.cache_status()["pages"]["hits"] >= 1


def test_a_matrix_served_by_catch_up_counts_as_used(tmp_path: Path, monkeypatch) -> None:
    """The catch-up branch is the common serve under a moving write generation."""
    import numpy as np

    from exomem import embedding_index, recall_policy, sidecar_store

    idx = embedding_index.EmbeddingIndex(tmp_path)
    idx.path.parent.mkdir(parents=True, exist_ok=True)
    idx.path.touch()
    identity = ("id", "sig")
    monkeypatch.setattr(recall_policy, "recall_policy_identity", lambda _root: identity)
    matrix = np.zeros((1, embedding_index.VECTOR_DIM), dtype=np.float32)
    first = embedding_index._EmbCache(1, 1, 1, 0.0, identity, [("a.md", 0)], matrix)
    monkeypatch.setattr(idx, "_load_all_rows", lambda: first)
    idx.all_vectors()
    assert idx.cache_status()["hits"] == 1
    monkeypatch.setattr(sidecar_store, "try_serve_cached", lambda _c, _path: None)
    monkeypatch.setattr(
        idx,
        "_catch_up_cache",
        lambda c: embedding_index._EmbCache(
            c.epoch,
            c.generation + 1,
            c.instance,
            c.mtime,
            c.recall_policy_identity,
            c.metadata,
            c.matrix,
        ),
    )
    for _ in range(3):
        idx.all_vectors()
    assert idx.cache_status()["hits"] == 4


def test_bm25_counts_only_searches_its_own_corpus_served(tmp_path: Path, monkeypatch) -> None:
    from exomem import bm25, lexstore

    index = bm25.BM25Index()
    monkeypatch.setattr(lexstore, "search_bm25", lambda *_a, **_k: [("a.md", 1.0)])
    assert index.search(tmp_path, "query", 5) == [("a.md", 1.0)]
    assert index.cache_status()["hits"] == 0  # the sidecar served; nothing here to reclaim
    monkeypatch.setattr(lexstore, "search_bm25", lambda *_a, **_k: None)
    index.search(tmp_path, "query", 5)  # falls through to the python corpus (empty vault)
    assert index.cache_status()["hits"] == 1
