"""The automatic replica boundary spends only against durable exact attempts."""

from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_media_replicas import MIRROR, _declare
from test_media_store import _offload, _preserved
from test_media_store import selected_store as selected_store
from test_original_fixity import _stored

from exomem import (
    held_fs,
    media_jobs,
    media_protection,
    media_protection_budget,
    media_store,
    originals,
    preserve,
)
from exomem.governance.principal import library_scope

DATA = b"An original for the storage policy."


def _policy(vault, selected, limit):
    values, path = selected
    values[media_protection.POLICY_FIELD] = {
        "replica_stores": [MIRROR], "minimum_verified_copies": 1, "minimum_failure_domains": 1,
        "copy_bytes_per_month": limit,
    }
    path.write_text(json.dumps({"vaults": {str(vault): values}}))
    return media_protection.owner_policy(originals.configuration(vault))


def _copy(vault, path, policy, store=MIRROR):
    return media_store.replicate(vault, path, store, policy=policy,
                                 expected_companion_hash=originals.original_state(vault, path).sidecar_hash)


def _budget(vault, *, now=None):
    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        return media_protection_budget.status(conn, now=now)


def _versions(root):
    return [path for path in root.glob("*/*") if path.is_file()]


@pytest.mark.parametrize("lost_at", ["upload", "binding"])
def test_replay_keeps_the_accepted_exact_version_and_settles_once(vault, selected_store, tmp_path, monkeypatch, lost_at):
    mirror = tmp_path / "mirror"
    _declare(vault, selected_store, mirror)
    policy = _policy(vault, selected_store, 100_000)
    path = _stored(vault, "policy", DATA)
    old_month = datetime(2026, 1, 31, 23, 59, tzinfo=UTC).timestamp()
    next_month = datetime(2026, 2, 1, tzinfo=UTC).timestamp()
    clock = SimpleNamespace(time=lambda: old_month)
    monkeypatch.setattr(media_protection_budget, "time", clock)

    with monkeypatch.context() as fault:
        if lost_at == "upload":
            send = media_store.FilesystemStore._upload_exact

            def lost_reply(*args, **kwargs):
                send(*args, **kwargs)
                raise OSError("the upload reply was lost")

            fault.setattr(media_store.FilesystemStore, "_upload_exact", lost_reply)
        else:
            def lost_binding(*_args, **_kwargs):
                assert _budget(vault).confirmed_bytes
                raise media_store.MediaStoreError(media_store.STORE_BUSY_CODE, "publication was interrupted")

            fault.setattr(media_store, "_publishing", lost_binding)
        with pytest.raises(media_store.MediaStoreError):
            _copy(vault, path, policy)
    [saved] = _versions(mirror)
    size = saved.stat().st_size
    pending = _budget(vault, now=old_month)
    assert (pending.confirmed_bytes, pending.uncertain_bytes) == ((0, size) if lost_at == "upload" else (size, 0))
    assert not originals.original_state(vault, path).replicas

    clock.time = lambda: next_month
    replica = _copy(vault, path, policy)
    assert _copy(vault, path, policy) == replica
    assert _versions(mirror) == [saved]
    assert replica.version == saved.name
    assert _budget(vault, now=old_month) == media_protection_budget.BudgetStatus("2026-01", size, 0)
    assert _budget(vault) == media_protection_budget.BudgetStatus("2026-02", 0, 0)
    sink = io.BytesIO()
    media_store.write_stored_original(vault, replica, sink, store=MIRROR, prove=True)
    assert sink.getvalue() == DATA


