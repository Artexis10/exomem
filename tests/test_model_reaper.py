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
    # These cases pin when a cache counts as idle; the normal-mode pressure gate has its own cases.
    monkeypatch.setattr(mode, "release_idle_cpu_caches_only_under_pressure", lambda: False)
    monkeypatch.setattr(readiness, "is_warming", lambda: False)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_cloud_idle_reap_retains_only_the_core_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
    loaded = {name: True for name in ("embeddings", "reranker", "cache")}
    slots = [model_reaper.ResourceSlot(
        name, lambda name=name: loaded[name], lambda: 0, lambda: 0,
        lambda name=name: loaded.__setitem__(name, False) or True,
        is_model=name != "cache",
    ) for name in loaded]
    model_reaper._reap_once(slots, now=1000, threshold=900)
    assert loaded == {"embeddings": True, "reranker": False, "cache": False}


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


def test_normal_mode_keeps_an_idle_cache_while_memory_is_plentiful(monkeypatch) -> None:
    """The live cost: every recall after fifteen quiet minutes rebuilt the find caches cold."""
    from exomem import mode, runtime_resources

    monkeypatch.setattr(mode, "release_idle_cpu_caches_only_under_pressure", lambda: True)
    monkeypatch.setattr(runtime_resources, "memory_short", lambda floor: False)
    clock = _Clock()
    slot, unloads = _slot(clock, refilled=False)
    _ticks(slot, clock, seconds=4 * 3600)
    assert unloads == []


def test_normal_mode_releases_an_idle_cache_once_memory_runs_short(monkeypatch) -> None:
    from exomem import mode, runtime_resources

    short = {"now": False}
    monkeypatch.setattr(mode, "release_idle_cpu_caches_only_under_pressure", lambda: True)
    monkeypatch.setattr(runtime_resources, "memory_short", lambda floor: short["now"])
    clock = _Clock()
    slot, unloads = _slot(clock, refilled=False)
    _ticks(slot, clock, seconds=1800)
    assert unloads == []
    short["now"] = True
    _ticks(slot, clock, seconds=60)
    assert len(unloads) == 1


def test_normal_mode_releases_an_idle_cache_when_memory_cannot_be_read(monkeypatch) -> None:
    """A host that reports no memory keeps today's idle release rather than holding memory blind."""
    from exomem import mode, runtime_resources

    monkeypatch.setattr(mode, "release_idle_cpu_caches_only_under_pressure", lambda: True)
    monkeypatch.setattr(runtime_resources, "memory_short", lambda floor: None)
    clock = _Clock()
    slot, unloads = _slot(clock, refilled=False)
    _ticks(slot, clock, seconds=960)
    assert len(unloads) == 1


def test_the_pressure_gate_reads_the_configured_floor(monkeypatch) -> None:
    from exomem import mode, runtime_resources

    floors = []
    monkeypatch.setenv("EXOMEM_CACHE_RELEASE_AVAILABLE_MIB", "512")
    monkeypatch.setattr(mode, "release_idle_cpu_caches_only_under_pressure", lambda: True)
    monkeypatch.setattr(runtime_resources, "memory_short", lambda floor: floors.append(floor) or False)
    clock = _Clock()
    slot, _ = _slot(clock, refilled=False)
    _ticks(slot, clock, seconds=960)
    assert floors and set(floors) == {512 * 1024 * 1024}


def test_memory_is_short_when_the_unit_nears_its_cgroup_limit(tmp_path, monkeypatch) -> None:
    from exomem import runtime_resources

    group = tmp_path / "user.slice" / "exomem.service"
    group.mkdir(parents=True)
    (group / "memory.high").write_text("10737418240\n")  # 10 GiB
    (group / "memory.current").write_text(str(10737418240 - 1024 * 1024 * 1024) + "\n")  # 1 GiB left
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/user.slice/exomem.service\n")
    monkeypatch.setattr(runtime_resources, "_PROC_CGROUP", cgroup)
    monkeypatch.setattr(runtime_resources, "_CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: 40 * 1024**3)
    assert runtime_resources.memory_short(2 * 1024**3) is True
    assert runtime_resources.memory_short(512 * 1024**2) is False


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


def test_find_reaper_observes_activity_during_a_concurrent_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """An ordinary LRU hit must not break the reaper's activity read."""
    import threading
    from collections import OrderedDict
    from concurrent.futures import ThreadPoolExecutor

    iterating = threading.Event()
    resume = threading.Event()
    hit_finished = threading.Event()

    class PausingEntries(OrderedDict):
        def values(self):
            iterator = iter(super().values())
            for index, page in enumerate(iterator):
                if index == 0:
                    iterating.set()
                    assert resume.wait(3), "status traversal did not resume"
                yield page

    cache = find_corpus.FrontmatterCache(entries=PausingEntries())
    paths = [tmp_path / "a.md", tmp_path / "b.md"]
    for page in paths:
        page.write_text("# Page\n\nbody\n", encoding="utf-8")
        assert cache.get(page, tmp_path) is not None
    monkeypatch.setattr(find, "_CACHE", cache)
    clock = _Clock()
    slot = model_reaper._quiet_cache_slot(
        "find-ram-caches", lambda: True, lambda: False, find.cache_activity, clock=clock,
    )

    def read_page():
        try:
            return cache.get(paths[0], tmp_path)
        finally:
            hit_finished.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        tick = workers.submit(model_reaper._reap_once, [slot], clock.now, 900)
        try:
            assert iterating.wait(3), "the reaper did not read cache activity"
            hit = workers.submit(read_page)
            # Permit the real hit to reorder the map at this preemption point.
            # A synchronized reader instead completes before the hit proceeds.
            hit_finished.wait(0.25)
        finally:
            resume.set()
        assert tick.result(timeout=3) == []
        assert hit.result(timeout=3) is not None

    assert not [record for record in caplog.records if record.name == model_reaper.log.name], caplog.text
    assert find.cache_status()["pages"] == {"entries": 2, "body_chars": 26, "hits": 1}


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
    loads = 0

    def _load_once():
        # A second full load means the catch-up branch was declined and the
        # serve fell through; that would count hits on the wrong branch, so
        # it fails loudly here instead of passing by accident.
        nonlocal loads
        loads += 1
        if loads > 1:
            raise AssertionError("full reload taken; the catch-up branch was not exercised")
        return first

    monkeypatch.setattr(idx, "_load_all_rows", _load_once)
    idx.all_vectors()
    assert idx.cache_status()["hits"] == 1
    monkeypatch.setattr(sidecar_store, "try_serve_cached", lambda _c, _path, **_kwargs: None)
    monkeypatch.setattr(
        idx,
        "_catch_up_cache",
        lambda c, _fields: embedding_index._EmbCache(
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
