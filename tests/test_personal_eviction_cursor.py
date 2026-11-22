import shutil
from contextlib import closing
from pathlib import Path

import pytest
from test_media_store import _configure, _stored, _volume, selected_store as selected_store
from test_personal_original_eviction import local_store as local_store

from exomem import media_jobs, media_replicas, media_store, originals


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


@pytest.mark.parametrize(("demand_bytes", "other_write", "fits"), [(900, 0, True), (902, 0, False), (900, 2, True)])
def test_personal_demand_only_evicts_after_checking_enough_cached_bytes(
    vault, local_store, monkeypatch, demand_bytes, other_write, fits,
):
    first = _stored(vault, "a", 601)
    second = _stored(vault, "b", 300)
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 1_000, first, second))
    monkeypatch.setattr(media_store, "_EVICTION_BATCH", 1)
    assert media_store.record_demand(vault, "series", demand_bytes, budget=0, reachable_budget=2_000)

    assert media_store.evict_to_target(vault) == 0
    assert first.exists() and second.exists()

    assert media_store.evict_to_target(vault) == int(fits)
    assert first.exists() is not fits and second.exists()
    # Recheck the known remainder when another write changes the shortfall.
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 1_000 + other_write, first, second))
    finishes = fits and not other_write
    assert media_store.evict_to_target(vault) == int(finishes)
    assert second.exists() is not finishes
