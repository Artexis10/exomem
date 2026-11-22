import io
import shutil
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_media_store import _configure, _offload, _stored, _volume, selected_store as selected_store
from test_personal_original_eviction import local_store as local_store

from exomem import artifact_lifecycle, media_bindings, media_jobs, media_replicas, media_store, original_pool, originals, preserve
from exomem.governance.principal import library_scope


def _allocated(path: Path) -> int:
    blocks = getattr(path.stat(), "st_blocks", None)
    if blocks is None:
        pytest.skip("this filesystem does not report allocated blocks")
    return blocks * 512


def _allocated_volume(base: int, *paths: Path):
    total = 1_000_000

    def usage(_path):
        used = base + sum(_allocated(path) for path in paths if path.exists())
        return SimpleNamespace(total=total, used=used, free=total - used)

    return usage


def test_personal_eviction_reaches_a_later_original_after_a_bounded_pass(
    vault: Path, selected_store, local_store, monkeypatch,
):
    _, configured = local_store
    # The fixture gives the first copy an unexported key and the second an exported key.
    monkeypatch.setenv(originals.CONFIG_ENV, str(configured))
    _configure((selected_store[0], configured), vault, key_version="2")
    first = _stored(vault, "a", 601)
    _configure((selected_store[0], configured), vault)
    second = _stored(vault, "b", 300)
    monkeypatch.delenv(originals.CONFIG_ENV)
    first_before = first.read_bytes()
    expected = second.read_bytes()
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 7_500, first, second))
    monkeypatch.setattr(media_store, "_EVICTION_BATCH", 1)

    assert media_store.evict_to_target(vault) == 0
    assert first.read_bytes() == first_before
    assert second.read_bytes() == expected
    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        status = media_store.personal_eviction_status(conn, originals.configuration(vault))
        assert status["waiting"] == media_replicas.KEY_NOT_EXPORTED

    assert media_store.evict_to_target(vault) == 1
    assert first.read_bytes() == first_before
    assert not second.exists()
    with media_store.open_original(vault, second) as recovered:
        assert recovered.stream.read() == expected
    media_replicas.confirm_key_export(vault, "2")
    assert media_store.evict_to_target(vault) == 1
    assert not first.exists()


def test_personal_eviction_continues_to_the_lower_threshold_after_yielding(vault, local_store, monkeypatch):
    first = _stored(vault, "a", 150)
    second = _stored(vault, "b", 151)
    volume = _volume(10_000, 6_800, first, second)
    monkeypatch.setattr(shutil, "disk_usage", volume)
    monkeypatch.setattr(media_store, "_EVICTION_BATCH", 1)

    assert media_store.evict_to_target(vault) == 1
    assert 6_000 < volume(vault).used < 7_000
    assert not first.exists()
    assert second.exists()
    assert media_store.evict_to_target(vault) == 1
    assert not second.exists()


@pytest.mark.parametrize(("other_write", "fits"), [(0, True), (0, False), (2, True)])
def test_personal_demand_only_evicts_after_checking_enough_cached_bytes(
    vault, local_store, monkeypatch, other_write, fits,
):
    first = _stored(vault, "a", 8192)
    second = _stored(vault, "b", 4096)
    available = _allocated(first) + _allocated(second)
    assert 0 < _allocated(second) <= _allocated(first)
    monkeypatch.setattr(shutil, "disk_usage", _allocated_volume(1_000, first, second))
    monkeypatch.setattr(media_store, "_EVICTION_BATCH", 1)
    demand = available - 1 if fits else available + 1
    assert media_store.record_demand(vault, "series", demand, budget=0, reachable_budget=available + 2)

    assert media_store.evict_to_target(vault) == 0
    assert first.exists() and second.exists()

    assert media_store.evict_to_target(vault) == int(fits)
    assert first.exists() is not fits and second.exists()
    # Recheck the known remainder when another write changes the shortfall.
    monkeypatch.setattr(shutil, "disk_usage", _allocated_volume(1_000 + other_write, first, second))
    finishes = fits and not other_write
    assert media_store.evict_to_target(vault) == int(finishes)
    assert second.exists() is not finishes


