"""The projected recall resolver is never rebuilt on a reader's thread.

`find.recall_resolver_snapshot` builds from a full `walk_vault_md` plus a parse
per admitted page. Measured on a 2.4k-page production vault it cost 30.7s of a
34s `ask_memory` call -- 90% of that read (#676), while every retrieval lane in
the same call finished in under half a second.

The warm path was already careful: `on_resolver_files_changed` patches the cache
incrementally from the exact event checkpoint. What had no bound was the miss.
Eviction is cheap and asynchronous; the rebuild behind it was expensive and
synchronous, so the entire cost landed on whichever caller asked first -- and on
the read path that is a reader.

These tests pin the two halves that fix it, and the two places it must NOT fire.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import vault as vault_module


@pytest.fixture(autouse=True)
def _enable_resolver_warm(monkeypatch: pytest.MonkeyPatch):
    """This suite exists to exercise the warm thread; the suite defaults it off."""
    monkeypatch.delenv("EXOMEM_DISABLE_RESOLVER_WARM", raising=False)
    yield
    # Never leave a daemon thread walking this test's tmp vault.
    assert find_module.await_recall_resolver_warm(timeout=30.0)
    find_module.unload_ram_caches()


def _seed(root: Path, count: int = 3) -> Path:
    notes = root / "Knowledge Base" / "Notes" / "Insights"
    notes.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (notes / f"page-{index}.md").write_text(
            f"---\ntitle: Page {index}\ntype: insight\nstatus: active\n---\n\nBody {index}.\n",
            encoding="utf-8",
        )
    return root


def _count_builds(monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.0) -> list[float]:
    """Record one entry per resolver BUILD, optionally slowing each.

    `WikilinkResolver.from_entries` rather than `walk_vault_md`: the freshness
    key this function needs walks the vault too, so counting walks would count
    work that is not the build under test.
    """
    builds: list[float] = []
    real_from_entries = vault_module.WikilinkResolver.from_entries

    def counted(vault_root, entries):  # noqa: ANN001, ANN202
        builds.append(time.monotonic())
        if delay:
            time.sleep(delay)
        return real_from_entries(vault_root, entries)

    monkeypatch.setattr(vault_module.WikilinkResolver, "from_entries", counted)
    return builds


def test_eviction_warms_the_cache_instead_of_leaving_it_for_a_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every eviction site is a correctness refusal, and each is right to refuse.

    What they could not do is leave the vault with no resolver and no plan to
    get one. That is what put a whole-vault walk in front of the next query.
    """
    root = _seed(tmp_path)
    find_module.recall_resolver_snapshot(root)
    assert Path(root) in find_module._RECALL_RESOLVER_CACHE

    builds = _count_builds(monkeypatch)
    with find_module._RESOLVER_LOCK:
        find_module._evict_recall_resolver(Path(root))

    assert find_module.await_recall_resolver_warm(timeout=30.0)
    assert len(builds) == 1, "the eviction did not rebuild exactly once off the read path"
    assert Path(root) in find_module._RECALL_RESOLVER_CACHE


def test_a_cold_burst_of_readers_walks_the_vault_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N readers arriving cold used to run N whole-vault walks concurrently.

    Each read the same files and produced a resolver N-1 of them discarded. The
    single-flight does not make any one caller faster -- it stops the vault
    being walked once per caller, which is what turns a slow read into a slow
    server.
    """
    root = _seed(tmp_path, count=8)
    builds = _count_builds(monkeypatch, delay=0.25)
    results: list[object] = []
    barrier = threading.Barrier(4)

    def reader() -> None:
        barrier.wait(timeout=10)
        results.append(find_module.recall_resolver_snapshot(root))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 4
    assert all(result is not None for result in results)
    assert len(builds) == 1, f"expected one build for four cold readers, got {len(builds)}"


def test_the_idle_reaper_does_not_rebuild_what_it_just_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`unload_ram_caches` is memory being handed back, not a correctness refusal.

    Spending a thread to rebuild the structure it just released would defeat the
    point of releasing it. The next caller builds, single-flighted.
    """
    root = _seed(tmp_path)
    find_module.recall_resolver_snapshot(root)
    builds = _count_builds(monkeypatch)

    find_module.unload_ram_caches()
    # Long enough that a scheduled thread would have started and been recorded.
    time.sleep(0.2)

    assert builds == []
    assert Path(root) not in find_module._RECALL_RESOLVER_CACHE


