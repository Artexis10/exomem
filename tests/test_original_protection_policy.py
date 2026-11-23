import io
import json
import os
import time
from contextlib import closing
from dataclasses import replace

import pytest
from test_media_policy_transfer import _policy, _versions
from test_media_replicas import MIRROR, _declare
from test_media_store import _offload
from test_media_store import selected_store as selected_store
from test_original_fixity import _stored

from exomem import (
    artifact_lifecycle,
    media_bindings,
    media_jobs,
    media_protection,
    media_protection_work,
    media_replicas,
    media_store,
    original_fixity,
    original_pool,
    originals,
    preserve,
)
from exomem import (
    vault as vault_files,
)
from exomem.governance.principal import library_scope


@pytest.mark.parametrize("selected_store", [False], indirect=True)
def test_policy_counts_only_exact_verified_versions_with_recovery_keys(vault, selected_store, tmp_path):
    _declare(vault, selected_store, tmp_path / "mirror")
    original = _stored(vault, "original", b"synthetic protected document")
    primary = _offload(vault, original)
    mirror = media_store.replicate(vault, original, MIRROR)
    media_replicas.rebuild(vault)
    config = originals.configuration(vault)
    unproved = replace(mirror, version="different-version")

    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        assert not media_replicas.verified_for_policy(conn, config, [primary, mirror])
    media_replicas.confirm_key_export(vault, "1")
    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        assert media_replicas.verified_for_policy(conn, config, [primary, mirror, unproved]) == {primary, mirror}

    media_replicas.quarantine(vault, original.relative_to(vault).as_posix(), mirror,
                              level=media_replicas.CONTENT, failure="digest_mismatch")
    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        assert media_replicas.verified_for_policy(conn, config, [primary, mirror, unproved]) == {primary}


def test_two_filings_of_one_version_do_not_supply_two_copies_or_domains():
    local = media_protection.VerifiedCopy(("local", "object"), "local-device")
    copy = media_protection.VerifiedCopy(("store", "object", "version"), "other-device")
    sibling = media_protection.VerifiedCopy(("store", "object", "next-version"), "other-device")

    assert media_protection.verified_coverage([local, copy, copy]) == media_protection.Coverage(2, 2)
    assert media_protection.verified_coverage([local, copy, sibling]) == media_protection.Coverage(3, 2)


@pytest.mark.skipif(not vault_files.STAT_GENERATION_TRUSTED, reason="requires a trusted file change time")
def test_local_policy_proof_expires_when_the_verified_file_changes(vault):
    original = _stored(vault, "local item", b"the verified bytes")
    snapshot = originals.original_state(vault, original)
    original_fixity.walk(vault)
    original_fixity.run_pass(vault, budget=4096, rate=1024**2)
    relative = original.relative_to(vault).as_posix()
    wanted = [(relative, snapshot.binary_sha256, snapshot.sidecar_path.relative_to(vault).as_posix())]
    assert original_fixity.verified_local_files(vault, wanted, deadline=time.monotonic() + 2) == {relative}
    before = original.stat()
    original.write_bytes(b"the modified bytes")
    os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not original_fixity.verified_local_files(vault, wanted, deadline=time.monotonic() + 2)


def test_policy_requires_positive_minimum_and_explicit_eviction_authority():
    config = {"store": "primary", "protection": {"replica_stores": ["primary"], "minimum_verified_copies": 1,
                                                "minimum_failure_domains": 1, "copy_bytes_per_month": 0}}
    policy = media_protection.owner_policy(config)
    assert not policy.eviction_authorized
    assert media_protection.verified_coverage([]).meets(policy) is False

    config["protection"]["minimum_verified_copies"] = 0
    with pytest.raises(media_protection.ProtectionPolicyError):
        media_protection.owner_policy(config)


def test_the_replica_worker_adds_and_reports_one_verified_policy_copy(vault, selected_store, tmp_path, monkeypatch):
    destination = tmp_path / "policy-mirror"
    _declare(vault, selected_store, destination)
    _policy(vault, selected_store, 100_000)
    original = _stored(vault, "a locally recorded item", b"An approved durable replica.")
    media_replicas.confirm_key_export(vault)
    monkeypatch.setattr(media_protection_work, "_SWEEP_INTERVAL", 0)

    first = media_replicas.status(vault)["protection"]
    assert first["state"] == "unknown"
    for _ in range(16):
        media_replicas.work(vault)
        report = media_replicas.status(vault)["protection"]
        if (report.get("last_sweep") or {}).get("met") == 1:
            break
    else:
        pytest.fail(f"the worker did not observe the configured protection: {report}")

    [replica] = originals.original_state(vault, original).replicas
    [stored] = _versions(destination)
    assert replica.version == stored.name
    assert report["last_sweep"]["unmet"] == report["last_sweep"]["unknown"] == 0
    assert report["transfer_allowance"]["confirmed_bytes"] == stored.stat().st_size