@pytest.mark.parametrize("kept_for", ["unexported", "settling", "quarantined"])
def test_demand_does_not_count_an_original_that_custody_keeps(vault, selected_store, local_store, monkeypatch, kept_for):
    _, configured = local_store
    first = _stored(vault, "a", 8192)
    monkeypatch.setenv(originals.CONFIG_ENV, str(configured))
    if kept_for == "unexported":
        _configure((selected_store[0], configured), vault, key_version="2")
    elif kept_for == "settling":
        _configure((selected_store[0], configured), vault, eviction_settle_seconds=600)
        with closing(media_jobs.MediaJobStore(vault)._connect()) as conn, conn:
            conn.execute("UPDATE media_replicas SET verified_at = ? WHERE binary_rel = ?",
                         (time.time() - 1200, first.relative_to(vault).as_posix()))
    second = _stored(vault, "b", 8191)
    ref = originals.original_state(vault, second).reference
    assert ref is not None
    if kept_for == "unexported":
        assert ref.key_version == "2"
    elif kept_for == "quarantined":
        media_replicas.quarantine(
            vault, second.relative_to(vault).as_posix(), ref, level=media_replicas.CONTENT, failure="size_mismatch",
        )
    monkeypatch.delenv(originals.CONFIG_ENV)
    monkeypatch.setattr(shutil, "disk_usage", _allocated_volume(1_000, first, second))
    demand = _allocated(first) + _allocated(second) - 1
    assert media_store.record_demand(vault, "series", demand, budget=0, reachable_budget=demand)

    assert media_store.evict_to_target(vault) == 0
    assert first.exists() and second.exists()


def test_demand_rechecks_a_previously_proposed_original_for_a_new_pin(vault, local_store, monkeypatch):
    first = _stored(vault, "a", 8192)
    second = _stored(vault, "b", 8192)
    monkeypatch.setattr(shutil, "disk_usage", _allocated_volume(1_000, first, second))
    monkeypatch.setattr(media_store, "_EVICTION_BATCH", 1)
    demand = _allocated(first) + _allocated(second) - 1
    assert media_store.record_demand(vault, "series", demand, budget=0, reachable_budget=demand)

    assert media_store.evict_to_target(vault) == 0
    with media_store.open_original(vault, first):
        assert media_store.evict_to_target(vault) == 0
        assert media_store.evict_to_target(vault) == 0
        assert first.exists() and second.exists()

    assert media_store.evict_to_target(vault) == 0
    assert media_store.evict_to_target(vault) == 1
    assert media_store.evict_to_target(vault) == 1
    assert not first.exists() and not second.exists()


def test_demand_does_not_count_a_sparse_originals_logical_size(vault, local_store, monkeypatch):
    first = _stored(vault, "a", 8192)
    sparse_size = 16_384
    with library_scope():
        captured = preserve.preserve(
            vault, scope="Test", category="b", filename="sparse.txt", content="\0" * sparse_size,
        )
    sparse = vault / captured.path
    with sparse.open("wb") as stream:
        stream.truncate(sparse_size)
    if _allocated(sparse):
        pytest.skip("the fixture filesystem did not create a sparse file")
    assert _offload(vault, sparse) is not None
    monkeypatch.setattr(shutil, "disk_usage", _allocated_volume(1_000, first, sparse))
    demand = _allocated(first) + 1
    assert demand < sparse_size
    assert media_store.record_demand(vault, "series", demand, budget=0, reachable_budget=demand)

    assert media_store.evict_to_target(vault) == 0
    assert first.exists() and sparse.exists()


def test_demand_counts_the_blocks_of_two_shared_filings_only_once(vault, local_store, monkeypatch):
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    data = b"recorded" * 1024
    paths = []
    for filename in ("first.txt", "second.txt"):
        with library_scope():
            filed = preserve.preserve_stream(vault, scope="Test", category="pool", filename=filename, stream=io.BytesIO(data))
        path = vault / filed.path
        assert media_store.offload(vault, path) is not None
        paths.append(path)
    assert media_bindings.rebuild(vault)
    pooled = originals.original_state(vault, paths[0]).local_path
    assert pooled is not None and originals.original_state(vault, paths[1]).local_path == pooled
    capacity = _allocated(pooled)
    monkeypatch.setattr(shutil, "disk_usage", _allocated_volume(1_000, pooled))
    assert media_store.record_demand(vault, "series", capacity + 1, budget=0, reachable_budget=capacity + 1)
    assert media_store.evict_to_target(vault) == 0
    assert pooled.exists()

    assert media_store.record_demand(vault, "series", capacity, budget=0, reachable_budget=capacity)
    assert media_store.evict_to_target(vault) == 1
    assert not pooled.exists()
    assert all(originals.original_state(vault, path).state == "evicted" for path in paths)