def test_an_absent_attempt_keeps_its_possible_spend_when_a_fresh_version_is_sent(
    vault, selected_store, tmp_path, monkeypatch,
):
    mirror = tmp_path / "mirror"
    _declare(vault, selected_store, mirror)
    original = _stored(vault, "policy", DATA)
    primary = _offload(vault, original)
    size = primary.ciphertext_size
    policy = _policy(vault, selected_store, 2 * size)
    publish = held_fs.publish_bytes
    upload = media_store.FilesystemStore._upload_exact
    versions = []

    def interrupt_upload(*args, **kwargs):
        expected = kwargs["version"]
        versions.append(expected)

        def interrupt(filesystem, parent, version, data, **options):
            if version != expected:
                return publish(filesystem, parent, version, data, **options)

            def incomplete():
                yield next(iter(data.open()))
                raise OSError("the transport stopped after writing ciphertext")

            return publish(filesystem, parent, version, held_fs.Chunks(incomplete), **options)

        with monkeypatch.context() as fault:
            fault.setattr(held_fs, "publish_bytes", interrupt)
            return upload(*args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(media_store.FilesystemStore, "_upload_exact", interrupt_upload)
        with pytest.raises(media_store.MediaStoreError, match="MEDIA_STORE_UNAVAILABLE"):
            _copy(vault, original, policy)
    assert _versions(mirror) == []
    assert _budget(vault).uncertain_bytes == size
    replica = _copy(vault, original, policy)
    assert replica.version != versions[0]
    spent = _budget(vault)
    assert (spent.confirmed_bytes, spent.uncertain_bytes) == (size, size)
    other = _stored(vault, "second", DATA)
    with pytest.raises(media_protection_budget.PolicyBudgetExceeded):
        _copy(vault, other, policy)
    assert len(_versions(mirror)) == 1


def test_zero_budget_blocks_the_policy_path_and_allows_an_independent_owner_copy(vault, selected_store, tmp_path):
    mirror = tmp_path / "mirror"
    _declare(vault, selected_store, mirror)
    policy = _policy(vault, selected_store, 0)
    path = _stored(vault, "policy", DATA)
    with pytest.raises(media_protection_budget.PolicyBudgetExceeded):
        _copy(vault, path, policy)
    assert not _versions(mirror)
    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        assert conn.execute("SELECT 1 FROM media_protections WHERE store = ?", (MIRROR,)).fetchone() is None
    manual = media_store.replicate(vault, path, MIRROR)
    assert _copy(vault, path, policy) == manual
    assert (_budget(vault).confirmed_bytes, _budget(vault).uncertain_bytes) == (0, 0)


def test_a_policy_copy_of_an_evicted_original_spends_its_existing_ciphertext_size(vault, selected_store, tmp_path):
    mirror = tmp_path / "mirror"
    _declare(vault, selected_store, mirror)
    path = _preserved(vault, category="policy")
    data = path.read_bytes()
    primary = _offload(vault, path)
    assert media_store.evict(vault, path)
    assert originals.original_state(vault, path).state == "evicted"
    policy = _policy(vault, selected_store, primary.ciphertext_size)

    replica = _copy(vault, path, policy)
    assert replica.ciphertext_sha256 == primary.ciphertext_sha256
    assert _budget(vault).confirmed_bytes == primary.ciphertext_size
    sink = io.BytesIO()
    media_store.write_stored_original(vault, replica, sink, store=MIRROR, prove=True)
    assert sink.getvalue() == data


def test_two_concurrent_transfers_share_one_durable_monthly_cap(vault, selected_store, tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    _declare(vault, selected_store, mirror)
    first = _stored(vault, "first", DATA)
    second = _stored(vault, "second", DATA)
    size = _offload(vault, first).ciphertext_size
    policy = _policy(vault, selected_store, size)
    actual_upload = media_store.FilesystemStore._upload_exact
    ready = threading.Event()
    both = threading.Barrier(2)

    def racing(*args, **kwargs):
        ready.set()
        both.wait(timeout=10)
        return actual_upload(*args, **kwargs)

    def attempt(path):
        try:
            return _copy(vault, path, policy)
        except media_protection_budget.PolicyBudgetExceeded:
            return None

    monkeypatch.setattr(media_store.FilesystemStore, "_upload_exact", racing)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(attempt, first)
        assert ready.wait(10)
        two = pool.submit(attempt, second)
        assert sum(item.result(timeout=20) is not None for item in (one, two)) == 1
    assert len(_versions(mirror)) == 1
    assert (_budget(vault).confirmed_bytes, _budget(vault).uncertain_bytes) == (size, 0)


def test_a_changed_policy_is_checked_at_the_ciphertext_send(vault, selected_store, tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    _declare(vault, selected_store, mirror)
    policy = _policy(vault, selected_store, 100_000)
    original = _stored(vault, "policy", DATA)
    send = media_store.FilesystemStore._upload_exact

    def change_policy(*args, **kwargs):
        _policy(vault, selected_store, 0)
        return send(*args, **kwargs)

    monkeypatch.setattr(media_store.FilesystemStore, "_upload_exact", change_policy)
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_ORIGINAL_CHANGED"):
        _copy(vault, original, policy)
    assert not _versions(mirror)
    assert (_budget(vault).confirmed_bytes, _budget(vault).uncertain_bytes) == (0, 0)


def test_a_filing_changed_after_transfer_keeps_the_charge_without_publishing_the_copy(
    vault, selected_store, tmp_path, monkeypatch,
):
    mirror = tmp_path / "mirror"
    _declare(vault, selected_store, mirror)
    policy = _policy(vault, selected_store, 100_000)
    original = _stored(vault, "policy", DATA)
    snapshot = originals.original_state(vault, original)
    send = media_store.FilesystemStore._upload_exact

    def change_filing(*args, **kwargs):
        version = send(*args, **kwargs)
        with library_scope():
            preserve.update_sidecar_extraction(vault, snapshot.sidecar_path, text="A new extraction.", engine="text")
        return version

    monkeypatch.setattr(media_store.FilesystemStore, "_upload_exact", change_filing)
    with pytest.raises(media_store.MediaStoreError, match="MEDIA_ORIGINAL_CHANGED"):
        _copy(vault, original, policy)
    [accepted] = _versions(mirror)
    assert _budget(vault).confirmed_bytes == accepted.stat().st_size
    assert not originals.original_state(vault, original).replicas


def test_policy_rejects_a_serving_store_before_transferring(vault, selected_store, tmp_path):
    _declare(vault, selected_store, tmp_path / "mirror")
    policy = _policy(vault, selected_store, 100_000)
    original = _stored(vault, "policy", DATA)
    with pytest.raises(media_protection_budget.PolicyTransferUnsupported):
        _copy(vault, original, policy, store=selected_store[0]["store"])


def test_the_held_upload_rejects_a_changed_store_identity_before_reserving(vault, selected_store, tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    _declare(vault, selected_store, mirror)
    policy = _policy(vault, selected_store, 100_000)
    original = _stored(vault, "policy", DATA)
    send = media_store.FilesystemStore._upload_exact

    def replace_root(*args, **kwargs):
        mirror.rename(tmp_path / "previous-mirror")
        mirror.mkdir()
        media_store.write_store_marker(mirror, MIRROR)
        return send(*args, **kwargs)

    monkeypatch.setattr(media_store.FilesystemStore, "_upload_exact", replace_root)
    with pytest.raises(media_store.MediaStoreError, match="policy store identity changed"):
        _copy(vault, original, policy)
    assert not _versions(mirror)
    assert (_budget(vault).confirmed_bytes, _budget(vault).uncertain_bytes) == (0, 0)
