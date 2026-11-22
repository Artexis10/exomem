import io
import shutil
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest
from test_media_store import _configure, _stored, _volume, selected_store as selected_store
from test_personal_original_eviction import local_store as local_store

from exomem import artifact_lifecycle, import_windows, media_bindings, media_jobs, media_replicas, media_store, original_pool, originals, preserve
from exomem.governance.principal import library_scope


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


@pytest.mark.parametrize("explicit", [False, True])
def test_personal_eviction_continues_to_the_lower_threshold_after_yielding(vault, local_store, monkeypatch, explicit):
    if explicit:
        monkeypatch.setenv(originals.CONFIG_ENV, str(local_store[1]))
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


def test_personal_import_series_does_not_start_eviction_below_pressure(vault, selected_store, monkeypatch):
    original = _stored(vault, "cached", 8_192)
    _configure(selected_store, vault, growth_threshold=0.8)
    monkeypatch.setattr(shutil, "disk_usage", _volume(10**9, 10**8, original))

    report = import_windows.report(vault, series_id="new-series", next_window_bytes=1_024)
    assert report["window_budget"]["demand"]["recorded"] is False
    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        assert conn.execute("SELECT series FROM media_demands").fetchall() == []

    # A lease from an older explicitly configured install cannot authorize a personal unlink.
    assert media_store.record_demand(vault, "previous-series", 1_024, budget=0, reachable_budget=8_192)
    assert media_store.evict_to_target(vault) == 0
    assert original.exists()


def test_personal_eviction_keeps_an_object_above_the_binding_limit_and_continues(vault, local_store, monkeypatch):
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    physical = {}
    for filename, payload in (("first.txt", b"sample" * 1_024), ("second.txt", b"other" * 1_024)):
        with library_scope():
            filed = preserve.preserve_stream(vault, scope="Test", category="pool", filename=filename, stream=io.BytesIO(payload))
        assert media_store.offload(vault, vault / filed.path) is not None
        original = originals.original_state(vault, filed.path).local_path
        assert original is not None
        physical[original] = filed.path
    assert media_bindings.rebuild(vault)
    first, second = sorted(physical)
    ledger = media_jobs.MediaJobStore(vault)
    with closing(ledger._connect()) as conn, conn:
        first_key = first.relative_to(vault).as_posix()
        conn.executemany(
            "INSERT INTO media_bindings SELECT ?, companion_rel, companion_hash, object_key, sha256, size, "
            "resident_only, reference FROM media_bindings WHERE binary_rel = ? AND object_key = ?",
            ((f"synthetic/binding{index:04}.txt", physical[first], first_key) for index in range(80)),
        )
    monkeypatch.setattr(shutil, "disk_usage", _volume(100_000, 75_000, first, second))
    monkeypatch.setattr(media_store, "_EVICTION_BATCH", 1)

    assert media_store.evict_to_target(vault) == 0
    assert first.exists() and second.exists()
    with closing(ledger._connect(readonly=True)) as conn:
        status = media_store.personal_eviction_status(conn, originals.configuration(vault))
    assert status["waiting"] == "binding_proof_limit"

    assert media_store.evict_to_target(vault) == 1
    assert first.exists()
    assert not second.exists()


def test_stalled_store_probe_releases_the_vault_writer_with_the_original_held(vault, local_store, monkeypatch):
    original = _stored(vault, "cached", 8_192)
    entered, release, finished = Event(), Event(), Event()
    check = media_store.FilesystemStore.confirm_versions

    def stalled(backend, witnesses):
        entered.set()
        try:
            if release.wait(15):
                check(backend, witnesses)
        finally:
            finished.set()

    monkeypatch.setattr(media_store.FilesystemStore, "confirm_versions", stalled)
    monkeypatch.setattr(shutil, "disk_usage", _volume(100_000, 75_000, original))
    try:
        assert media_store.evict_to_target(vault) == 0
        assert entered.wait(5) and not finished.is_set()
        assert original.exists()
        with media_store._guard(vault, "test_unrelated_write"):
            (vault / "second-file.txt").write_bytes(b"another write")
        ref = originals.original_state(vault, original).reference
        assert ref is not None
        assert not media_replicas.has_quarantined(vault, (ref,))
        with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
            status = media_store.personal_eviction_status(conn, originals.configuration(vault))
        assert status["waiting"] == "store_unavailable"
    finally:
        release.set()
        assert finished.wait(5)