def test_a_policy_budget_refusal_does_not_hold_the_owner_copy_queue(vault, selected_store, tmp_path, monkeypatch):
    destination = tmp_path / "owner-mirror"
    _declare(vault, selected_store, destination)
    _policy(vault, selected_store, 0)
    _stored(vault, "a locally recorded item", b"An owner copy remains independent.")
    media_replicas.confirm_key_export(vault)
    monkeypatch.setattr(media_protection_work, "_SWEEP_INTERVAL", 0)

    for _ in range(6):
        media_replicas.work(vault)
    assert not _versions(destination)
    report = media_replicas.status(vault)["protection"]
    assert report["last_sweep"]["unmet"] == 1 and report["transfer_allowance"]["confirmed_bytes"] == 0
    assert report["copy_waiting"] == {MIRROR: "MEDIA_POLICY_BUDGET_EXCEEDED"}
    media_replicas.request(vault, MIRROR)
    for _ in range(6):
        media_replicas.work(vault)
        if media_replicas.progress(vault, MIRROR)["copied"] == 1:
            break
    assert media_replicas.progress(vault, MIRROR)["copied"] == 1
    assert len(_versions(destination)) == 1
    assert media_replicas.status(vault)["protection"]["transfer_allowance"]["confirmed_bytes"] == 0


@pytest.mark.parametrize("selected_store", [False], indirect=True)
@pytest.mark.parametrize("key_before_repair", [True, False], ids=["key-first", "repair-first"])
def test_managed_eviction_keeps_local_bytes_until_the_current_policy_has_a_verified_recovery(
    vault, selected_store, tmp_path, key_before_repair,
):
    _declare(vault, selected_store, tmp_path / "mirror")
    _policy(vault, selected_store, 100_000)
    data = b"The last copy remains readable through each custody transition."
    original = _stored(vault, "recorded bytes", data)
    primary = _offload(vault, original)
    mirror = media_store.replicate(vault, original, MIRROR)
    media_replicas.rebuild(vault)
    assert media_bindings.rebuild(vault)
    assert not media_store.evict(vault, original)

    values, config_path = selected_store
    values["protection"]["eviction_authorized"] = True
    config_path.write_text(json.dumps({"vaults": {str(vault): values}}))
    relative = original.relative_to(vault).as_posix()
    media_replicas.quarantine(vault, relative, mirror, level=media_replicas.CONTENT, failure="digest_mismatch")
    assert not media_store.evict(vault, original) and original.read_bytes() == data
    if key_before_repair:
        media_replicas.confirm_key_export(vault)
        assert not media_store.evict(vault, original) and original.read_bytes() == data
    replacement = media_store.recopy(vault, original, MIRROR)
    assert replacement != mirror and primary is not None
    assert media_bindings.rebuild(vault)
    if not key_before_repair:
        assert not media_store.evict(vault, original) and original.read_bytes() == data
        media_replicas.confirm_key_export(vault)
    assert media_store.evict(vault, original)
    restored = io.BytesIO()
    media_store.write_stored_original(vault, replacement, restored, store=MIRROR, prove=True)
    assert restored.getvalue() == data


def test_managed_eviction_requires_recovery_for_each_filing_of_a_shared_original(
    vault, selected_store, tmp_path, monkeypatch,
):
    _declare(vault, selected_store, tmp_path / "mirror")
    _policy(vault, selected_store, 100_000)
    values, config_path = selected_store
    values["protection"]["eviction_authorized"] = True
    config_path.write_text(json.dumps({"vaults": {str(vault): values}}))
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    data = b"Two filings share the one local copy."
    with library_scope():
        first = vault / preserve.preserve_stream(
            vault, scope="Field", category="notes", filename="first.bin", stream=io.BytesIO(data),
        ).path
        second = vault / preserve.preserve_stream(
            vault, scope="Field", category="notes", filename="second.bin", stream=io.BytesIO(data),
        ).path
    reference = _offload(vault, first)
    assert reference is not None and _offload(vault, second) == reference
    media_store.replicate(vault, first, MIRROR)
    media_replicas.rebuild(vault)
    media_replicas.confirm_key_export(vault)
    assert media_bindings.rebuild(vault)
    local = originals.original_state(vault, first).local_path
    assert local is not None and local == originals.original_state(vault, second).local_path
    assert not media_store.evict(vault, first) and local.read_bytes() == data

    media_store.replicate(vault, second, MIRROR)
    assert media_bindings.rebuild(vault)
    assert media_store.evict(vault, first) and not local.exists()
    for logical in (first, second):
        [copy] = originals.original_state(vault, logical).replicas
        restored = io.BytesIO()
        media_store.write_stored_original(vault, copy, restored, store=MIRROR, prove=True)
        assert restored.getvalue() == data