def test_the_warm_thread_can_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background walk of the operator's vault has to be refusable.

    Same shape as every other EXOMEM_DISABLE_* switch: off restores the previous
    behaviour exactly -- the cache stays cold and the next caller builds it.
    """
    root = _seed(tmp_path)
    find_module.recall_resolver_snapshot(root)
    monkeypatch.setenv("EXOMEM_DISABLE_RESOLVER_WARM", "1")
    builds = _count_builds(monkeypatch)

    with find_module._RESOLVER_LOCK:
        find_module._evict_recall_resolver(Path(root))
    time.sleep(0.2)

    assert builds == []
    assert Path(root) not in find_module._RECALL_RESOLVER_CACHE

    # And the reader still gets a correct resolver, the slow way.
    assert find_module.recall_resolver_snapshot(root) is not None
    assert len(builds) == 1


def test_a_rebuilt_resolver_still_resolves_the_same_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound may not change what a caller gets.

    Single-flight and background warming only decide WHO pays for the build and
    WHEN; a resolver from either path has to answer identically, or this traded
    latency for wrong recall.
    """
    root = _seed(tmp_path, count=4)
    rels = [f"Knowledge Base/Notes/Insights/page-{index}.md" for index in range(4)]
    direct = find_module.recall_resolver_snapshot(root)
    baseline = (
        set(direct.full_paths),
        set(direct.kb_stripped),
        {key: sorted(value) for key, value in direct.stems.items()},
        {key: sorted(value) for key, value in direct.titles.items()},
        {rel: direct.title_key_for_path(rel) for rel in rels},
    )
    assert baseline[4] == {rel: f"page {rel[-4]}" for rel in rels}, baseline[4]

    with find_module._RESOLVER_LOCK:
        find_module._evict_recall_resolver(Path(root))
    assert find_module.await_recall_resolver_warm(timeout=30.0)

    warmed = find_module.recall_resolver_snapshot(root)
    assert (
        set(warmed.full_paths),
        set(warmed.kb_stripped),
        {key: sorted(value) for key, value in warmed.stems.items()},
        {key: sorted(value) for key, value in warmed.titles.items()},
        {rel: warmed.title_key_for_path(rel) for rel in rels},
    ) == baseline


def test_a_follower_never_rebuilds_what_the_leader_just_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leader's signal has to mean "cached", not "finished building".

    Signalling at the end of the build wakes every follower onto a still-empty
    cache; each then elects itself leader and rebuilds, which is the stampede
    the single-flight exists to prevent, narrowed to a race window instead of
    removed. Pinning it deterministically because the window is small enough
    that a timing-based test would pass by luck.
    """
    root = _seed(tmp_path, count=6)
    release = threading.Event()
    builds: list[int] = []
    real_from_entries = vault_module.WikilinkResolver.from_entries

    def gated(vault_root, entries):  # noqa: ANN001, ANN202
        builds.append(1)
        assert release.wait(20)
        return real_from_entries(vault_root, entries)

    monkeypatch.setattr(vault_module.WikilinkResolver, "from_entries", gated)

    results: list[object] = []

    def reader() -> None:
        results.append(find_module.recall_resolver_snapshot(root))

    leader = threading.Thread(target=reader)
    leader.start()
    deadline = time.monotonic() + 20
    while not builds and time.monotonic() < deadline:
        time.sleep(0.01)
    assert builds, "the leader never entered the build"

    followers = [threading.Thread(target=reader) for _ in range(3)]
    for thread in followers:
        thread.start()
    # Parked on the leader's event, not building.
    time.sleep(0.3)
    assert len(builds) == 1

    release.set()
    for thread in [leader, *followers]:
        thread.join(timeout=30)

    assert len(results) == 4
    assert all(result is not None for result in results)
    assert len(builds) == 1, "a follower rebuilt after the leader published"
