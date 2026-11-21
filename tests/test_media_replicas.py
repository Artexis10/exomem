"""Replicas of an original in a second store: how they are made, found, protected and used to repair.

Each case runs the real offload, replicate and fixity code on a temporary vault with two filesystem stores
and the real `age`. The data is invented.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import time
import types
from contextlib import closing
from pathlib import Path

import pytest
from test_media_store import _configure, _count_censuses, _filed, _offload, _preserved
from test_media_store import selected_store as selected_store
from test_original_fixity import DAMAGED, TRANSCRIPT, _count_downloads, _findings, _fixity_on, _stored  # noqa: F401

from exomem import (
    artifact_lifecycle,
    commands,
    held_fs,
    media_allowance,
    media_bindings,
    media_jobs,
    media_replicas,
    media_store,
    original_fixity,
    original_pool,
    originals,
)

MIRROR = "mirror"
IMMEDIATE = {"retrieval_class": "immediate", "head_recomputes": True}


def _declare(vault: Path, selected_store, mirror_root: Path, **mirror) -> None:
    """List the eviction store and a mirror in the vault's configuration; the mirror's root holds its marker."""
    values, config = selected_store
    mirror_root.mkdir(exist_ok=True)
    if media_store.read_store_marker(mirror_root) is None:
        media_store.write_store_marker(mirror_root, MIRROR)
    values["stores"] = [
        {"name": "test-store", "failure_domain": "disk-a", **IMMEDIATE},
        {"name": MIRROR, "adapter": "filesystem", "root": str(mirror_root), "failure_domain": "disk-b",
         **IMMEDIATE, **mirror},
    ]
    config.write_text(json.dumps({"vaults": {str(vault): values}}))


@pytest.fixture
def mirrored(vault: Path, tmp_path: Path, selected_store) -> Path:
    _declare(vault, selected_store, tmp_path / "mirror")
    return tmp_path / "mirror"


def _replicate_all(vault: Path, store: str = MIRROR) -> dict[str, object]:
    """Ask for replicas and run the worker's units until it has nothing left to do, as the service would."""
    media_replicas.request(vault, store)
    while media_replicas.work(vault):
        pass
    return media_replicas.progress(vault, store)


def _objects(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file() and path.name != media_store.STORE_MARKER)


def test_a_second_store_replica_restores_an_original_that_the_first_store_lost(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The point of custody: with the local file and the eviction store's version both damaged, fixity restores
    the original from the replica, decrypted and hashed against the binding, and names why the first copy failed."""
    original = _stored(vault, "Recordings")
    reference = _offload(vault, original)
    assert _replicate_all(vault) == {"store": MIRROR, "queued": 1, "copied": 1, "refused": 0, "remaining": 0,
                                     "scan": "complete"}
    snapshot = originals.original_state(vault, original)
    [replica] = snapshot.replicas
    assert (snapshot.reference, replica.store) == (reference, MIRROR)
    assert replica.object != reference.object  # a store is told nothing that links its names to another's
    [stored] = _objects(mirrored)
    assert stored.read_bytes().startswith(b"age-encryption.org/v1\n")
    assert TRANSCRIPT not in stored.read_bytes()
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    original.write_bytes(DAMAGED)
    first = Path(selected_store[0]["root"]) / reference.object / reference.version
    first.write_bytes(os.urandom(first.stat().st_size))
    assert original_fixity.run_pass(vault, budget=0).outcomes == {"restored": 1}

    assert original.read_bytes() == TRANSCRIPT
    [finding] = _findings(vault)
    [tried, used] = finding.meta["route"]
    assert (tried["source"], bool(tried["failed"])) == ("media_store", True)
    assert (used["source"], used["store"], used["object"]) == ("replica", MIRROR, replica.object)
    assert "failed" not in used


def test_an_original_whose_every_copy_is_damaged_stays_corrupt_and_is_not_overwritten(
        vault: Path, selected_store, mirrored: Path) -> None:
    original = _stored(vault, "Recordings")
    reference = _offload(vault, original)
    _replicate_all(vault)
    [replica] = originals.original_state(vault, original).replicas
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)

    original.write_bytes(DAMAGED)
    first = Path(selected_store[0]["root"]) / reference.object / reference.version
    first.write_bytes(os.urandom(first.stat().st_size))
    second = mirrored / replica.object / replica.version
    second.write_bytes(os.urandom(second.stat().st_size))
    assert original_fixity.run_pass(vault, budget=0).outcomes == {"corrupt_no_copy": 1}

    assert original.read_bytes() == DAMAGED
    [finding] = _findings(vault)
    assert [step["source"] for step in finding.meta["route"]] == ["media_store", "replica"]
    assert all(step["failed"] for step in finding.meta["route"])


def test_a_restricted_caller_is_not_told_which_store_holds_a_replica_or_under_which_key(
        vault: Path, selected_store, mirrored: Path, monkeypatch) -> None:
    from exomem.governance import egress

    original = _stored(vault, "Recordings")
    reference = _offload(vault, original)
    _replicate_all(vault)
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    original.write_bytes(DAMAGED)
    for root, copy in ((Path(selected_store[0]["root"]), reference), (mirrored, originals.original_state(vault, original).replicas[0])):
        (root / copy.object / copy.version).write_bytes(b"damaged")
    original_fixity.run_pass(vault, budget=0)
    monkeypatch.setattr(egress, "caller_restricted", lambda _root: True)

    [finding] = _findings(vault)

    [replica] = [step for step in finding.meta["route"] if step["source"] == "replica"]
    assert not {"store", "key_version", "object", "version", "path"} & replica.keys()


def test_a_replica_of_an_evicted_original_is_the_stored_ciphertext_and_needs_no_download(
        vault: Path, selected_store, mirrored: Path) -> None:
    """An evicted original has no bytes on the volume: replicating it must copy the first store's ciphertext, hashed
    against its pointer, and never hydrate the original or hold its plaintext."""
    original = _preserved(vault)
    source = original.read_bytes()
    reference = _offload(vault, original)
    assert media_store.evict(vault, original)

    assert _replicate_all(vault)["copied"] == 1

    assert not original.exists()
    snapshot = originals.original_state(vault, original)
    [replica] = snapshot.replicas
    assert snapshot.state == "evicted"
    assert (replica.key_version, replica.ciphertext_sha256) == (reference.key_version, reference.ciphertext_sha256)
    sink = io.BytesIO()
    media_store.write_stored_original(vault, replica, sink, store=MIRROR)
    assert sink.getvalue() == source


def test_a_later_edit_of_the_binding_keeps_the_replicas_it_does_not_change(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A writer keeps `replicas` entries it does not touch: eviction and hydration rewrite the same sidecar, and a
    dropped entry would orphan a ciphertext in the replica store that nothing lists."""
    original = _preserved(vault)
    _offload(vault, original)
    _replicate_all(vault)
    [listed] = originals.original_state(vault, original).replicas

    assert media_store.evict(vault, original)
    with media_store.open_original(vault, original) as hydrated:
        assert hydrated.stream.read()
    assert originals.original_state(vault, original).replicas == (listed,)


def test_a_binding_with_replicas_still_reads_as_the_pointer_alone_for_a_release_without_the_key(
        vault: Path, selected_store, mirrored: Path) -> None:
    """An older release ignores `replicas` and reads `stored_object` as before, so a downgrade keeps every original
    readable. The sidecar's pointer, with the key removed, must read as the same state."""
    from exomem.vault import parse_frontmatter, render_frontmatter_document, serialize_frontmatter

    original = _preserved(vault)
    reference = _offload(vault, original)
    _replicate_all(vault)
    sidecar = original.with_name(original.name + ".md")
    fields, body, _ = parse_frontmatter(sidecar.read_text(), strict=True)
    assert len(fields[originals.REPLICAS]) == 1
    with_replicas = originals.original_state(vault, original)

    del fields[originals.REPLICAS]
    sidecar.write_text(render_frontmatter_document(serialize_frontmatter(fields), body))
    without = originals.original_state(vault, original)

    assert (with_replicas.reference, with_replicas.state) == (without.reference, without.state) == (reference, "cached")
    assert without.replicas == ()


def test_a_root_that_does_not_name_the_store_refuses_the_copy_and_leaves_the_replicas_alone(
        vault: Path, selected_store, mirrored: Path, monkeypatch) -> None:
    """An unmounted NAS leaves an empty mount point on the vault's own disk: a copy written there would look
    protected and vanish with the disk. A root without its marker is the store being unavailable."""
    monkeypatch.setattr(media_replicas, "_RETRY_SECONDS", 0)
    first = _preserved(vault, category="a")
    _offload(vault, first)
    _replicate_all(vault)
    [listed] = originals.original_state(vault, first).replicas
    second = _preserved(vault, category="b")
    _offload(vault, second)
    (mirrored / media_store.STORE_MARKER).unlink()  # the volume is gone: the mount point is empty
    before = _objects(mirrored)

    progress = _replicate_all(vault)

    assert _objects(mirrored) == before
    assert (progress["copied"], progress["remaining"]) == (0, 1)
    assert originals.original_state(vault, second).replicas == ()
    assert originals.original_state(vault, first).replicas == (listed,)
    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.store_for(vault, MIRROR).head(listed.object, listed.version)
    assert refused.value.code == "MEDIA_STORE_UNAVAILABLE"
    assert media_replicas.status(vault)["stores"][1]["reachable"] is False


def test_a_store_names_itself_only_by_the_owner_s_declaration_and_never_on_the_vault_s_own_device(
        vault: Path, selected_store, tmp_path: Path) -> None:
    values, config = selected_store
    root = tmp_path / "attached"
    root.mkdir()
    values["stores"] = [{"name": MIRROR, "adapter": "filesystem", "root": str(root), "failure_domain": "disk-b",
                         **IMMEDIATE}]
    config.write_text(json.dumps({"vaults": {str(vault): values}}))

    with pytest.raises(media_store.MediaStoreError, match="own device"):
        media_store.declare_store(vault, MIRROR)
    assert not (root / media_store.STORE_MARKER).exists()


def test_declaring_a_store_on_another_device_writes_its_marker_once(vault: Path, selected_store) -> None:
    other = Path("/dev/shm")
    if not other.is_dir() or os.stat(other).st_dev == os.stat(vault).st_dev:
        pytest.skip("needs a second filesystem")
    root = other / f"replica-custody-test-{os.getpid()}"
    root.mkdir()
    try:
        values, config = selected_store
        values["stores"] = [{"name": MIRROR, "adapter": "filesystem", "root": str(root), "failure_domain": "disk-b",
                             **IMMEDIATE}]
        config.write_text(json.dumps({"vaults": {str(vault): values}}))
        assert media_store.declare_store(vault, MIRROR) == root
        assert media_store.declare_store(vault, MIRROR) == root  # the same declaration changes nothing
        assert media_store.read_store_marker(root) == MIRROR
        assert media_replicas.declared_store_id(vault, MIRROR) == media_store.read_store_id(root) is not None
    finally:
        for child in root.iterdir():
            child.unlink()
        root.rmdir()


def test_an_incomplete_descriptor_makes_only_that_store_unavailable_and_the_status_names_the_field(
        vault: Path, selected_store, tmp_path: Path) -> None:
    values, config = selected_store
    other = tmp_path / "other"
    other.mkdir()
    values["stores"] = [
        {"name": "test-store", "failure_domain": "disk-a", **IMMEDIATE},
        {"name": MIRROR, "adapter": "filesystem", "root": str(tmp_path / "mirror"), "retrieval_class": "immediate"},
        {"name": "far", "adapter": "filesystem", "root": str(other), "failure_domain": "site-2",
         "retrieval_class": "weekly", "head_recomputes": False},
    ]
    config.write_text(json.dumps({"vaults": {str(vault): values}}))

    entries = originals.store_entries(values)
    assert entries["test-store"].available
    assert (entries[MIRROR].available, entries[MIRROR].missing) == (False, ("failure_domain", "head_recomputes"))
    assert (entries["far"].available, entries["far"].invalid) == (False, ("retrieval_class",))
    with pytest.raises(media_store.MediaStoreError, match="failure_domain"):
        media_store.require_replica_store(vault, MIRROR)
    media_store.store_for(vault)  # the eviction store is unaffected
    [mirror] = [stats for stats in _measured(vault)["stores"] if stats["name"] == MIRROR]
    assert (mirror["available"], mirror["missing_fields"]) == (False, ["failure_domain", "head_recomputes"])


def test_a_vault_that_declares_no_store_list_reads_as_one_immediate_store(vault: Path, selected_store) -> None:
    [entry] = originals.store_entries(selected_store[0]).values()
    assert (entry.name, entry.retrieval_class, entry.failure_domain, entry.head_recomputes) == (
        "test-store", "immediate", None, None)
    assert entry.available
    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.require_replica_store(vault, "test-store")  # offload fills the store that serves eviction
    assert refused.value.code == "MEDIA_REPLICA_STORE_REFUSED"


def _measured(vault: Path) -> dict[str, object]:
    media_replicas.rebuild(vault)
    status = media_replicas.status(vault)
    assert status["state"] == "measured"
    return status


def test_the_status_counts_protection_from_the_table_and_reads_unknown_when_the_table_is_gone(
        vault: Path, selected_store, mirrored: Path) -> None:
    for category in ("a", "b"):
        _offload(vault, _preserved(vault, category=category))
    _replicate_all(vault)

    status = _measured(vault)

    assert status["totals"]["replicas"] == 4  # the two pointers and the two replicas
    assert status["totals"]["distinct_failure_domains"] == 2
    by_name = {stats["name"]: stats for stats in status["stores"]}
    assert (by_name[MIRROR]["replicas"], by_name[MIRROR]["verified_at_upload"]) == (2, 2)
    assert by_name["test-store"]["serves_eviction"] and not by_name[MIRROR]["serves_eviction"]

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("DROP TABLE media_replicas")
    unknown = media_replicas.status(vault)
    assert (unknown["state"], unknown["stores"], unknown["totals"]) == ("unknown", None, None)
    assert media_replicas.work(vault)  # the background rebuild
    rebuilt = media_replicas.status(vault)["totals"]
    assert rebuilt["replicas"] == 4
    assert (rebuilt["never_checked"], rebuilt["checks"][media_replicas.CONTENT]["replicas"]) == (4, 0)  # no history to trust


def test_the_status_reads_one_index_and_the_page_returns_originals_by_path(vault: Path, selected_store, mirrored: Path) -> None:
    for category in ("a", "b", "c"):
        _offload(vault, _preserved(vault, category=category))
    _replicate_all(vault)
    _measured(vault)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        plan = " ".join(row[3] for row in ledger.execute("EXPLAIN QUERY PLAN " + media_replicas._GROUPS, (202611,)))
    assert "COVERING INDEX media_replicas_status" in plan and "TEMP B-TREE" not in plan
    first = media_replicas.page(vault, limit=2)
    second = media_replicas.page(vault, after=first["next_after"], limit=2)
    paths = [row["path"] for row in (*first["originals"], *second["originals"])]
    assert paths == sorted(set(paths)) and len(paths) == 3
    assert (first["next_after"] is None, second["next_after"]) == (False, None)
    assert all(len(row["replicas"]) == 2 for row in first["originals"])


def test_the_drain_picks_its_next_original_from_an_index_with_no_sort(vault: Path, selected_store, mirrored: Path) -> None:
    """A sort of the pending rows for every pick is quadratic over a queue of hundreds of thousands."""
    _offload(vault, _preserved(vault))
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        plan = " ".join(row[3] for row in ledger.execute("EXPLAIN QUERY PLAN " + media_replicas._pick({"x"}), (
            media_replicas.PENDING, 0.0, "x")))
    assert "USING INDEX" in plan and "TEMP B-TREE" not in plan


def test_the_owner_s_key_export_is_what_makes_a_replica_recoverable(vault: Path, selected_store, mirrored: Path) -> None:
    """Recoverable status must not be assumed: a replica whose key lives only on a lost machine cannot be read."""
    _offload(vault, _preserved(vault))
    _replicate_all(vault)

    before = _measured(vault)["totals"]
    assert (before["recoverable"], before["not_recoverable"]) == (0, 2)
    assert media_replicas.confirm_key_export(vault) == "1"
    after = _measured(vault)["totals"]
    assert (after["recoverable"], after["not_recoverable"]) == (2, 0)

    # A vault that lists no stores says nothing about custody; a platform that holds the identity says so itself.
    assert media_replicas.recoverability({"store": "x"}, "1", {}) == (None, "key custody unknown")
    assert media_replicas.recoverability({"store": "x", originals.KEY_CUSTODY: originals.PLATFORM_CUSTODY}, "1", {}) == (True, None)


def test_failure_domains_read_unknown_when_a_store_that_holds_replicas_declares_none(
        vault: Path, selected_store, mirrored: Path) -> None:
    """Two stores may be one disk: counting only the declared domains would report a lower bound as a number."""
    _offload(vault, _preserved(vault))
    _replicate_all(vault)
    assert _measured(vault)["totals"]["distinct_failure_domains"] == 2

    values, config = selected_store
    values["stores"][1].pop("failure_domain")
    config.write_text(json.dumps({"vaults": {str(vault): values}}))
    totals = media_replicas.status(vault)["totals"]

    assert (totals["distinct_failure_domains"], totals["stores_without_failure_domain"]) == (None, 1)


def test_the_status_keeps_a_replica_known_not_to_survive_apart_from_one_whose_custody_is_unstated(
        vault: Path, selected_store, mirrored: Path) -> None:
    _offload(vault, _preserved(vault))
    _replicate_all(vault)
    [personal] = [s for s in _measured(vault)["stores"] if s["name"] == MIRROR]
    assert (personal["not_recoverable_reasons"], personal["recoverability_unknown_reasons"]) == (["key not exported"], [])

    values, config = selected_store
    del values["stores"]  # a vault that lists no stores states nothing about who holds the key
    config.write_text(json.dumps({"vaults": {str(vault): values}}))
    [cloud] = [s for s in media_replicas.status(vault)["stores"] if s["name"] == MIRROR]
    assert (cloud["not_recoverable"], cloud["recoverability_unknown"]) == (0, 1)
    assert (cloud["not_recoverable_reasons"], cloud["recoverability_unknown_reasons"]) == ([], ["key custody unknown"])


def test_the_status_reports_the_workers_observation_of_a_store_and_never_reads_the_store(
        vault: Path, selected_store, mirrored: Path, monkeypatch) -> None:
    media_replicas.observe(vault)
    monkeypatch.setattr(media_store.FilesystemStore, "require_identity",
                        lambda self: (_ for _ in ()).throw(AssertionError("status read a store")))
    media_replicas.rebuild(vault)

    [mirror] = [s for s in media_replicas.status(vault)["stores"] if s["name"] == MIRROR]

    assert mirror["reachable"] is True and mirror["reachable_observed_at"] is not None


def test_a_publication_made_during_a_rebuild_survives_it_whatever_the_clock_says(
        vault: Path, selected_store, mirrored: Path, monkeypatch) -> None:
    """Staleness is a rebuild generation, not a timestamp: a wall clock that steps back must not drop a fresh row."""
    original = _preserved(vault)
    _offload(vault, original)
    real_walk = media_store.bound_originals
    fresh = originals.StoredObject(1, MIRROR, "1", "o" * 64, "v1", 10, "c" * 64)

    def walk_with_a_publication(*args, **kwargs):
        yield from real_walk(*args, **kwargs)
        monkeypatch.setattr(media_replicas.time, "time", lambda: 1.0)  # the clock steps back mid-walk
        media_replicas.record(vault, "Knowledge Base/Recordings/new.m4a", fresh, verified_at=1.0)

    monkeypatch.setattr(media_store, "bound_originals", walk_with_a_publication)
    media_replicas.rebuild(vault)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        rows = ledger.execute("SELECT binary_rel, store FROM media_replicas ORDER BY binary_rel, store").fetchall()
    assert ("Knowledge Base/Recordings/new.m4a", MIRROR) in rows


def test_a_retry_after_a_lost_upload_reply_reuses_the_replica_name(vault: Path, selected_store, mirrored: Path, monkeypatch) -> None:
    """A fresh name per attempt would leave a landed first attempt under a name that no binding lists."""
    monkeypatch.setattr(media_replicas, "_RETRY_SECONDS", 0)
    for round_ in ("check", "sample"):  # a check or sample round counts as work, which would retry at once
        monkeypatch.setattr(media_replicas, round_, lambda *args, **kwargs: 0)
    original = _preserved(vault)
    _offload(vault, original)
    upload = media_store.FilesystemStore.upload
    landed: list[str] = []

    def lose_the_reply(self, name, stream, *, size, sha256):
        result = upload(self, name, stream, size=size, sha256=sha256)
        if self.root == mirrored and not landed:
            landed.append(name)
            raise media_store.MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the reply was lost")
        return result

    monkeypatch.setattr(media_store.FilesystemStore, "upload", lose_the_reply)
    _replicate_all(vault)
    assert originals.original_state(vault, original).replicas == ()  # the first attempt did not publish
    while media_replicas.work(vault):  # the worker's retry, after its delay
        pass

    [replica] = originals.original_state(vault, original).replicas
    assert replica.object == landed[0]
    assert {path.parent.name for path in _objects(mirrored)} == {landed[0]}


def test_one_drain_settles_a_bounded_batch_and_an_unavailable_store_pauses_its_queue(
        vault: Path, selected_store, mirrored: Path, monkeypatch) -> None:
    monkeypatch.setattr(media_replicas, "_DRAIN_BATCH", 2)
    monkeypatch.setattr(media_replicas, "_RETRY_SECONDS", 0)
    for category in ("a", "b", "c"):
        _offload(vault, _preserved(vault, category=category))
    media_replicas.request(vault, MIRROR)
    assert media_replicas.work(vault)  # the rebuild
    assert media_replicas.work(vault)  # the scan
    assert media_replicas.progress(vault, MIRROR)["queued"] == 3

    assert media_replicas.drain(vault) == 2
    assert media_replicas.progress(vault, MIRROR) == {"store": MIRROR, "queued": 3, "copied": 2, "refused": 0,
                                                       "remaining": 1, "scan": "complete"}
    (mirrored / media_store.STORE_MARKER).unlink()
    assert media_replicas.drain(vault) == 0
    progress = media_replicas.progress(vault, MIRROR)
    assert (progress["copied"], progress["remaining"]) == (2, 1)


def test_a_worker_round_that_settles_copies_still_runs_the_checks_that_are_due(
        vault: Path, selected_store, mirrored: Path, monkeypatch) -> None:
    """A copy backlog that never empties must not suspend verification: the replicas it made would stay unchecked."""
    monkeypatch.setattr(media_replicas, "_DRAIN_BATCH", 1)
    for category in ("a", "b"):
        _offload(vault, _preserved(vault, category=category))
    media_replicas.request(vault, MIRROR)
    media_replicas.work(vault)  # the rebuild
    media_replicas.work(vault)  # the scan

    assert media_replicas.work(vault)  # one copy settles, one is still queued

    assert media_replicas.progress(vault, MIRROR)["remaining"] == 1
    assert _replica_row(vault)["checked_at"] > 0


def test_a_repeated_replicate_keeps_the_progress_of_a_running_request_and_starts_a_new_one_when_it_ends(
        vault: Path, selected_store, mirrored: Path, monkeypatch) -> None:
    monkeypatch.setattr(media_replicas, "_DRAIN_BATCH", 1)
    for category in ("a", "b"):
        _offload(vault, _preserved(vault, category=category))
    media_replicas.request(vault, MIRROR)
    media_replicas.work(vault)  # the rebuild
    media_replicas.work(vault)  # the scan
    assert media_replicas.drain(vault) == 1
    running = media_replicas.progress(vault, MIRROR)
    assert (running["copied"], running["remaining"]) == (1, 1)

    assert media_replicas.request(vault, MIRROR) == running  # nothing wiped, no new scan
    assert media_replicas.progress(vault, MIRROR)["scan"] == "complete"

    while media_replicas.work(vault):
        pass
    assert media_replicas.progress(vault, MIRROR)["copied"] == 2
    assert media_replicas.request(vault, MIRROR)["scan"] == "running"  # a finished request is asked again from the start


def test_a_malformed_connection_field_makes_only_that_store_unavailable(
        vault: Path, selected_store, mirrored: Path) -> None:
    values, config = selected_store
    values["stores"] = [
        {"name": "test-store", "failure_domain": "disk-a", **IMMEDIATE},
        {"name": MIRROR, "adapter": "filesystem", "root": str(mirrored), "failure_domain": "disk-b",
         "max_object_bytes": "ten gigabytes", **IMMEDIATE},
    ]
    config.write_text(json.dumps({"vaults": {str(vault): values}}))
    original = _preserved(vault)
    _offload(vault, original)  # the store that serves eviction still works

    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.replicate(vault, original, MIRROR)
    assert refused.value.code == "MEDIA_STORE_UNAVAILABLE"
    assert media_store.store_reachable(vault, MIRROR) is False
    [mirror] = [s for s in _measured(vault)["stores"] if s["name"] == MIRROR]
    assert mirror["available"] is False
    assert "connection fields are invalid" in mirror["unavailable_reason"]


def test_a_replica_entry_that_cannot_name_an_object_is_a_failed_candidate_and_not_an_aborted_pass(
        vault: Path, selected_store, mirrored: Path) -> None:
    original = _stored(vault, "Recordings")
    _offload(vault, original)
    _replicate_all(vault)
    sidecar = original.with_name(original.name + ".md")
    text = sidecar.read_text()
    [replica] = originals.original_state(vault, original).replicas
    sidecar.write_text(text.replace(replica.version, "a/b"))
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    original.write_bytes(DAMAGED)
    first = originals.original_state(vault, original).reference
    damaged_first = Path(selected_store[0]["root"]) / first.object / first.version
    damaged_first.write_bytes(os.urandom(damaged_first.stat().st_size))

    assert original_fixity.run_pass(vault, budget=0).outcomes == {"corrupt_no_copy": 1}

    [finding] = _findings(vault)
    failed = [step for step in finding.meta["route"] if step["source"] == "replica"]
    assert [step["failed"] for step in failed] == ["ValueError"]


@pytest.mark.parametrize("store_present", [True, False])
def test_replication_distinguishes_a_lost_version_from_an_unavailable_store(
        vault: Path, selected_store, mirrored: Path, store_present: bool) -> None:
    original = _preserved(vault)
    pointer = _offload(vault, original)
    assert media_store.evict(vault, original)
    root = Path(selected_store[0]["root"])
    _stored_file(root, pointer).unlink()
    if not store_present:
        (root / media_store.STORE_MARKER).unlink()

    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.replicate(vault, original, MIRROR)

    row = _replica_row(vault, store=pointer.store)
    if store_present:
        assert refused.value.code == media_store.NO_VERIFIED_SOURCE
        assert (row["state"], row["failure"]) == (media_replicas.QUARANTINED, "absent")
    else:
        assert refused.value.code == "MEDIA_STORE_UNAVAILABLE"
        assert (row["state"], row["failure"]) == (media_replicas.UPLOAD_VERIFIED, None)


@pytest.mark.parametrize("source_locator_valid", [True, False])
def test_replication_reads_only_the_source_needed_for_one_target_attempt(
        vault: Path, selected_store, mirrored: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        source_locator_valid: bool) -> None:
    original = _preserved(vault)
    pointer = _offload(vault, original)
    _replicate_all(vault)
    assert media_store.evict(vault, original)
    target = tmp_path / "target"
    target.mkdir()
    media_store.write_store_marker(target, "target")
    values, config = selected_store
    values["stores"].append({"name": "target", "adapter": "filesystem", "root": str(target),
                             "failure_domain": "disk-c", **IMMEDIATE})
    config.write_text(json.dumps({"vaults": {str(vault): values}}))
    if not source_locator_valid:
        sidecar = original.with_name(original.name + ".md")
        sidecar.write_text(sidecar.read_text().replace(pointer.version, "a/b"))
    real_open = media_store.FilesystemStore.open
    real_upload = media_store.FilesystemStore.upload
    reads: list[str] = []
    attempts: list[str] = []

    def counted(store, name, version):
        reads.append(store.name)
        return real_open(store, name, version)

    def unavailable(store, *args, **kwargs):
        if store.name == "target":
            attempts.append(store.name)
            raise OSError("target is offline")
        return real_upload(store, *args, **kwargs)

    monkeypatch.setattr(media_store.FilesystemStore, "open", counted)
    monkeypatch.setattr(media_store.FilesystemStore, "upload", unavailable)

    with pytest.raises(media_store.MediaStoreError, match="target is offline"):
        media_store.replicate(vault, original, "target")

    assert reads == ([pointer.store] if source_locator_valid else [MIRROR])
    assert attempts == ["target"]
    assert [replica.store for replica in originals.original_state(vault, original).replicas] == [MIRROR]


def test_a_declared_media_edge_store_has_no_connection_of_its_own(vault: Path, selected_store, mirrored: Path) -> None:
    """The edge is one connection for the process: a second declared store of that adapter would be the same place."""
    values, config = selected_store
    values["stores"] = [
        {"name": "test-store", "failure_domain": "disk-a", **IMMEDIATE},
        {"name": "edge-two", "adapter": "media-edge", "failure_domain": "disk-c", **IMMEDIATE},
    ]
    config.write_text(json.dumps({"vaults": {str(vault): values}}))

    with pytest.raises(media_store.MediaStoreError, match="no connection of its own"):
        media_store.require_replica_store(vault, "edge-two")
    with pytest.raises(media_store.MediaStoreError, match="no connection of its own"):
        media_store.store_for(vault, "edge-two")
    assert media_store.replica_unusable(
        vault, originals.StoredObject(1, "edge-two", "1", "o" * 64, "v1", 10, "c" * 64)) == "store_unavailable"
    [edge] = [stats for stats in _measured(vault)["stores"] if stats["name"] == "edge-two"]
    assert (edge["available"], "no connection of its own" in edge["unavailable_reason"]) == (False, True)


def test_a_root_with_another_stores_marker_id_is_not_served_once_the_owner_recorded_the_id(
        vault: Path, selected_store, mirrored: Path) -> None:
    """Two disks that carry the same store name are told apart by the id that the declaration recorded."""
    original = _preserved(vault)
    _offload(vault, original)
    media_replicas.record_store_id(vault, MIRROR, media_store.read_store_id(mirrored))
    assert media_store.store_reachable(vault, MIRROR) is True

    (mirrored / media_store.STORE_MARKER).unlink()
    media_store.write_store_marker(mirrored, MIRROR)  # another disk of the same name: a new random id

    assert media_store.store_reachable(vault, MIRROR) is False
    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.replicate(vault, original, MIRROR)
    assert refused.value.code == "MEDIA_STORE_UNAVAILABLE"


def test_a_busy_vault_boundary_ends_the_round_and_spends_no_attempt(
        vault: Path, selected_store, mirrored: Path, monkeypatch) -> None:
    _offload(vault, _preserved(vault, category="a"))
    media_replicas.request(vault, MIRROR)
    media_replicas.work(vault)
    media_replicas.work(vault)

    def busy(*_args, **_kwargs):
        raise media_store.MediaStoreError(media_store.STORE_BUSY_CODE, "the vault boundary is busy")

    monkeypatch.setattr(media_store, "replicate", busy)
    for _ in range(media_replicas._MAX_ATTEMPTS + 1):
        assert media_replicas.drain(vault) == 0
    assert media_replicas.progress(vault, MIRROR)["remaining"] == 1  # not refused after the attempt limit


def test_a_store_that_does_not_answer_is_checked_before_any_work_and_spends_no_attempt(
        vault: Path, selected_store, mirrored: Path, monkeypatch) -> None:
    """An unmounted store must cost no hashing, encryption or download, and must not walk the queue to refusal."""
    original = _preserved(vault, category="a")
    _offload(vault, original)
    media_replicas.request(vault, MIRROR)
    media_replicas.work(vault)
    media_replicas.work(vault)
    (mirrored / media_store.STORE_MARKER).unlink()
    worked: list[str] = []
    for name in ("_pin", "_upload_encrypted", "_copy_ciphertext"):
        real = getattr(media_store, name)
        monkeypatch.setattr(media_store, name, lambda *a, _name=name, _real=real, **k: worked.append(_name) or _real(*a, **k))

    for _ in range(media_replicas._MAX_ATTEMPTS + 1):
        assert media_replicas.drain(vault) == 0
    with pytest.raises(media_store.MediaStoreError) as direct:
        media_store.replicate(vault, original, MIRROR)  # a direct call is refused as early

    assert worked == [] and direct.value.code == "MEDIA_STORE_UNAVAILABLE"
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        assert ledger.execute("SELECT state, attempts, code FROM media_replica_queue").fetchall() == [("pending", 0, None)]
    assert originals.original_state(vault, original).replicas == ()


def test_a_replica_that_the_binding_no_longer_lists_is_not_read_by_repair(vault: Path, selected_store, mirrored: Path) -> None:
    """Repair reads a replica only when the store is available, immediate and its key version is readable here;
    anything else is named in the route for the owner and never tried."""
    from exomem.originals import StoredObject

    foreign = StoredObject(1, MIRROR, "2", "o" * 64, "v1", 10, "c" * 64)
    assert media_store.replica_unusable(vault, foreign) == "identity_unreadable"
    assert media_store.replica_unusable(vault, StoredObject(1, "absent", "1", "o" * 64, "v1", 10, "c" * 64)) == "store_unavailable"
    _declare(vault, selected_store, mirrored, retrieval_class="hours")
    assert media_store.replica_unusable(vault, StoredObject(1, MIRROR, "1", "o" * 64, "v1", 10, "c" * 64)) == "owner_restores"


def test_replicate_is_the_owner_s_and_reports_progress_while_status_shows_the_replicas(
        vault: Path, selected_store, mirrored: Path) -> None:
    from test_governance_egress import _external, _reset_governance_caches, write_rule, write_scope

    from exomem import writer_lease
    from exomem.cli_ops import OpError
    from exomem.governance.principal import request_scope

    original = _preserved(vault)
    _offload(vault, original)
    started = commands.op_process_media(vault, operation="replicate", store=MIRROR)
    assert (started["operation"], started["store"], started["scan"]) == ("replicate", MIRROR, "running")
    while media_replicas.work(vault):
        pass
    status = commands.op_process_media(vault, operation="status")
    assert status["replicas"]["totals"]["replicas"] == 2
    exact = commands.op_process_media(vault, operation="status", path=original.relative_to(vault).as_posix())
    assert [replica["store"] for replica in exact["results"][0]["replicas"]["replicas"]] == ["test-store", MIRROR]
    with pytest.raises(OpError) as unknown:
        commands.op_process_media(vault, operation="replicate", store="absent")
    assert unknown.value.code == "MEDIA_REPLICA_STORE_UNKNOWN"
    with pytest.raises(OpError) as scoped:
        commands.op_process_media(vault, operation="replicate", store=MIRROR, path="Knowledge Base/x")
    assert scoped.value.code == "MEDIA_REPLICATE_SCOPE"

    write_scope(vault)
    write_rule(vault, ceiling=0)
    _reset_governance_caches()
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "process_media")
    with request_scope(_external()):
        listing = writer_lease.invoke_command(command, vault, operation="status")
        with pytest.raises(OpError) as other:
            writer_lease.invoke_command(command, vault, operation="replicate", store=MIRROR)
    assert "replicas" not in listing
    assert other.value.code == "MEDIA_OWNER_ONLY"


# --- verify-stored-replicas: the check columns ---------------------------------------------------------


def _replica_row(vault: Path, store: str = MIRROR) -> sqlite3.Row:
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        ledger.row_factory = sqlite3.Row
        return ledger.execute("SELECT * FROM media_replicas WHERE store = ?", (store,)).fetchone()


def _replica_row_of(vault: Path, path: Path, store: str) -> sqlite3.Row:
    """The row of one filing in one store."""
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        ledger.row_factory = sqlite3.Row
        return ledger.execute("SELECT * FROM media_replicas WHERE binary_rel = ? AND store = ?",
                              (path.relative_to(vault).as_posix(), store)).fetchone()


def _replicated(vault: Path) -> tuple[str, originals.StoredObject]:
    """One evicted original with a replica in the mirror: its binding path and that replica's entry."""
    original = _preserved(vault)
    _offload(vault, original)
    _replicate_all(vault)
    snapshot = originals.original_state(vault, original)
    return original.relative_to(vault).as_posix(), next(entry for entry in snapshot.replicas if entry.store == MIRROR)


def test_a_rebuild_keeps_the_quarantine_of_a_replica_whose_entry_is_unchanged(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A rebuild that forgot the finding would show the owner two safe copies when one is known bad."""
    relative, replica = _replicated(vault)
    media_replicas.quarantine(vault, relative, replica, level=media_replicas.CONTENT, failure="digest_mismatch")

    media_replicas.rebuild(vault)

    row = _replica_row(vault)
    assert (row["state"], row["level"], row["failure"]) == (media_replicas.QUARANTINED, media_replicas.CONTENT,
                                                            "digest_mismatch")


def test_a_new_version_of_a_replica_starts_unchecked_and_unquarantined(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The copy that replaces a bad replica is a different object: it must not inherit the bad one's verdict."""
    relative, replica = _replicated(vault)
    media_replicas.quarantine(vault, relative, replica, level=media_replicas.CONTENT, failure="digest_mismatch")
    newer = originals.StoredObject(1, MIRROR, replica.key_version, replica.object, "newer-version",
                                   replica.ciphertext_size, replica.ciphertext_sha256)

    media_replicas.record(vault, relative, newer, verified_at=5.0)

    row = _replica_row(vault)
    assert (row["version"], row["state"], row["level"], row["failure"], row["checked_at"]) == (
        "newer-version", media_replicas.UPLOAD_VERIFIED, None, None, 0)


def test_quarantining_an_entry_that_the_table_already_replaced_leaves_the_newer_replica_alone(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A check that finished after a re-copy published must not condemn the copy that replaced its subject."""
    relative, replica = _replicated(vault)
    newer = originals.StoredObject(1, MIRROR, replica.key_version, replica.object, "newer-version",
                                   replica.ciphertext_size, replica.ciphertext_sha256)
    media_replicas.record(vault, relative, newer, verified_at=5.0)

    media_replicas.quarantine(vault, relative, replica, level=media_replicas.CONTENT, failure="absent")

    row = _replica_row(vault)
    assert (row["version"], row["state"]) == ("newer-version", media_replicas.UPLOAD_VERIFIED)


def test_a_table_of_an_earlier_release_gains_the_check_columns_and_keeps_its_rows(
        vault: Path, selected_store, mirrored: Path) -> None:
    """An upgrade that met the old table with `no such column` would stop the replica worker for good."""
    relative, replica = _replicated(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("DROP TABLE media_replicas")
        ledger.execute("DROP TABLE media_replica_queue")
        ledger.execute("CREATE TABLE media_replicas (binary_rel TEXT NOT NULL, store TEXT NOT NULL, object TEXT NOT NULL, "
                       "version TEXT NOT NULL, key_version TEXT NOT NULL, verified_at REAL, state TEXT NOT NULL, "
                       "seen INTEGER NOT NULL, PRIMARY KEY (binary_rel, store)) WITHOUT ROWID")
        ledger.execute("INSERT INTO media_replicas VALUES (?, ?, ?, ?, '1', 7.0, 'verified-at-upload', 1)",
                       (relative, MIRROR, replica.object, replica.version))
        ledger.execute("CREATE TABLE media_replica_queue (store TEXT NOT NULL, binary_rel TEXT NOT NULL, state TEXT NOT NULL, "
                       "code TEXT, attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, "
                       "PRIMARY KEY (store, binary_rel)) WITHOUT ROWID")
        media_replicas.initialize(ledger)

    row = _replica_row(vault)
    assert (row["verified_at"], row["state"], row["checked_at"], row["level"], row["size"]) == (
        7.0, media_replicas.UPLOAD_VERIFIED, 0, None, 0)
    assert media_replicas.status(vault)["state"] == "unknown"  # no sizes until the next rebuild writes them
    assert media_replicas.drain(vault) == 0  # the queue table gained its `kind` and `token` columns
    assert row["ciphertext_sha256"] is None  # the digest of the stored ciphertext waits for the rebuild too

    media_replicas.rebuild(vault)

    assert _replica_row(vault)["ciphertext_sha256"] == replica.ciphertext_sha256


# --- verify-stored-replicas: the check round -----------------------------------------------------------


def _check_levels(vault: Path) -> dict[str, object]:
    """Replicate one evicted original into the mirror, run one check round, and read the mirror's row."""
    _replicated(vault)
    media_replicas.rebuild(vault)
    media_replicas.check(vault)
    row = _replica_row(vault)
    return {"state": row["state"], "level": row["level"], "checked": row["checked_at"] > 0}


@pytest.mark.parametrize(("descriptor", "level"), [
    ({"head_recomputes": True}, media_replicas.CONTENT),
    ({"head_recomputes": False}, media_replicas.PRESENCE),
])
def test_a_check_proves_what_the_descriptor_says_the_store_can_prove(
        vault: Path, selected_store, tmp_path: Path, descriptor: dict[str, bool], level: str) -> None:
    """A store that only echoes the digest it was given must never be reported as content-verified."""
    _declare(vault, selected_store, tmp_path / "mirror", **descriptor)

    assert _check_levels(vault) == {"state": media_replicas.UPLOAD_VERIFIED, "level": level, "checked": True}


def test_a_store_with_no_declared_descriptor_is_never_checked(vault: Path, selected_store) -> None:
    """A Cloud cell's single store declares no `stores` list: a check of unknown strength would claim a proof."""
    _offload(vault, _preserved(vault))
    media_replicas.rebuild(vault)

    assert media_replicas.check(vault) == 0

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        assert ledger.execute("SELECT checked_at, level FROM media_replicas").fetchall() == [(0.0, None)]


def test_a_store_that_does_not_answer_as_itself_changes_no_replica_when_its_check_is_due(
        vault: Path, selected_store, mirrored: Path) -> None:
    """An unmounted NAS reads as an empty store; treating that as absence would quarantine every replica on it."""
    _replicated(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("UPDATE media_replicas SET checked_at = 0, check_from = 0, level = NULL")  # due, as a month from now would be
    (mirrored / media_store.STORE_MARKER).unlink()

    media_replicas.check(vault)

    row = _replica_row(vault)
    assert (row["state"], row["level"], row["checked_at"]) == (media_replicas.UPLOAD_VERIFIED, None, 0)


def _unmount_when_held(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Make `root` an empty mount point the moment the store takes its hold on it: the volume left after an earlier check."""
    acquire = media_store.held_fs.acquire

    def vanishing(path, *args, **kwargs):
        if Path(path) == root and (root / media_store.STORE_MARKER).exists():
            root.rename(root.with_name(root.name + "-gone"))
            root.mkdir()
        return acquire(path, *args, **kwargs)

    monkeypatch.setattr(media_store.held_fs, "acquire", vanishing)


def test_a_store_that_leaves_between_its_identity_and_its_head_is_not_read_as_missing_a_version(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The empty mount point answers every HEAD with absent: a quarantine on that has no way back once the NAS returns."""
    _replicated(vault)
    _due_again(vault)
    _unmount_when_held(monkeypatch, mirrored)

    media_replicas.check(vault)

    row = _replica_row(vault)
    assert (row["state"], row["failure"], row["level"], row["checked_at"]) == (media_replicas.UPLOAD_VERIFIED, None, None, 0)


@pytest.mark.parametrize("operation", ["head", "open", "upload"])
def test_a_store_that_leaves_while_it_is_held_is_unavailable_to_every_operation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    """A put into the empty mount point would land on the wrong disk and read as stored; a get or head would read as gone."""
    root = tmp_path / "store"
    root.mkdir()
    media_store.write_store_marker(root, "solo")
    backend = media_store.FilesystemStore(root, name="solo")
    data = b"bytes an owner handed in"
    digest = hashlib.sha256(data).hexdigest()
    version = backend.upload(digest, io.BytesIO(data), size=len(data), sha256=digest)

    def open_it() -> None:
        with backend.open(digest, version):
            pass

    operations = {"head": lambda: backend.head(digest, version), "open": open_it,
                  "upload": lambda: backend.upload(digest, io.BytesIO(data), size=len(data), sha256=digest)}
    _unmount_when_held(monkeypatch, root)

    with pytest.raises(media_store.MediaStoreError) as refused:
        operations[operation]()

    assert refused.value.code == "MEDIA_STORE_UNAVAILABLE"
    assert list(root.iterdir()) == []  # nothing was written where the store is not


def test_a_check_round_takes_the_replicas_checked_longest_ago_first_and_stops_at_its_share_of_the_cycle(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A round that checked the same replicas every time would leave the rest of the store unchecked for good."""
    for category in ("a", "b", "c"):
        _offload(vault, _preserved(vault, category=category))
    _replicate_all(vault)
    media_replicas.rebuild(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        paths = [row[0] for row in ledger.execute("SELECT binary_rel FROM media_replicas WHERE store = ? ORDER BY binary_rel", (MIRROR,))]
        for age, path in enumerate(paths):  # the second path has waited longest
            ledger.execute("UPDATE media_replicas SET checked_at = 0, check_from = ? WHERE binary_rel = ? AND store = ?",
                           (100.0 + (age - 1) ** 2, path, MIRROR))
    monkeypatch.setattr(media_replicas, "_CHECK_FLOOR_ROWS", 1)  # one replica per round, as a large store's share is

    media_replicas.check(vault)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        checked = dict(ledger.execute("SELECT binary_rel, checked_at > 1000 FROM media_replicas WHERE store = ?", (MIRROR,)))
    assert [checked[path] for path in paths] == [0, 1, 0]


def test_a_check_pick_reads_the_due_index_in_order_with_no_sort(vault: Path, selected_store, mirrored: Path) -> None:
    """A sort of a million rows for every pick would make each round cost the size of the store."""
    _replicated(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        plan = " ".join(row[3] for row in ledger.execute("EXPLAIN QUERY PLAN " + media_replicas._DUE, (MIRROR, 0.0, 8)))
    assert "USING INDEX media_replicas_due" in plan and "TEMP B-TREE" not in plan


# --- verify-stored-replicas: the sampled check ---------------------------------------------------------


def _unproven(vault: Path) -> None:
    """Forget every sample, as a replica that no round has decrypted yet."""
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("UPDATE media_replicas SET decrypted_at = 0, sample_from = 0, sample_month = 0")
        ledger.execute("DELETE FROM media_replica_samples")


def _sampled(vault: Path) -> list[tuple[str, int]]:
    """Each mirror replica's path and whether a sample has decrypted it."""
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        return [(path, proven > 0) for path, proven in ledger.execute(
            "SELECT binary_rel, decrypted_at FROM media_replicas WHERE store = ? ORDER BY binary_rel", (MIRROR,))]


def test_a_sample_that_decrypts_to_the_binding_proves_the_key(vault: Path, selected_store, mirrored: Path) -> None:
    """Only a decrypt shows that the key still opens a replica; a hash of its ciphertext cannot."""
    _replicated(vault)
    _due_again(vault)  # no recorded HEAD proof for either version
    _unproven(vault)

    assert media_replicas.sample(vault) == 2  # the eviction store's pointer and the mirror's replica

    assert [proven for _path, proven in _sampled(vault)] == [True]
    assert _replica_row(vault)["state"] == media_replicas.UPLOAD_VERIFIED
    assert media_replicas.status(vault)["totals"]["never_checked"] == 0


def test_a_sample_writes_no_plaintext_to_disk(vault: Path, selected_store, mirrored: Path, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """The sample's whole point is a hash: plaintext of a private original must not outlive it in a temporary file."""
    _replicated(vault)
    _unproven(vault)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("tempfile.tempdir", None)
    before = sorted(path for path in vault.rglob("*"))

    assert media_replicas.sample(vault) == 2

    assert list(scratch.iterdir()) == [] and sorted(path for path in vault.rglob("*")) == before


def test_a_store_that_spent_its_monthly_budget_is_not_sampled_until_the_next_month(
        vault: Path, selected_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget is the owner's egress bill: a sample that ignored it would spend a remote store's allowance in a day."""
    mirror_root = tmp_path / "mirror"
    _declare(vault, selected_store, mirror_root)
    paths = [_preserved(vault, category=category) for category in ("a", "b", "c")]
    for path in paths:
        _offload(vault, path)
    _replicate_all(vault)
    one_replica = originals.original_state(vault, paths[0]).replicas[0].ciphertext_size
    _declare(vault, selected_store, mirror_root, sample_bytes_per_month=one_replica)
    _unproven(vault)

    media_replicas.sample(vault)
    media_replicas.sample(vault)
    assert [proven for _path, proven in _sampled(vault)].count(True) == 1  # one replica fits the month

    monkeypatch.setattr(media_replicas, "_month", lambda moment: 209912)
    media_replicas.sample(vault)
    assert [proven for _path, proven in _sampled(vault)].count(True) == 2  # the next month starts a new budget


@pytest.mark.parametrize(("written", "budget", "available"), [
    pytest.param(None, 1024**3, True, id="absent-is-the-default-of-1-GiB"),
    pytest.param(0, 0, True, id="zero-turns-sampling-off-and-is-not-the-default"),
    pytest.param(5_000_000, 5_000_000, True, id="the-owners-figure"),
    pytest.param(2**63 - 1, 2**63 - 1, True, id="largest-ledger-byte-count"),
    pytest.param(2**63, 1024**3, False, id="byte-count-overflows-the-ledger"),
    pytest.param(-1, 1024**3, False, id="negative-makes-the-store-unavailable"),
    pytest.param("1GiB", 1024**3, False, id="text-makes-the-store-unavailable"),
    pytest.param(True, 1024**3, False, id="a-boolean-is-not-a-byte-count"),
])
def test_the_sample_budget_field_is_optional_and_a_wrong_value_is_shown_not_guessed(
        vault: Path, selected_store, tmp_path: Path, written, budget: int, available: bool) -> None:
    """A budget of 0 read as absent would spend a store's egress that the owner switched off; a wrong value must show."""
    values, _config = selected_store
    extra = {} if written is None else {"sample_bytes_per_month": written}
    _declare(vault, selected_store, tmp_path / "mirror", **extra)

    mirror = originals.store_entries(values)[MIRROR]

    assert (mirror.available, mirror.sample_bytes_per_month if available else None) == (available,
                                                                                         budget if available else None)
    assert mirror.invalid == (() if available else ("sample_bytes_per_month",))


def test_a_budget_of_zero_turns_sampling_off_for_that_store(vault: Path, selected_store, tmp_path: Path) -> None:
    _declare(vault, selected_store, tmp_path / "mirror", sample_bytes_per_month=0)
    _replicated(vault)
    _unproven(vault)

    media_replicas.sample(vault)

    assert [proven for _path, proven in _sampled(vault)] == [False]


def test_a_replica_that_this_install_cannot_decrypt_is_never_sampled_and_is_no_finding(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A moved identity file is the owner's to fix: it must not read as a damaged replica."""
    values, _config = selected_store
    _replicated(vault)
    _unproven(vault)
    Path(values["identity_path"]).chmod(0)

    assert media_replicas.sample(vault) == 0

    assert media_store.unreadable_reason(vault, MIRROR, "1") == "identity_unreadable"
    assert [proven for _path, proven in _sampled(vault)] == [False]
    assert _replica_row(vault)["state"] == media_replicas.UPLOAD_VERIFIED


def test_a_replica_of_another_key_version_is_never_sampled_and_gives_its_reason(
        vault: Path, selected_store, mirrored: Path) -> None:
    _replicated(vault)
    _unproven(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("UPDATE media_replicas SET key_version = '0' WHERE store = ?", (MIRROR,))

    media_replicas.sample(vault)

    assert media_store.unreadable_reason(vault, MIRROR, "0") == "identity_unreadable"
    assert [proven for _path, proven in _sampled(vault)] == [False]


def test_a_sample_that_finds_a_stored_replica_damaged_quarantines_it(vault: Path, selected_store, mirrored: Path) -> None:
    """A replica whose ciphertext no longer matches its record is the finding the sample exists to make."""
    relative, replica = _replicated(vault)
    _unproven(vault)
    [stored] = [path for path in _objects(mirrored) if path.parent.name == replica.object]
    damaged = bytearray(stored.read_bytes())
    damaged[-1] ^= 1
    stored.write_bytes(bytes(damaged))

    media_replicas.sample(vault)

    row = _replica_row(vault)
    assert (row["state"], row["level"], row["failure"]) == (media_replicas.QUARANTINED, media_replicas.DECRYPTED,
                                                            "digest_mismatch")


def test_the_sample_pick_and_the_month_release_read_the_sample_index_with_no_sort(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A scan or a sort over every replica for each pick would make a sample round cost the size of the store."""
    _replicated(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        pick = " ".join(row[3] for row in ledger.execute("EXPLAIN QUERY PLAN " + media_replicas._SAMPLE,
                                                         (MIRROR, "1", 0.0)))
        release = " ".join(row[3] for row in ledger.execute("EXPLAIN QUERY PLAN " + media_replicas._SAMPLE_RESET,
                                                            (MIRROR, "1", 202611, 8)))
    assert "USING INDEX media_replicas_sample" in pick and "TEMP B-TREE" not in pick
    assert "USING INDEX media_replicas_sample" in release


def test_the_eviction_gate_reads_the_damaged_versions_by_their_key(vault: Path, selected_store, mirrored: Path) -> None:
    """Eviction asks this for every candidate: a scan of every replica would cost the size of the store each time."""
    _replicated(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        plan = " ".join(row[3] for row in ledger.execute("EXPLAIN QUERY PLAN " + media_replicas._DAMAGED,
                                                         (json.dumps([["s", "o", "v"]]),)))
    assert "USING PRIMARY KEY" in plan and "SCAN media_damaged_versions" not in plan


def test_the_eviction_gate_asks_the_table_once_however_many_pointers_a_pooled_object_has(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A pooled object keeps a pointer for every filing and re-copy: a lookup for each ran inside eviction's write
    transaction, so its cost grew with the pool and held the vault's single writer."""
    pointers = [originals.StoredObject(1, MIRROR, "1", f"{number:064x}", f"version-{number}", 10, "0" * 64)
                for number in range(1000)]
    media_replicas.quarantine(vault, "Originals/damaged.bin", pointers[777], level=media_replicas.CONTENT,
                              failure="digest_mismatch")
    statements: list[str] = []
    with closing(media_jobs.MediaJobStore(vault)._connect(readonly=True)) as conn:
        conn.set_trace_callback(statements.append)
        found = media_replicas.quarantined_in(conn, pointers)
        clean = media_replicas.quarantined_in(conn, pointers[:777])
        conn.set_trace_callback(None)

    assert (found, clean) == (True, False)  # only the quarantined pointer counts, wherever it stands in the set
    assert len([sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))]) == 2


def _vm_steps_of_a_repeated_finding(vault: Path, holders: int, monkeypatch: pytest.MonkeyPatch, wrong: str | None) -> int:
    """Quarantine `holders` filings that a finding already holds quarantined, find it again, and count what SQLite runs.

    The filings share one version found damaged (`wrong` is None), share one found to decrypt wrong ("shared"), or are
    copies of one ciphertext under names of their own, each version found to decrypt wrong ("copies").
    """
    level, failure = (media_replicas.DECRYPTED, media_replicas.PLAINTEXT_MISMATCH) if wrong else (
        media_replicas.CONTENT, "digest_mismatch")
    digest = "d" * 64 if wrong else None
    versions = [f"{holders:032x}{number:032x}" for number in range(holders if wrong == "copies" else 1)]
    pointer = originals.StoredObject(1, MIRROR, "1", versions[0], "version", 10, digest or "0" * 64)
    with closing(media_jobs.MediaJobStore(vault)._connect()) as ledger, ledger:
        ledger.executemany(
            "INSERT INTO media_replicas(binary_rel, store, object, version, key_version, size, verified_at, state, level, "
            "failure, seen, ciphertext_sha256) VALUES (?, ?, ?, 'version', '1', 10, 0, 'quarantined', ?, ?, 1, ?)",
            [(f"Originals/{holders}/{number}.bin", MIRROR, versions[number % len(versions)], level, failure, digest)
             for number in range(holders)])
        ledger.executemany(
            "INSERT INTO media_damaged_versions(store, object, version, level, failure, ciphertext_sha256) "
            "VALUES (?, ?, 'version', ?, ?, ?)", [(MIRROR, name, level, failure, digest) for name in versions])
    steps = [0]
    connect = media_jobs.MediaJobStore._connect

    def counting(self, **options):
        conn = connect(self, **options)
        conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1), 1)
        return conn

    monkeypatch.setattr(media_jobs.MediaJobStore, "_connect", counting)
    media_replicas.quarantine(vault, f"Originals/{holders}/0.bin", pointer, level=level, failure=failure)
    return steps[0]


@pytest.mark.parametrize("wrong", [None, "shared", "copies"], ids=["found-damaged", "found-wrong", "copies-found-wrong"])
def test_a_finding_that_repeats_over_many_quarantined_filings_costs_what_it_costs_over_few(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch, wrong: str | None) -> None:
    """Every filing's attempt can record the finding again: a rescan of the group would make n attempts cost n squared."""
    few = _vm_steps_of_a_repeated_finding(vault, 50, monkeypatch, wrong)
    many = _vm_steps_of_a_repeated_finding(vault, 5000, monkeypatch, wrong)

    assert many < few * 2 + 500


# --- verify-stored-replicas: findings from a check and from eviction ---------------------------------------


def _due_again(vault: Path) -> None:
    """Make every replica due for its next check, as a month from now would."""
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("UPDATE media_replicas SET checked_at = 0, check_from = 0, level = NULL")


def _stored_file(root: Path, replica: originals.StoredObject) -> Path:
    [stored] = [path for path in _objects(root) if path.parent.name == replica.object]
    return stored


def _damage_a_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(bytes(data))


@pytest.mark.parametrize(("damage", "failure"), [
    pytest.param(lambda path: path.unlink(), "absent", id="the-store-lost-the-object"),
    pytest.param(lambda path: path.write_bytes(path.read_bytes()[:-1]), "size_mismatch", id="the-object-is-short"),
    pytest.param(_damage_a_byte, "digest_mismatch", id="one-byte-is-flipped"),
])
def test_a_check_quarantines_a_replica_that_the_store_lost_or_holds_damaged(
        vault: Path, selected_store, mirrored: Path, damage, failure: str) -> None:
    """A check that only counted what it read would leave a lost copy listed as one of the owner's safe copies."""
    _relative, replica = _replicated(vault)
    damage(_stored_file(mirrored, replica))
    _due_again(vault)

    media_replicas.check(vault)

    row = _replica_row(vault)
    assert (row["state"], row["level"], row["failure"]) == (media_replicas.QUARANTINED, media_replicas.CONTENT, failure)
    assert (row["object"], row["version"]) == (replica.object, replica.version)  # the finding names the damaged version


def test_eviction_keeps_an_original_whose_stored_copy_is_damaged_and_reports_the_finding(
        vault: Path, selected_store) -> None:
    """The drill: eviction's own head used to refuse silently, leaving the owner unaware that the stored copy was gone."""
    values, _config = selected_store
    path = _preserved(vault)
    reference = _offload(vault, path)
    _damage_a_byte(Path(values["root"]) / reference.object / reference.version)

    assert not media_store.evict(vault, path)

    row = _replica_row(vault, store=reference.store)
    # The eviction store declares no `head_recomputes`, so its head never proves content: the finding says presence.
    assert (row["state"], row["level"], row["failure"]) == (media_replicas.QUARANTINED, media_replicas.PRESENCE,
                                                            "digest_mismatch")
    assert (row["object"], row["version"]) == (reference.object, reference.version)
    assert path.exists()  # the local original is the only intact copy


def test_eviction_keeps_an_original_whose_pointer_is_quarantined_even_when_the_store_reads_well(
        vault: Path, selected_store) -> None:
    """A pointer that a check found damaged must not be trusted again because a later read of the store passes."""
    path = _preserved(vault)
    reference = _offload(vault, path)
    media_replicas.quarantine(vault, path.relative_to(vault).as_posix(), reference, level=media_replicas.CONTENT,
                              failure="digest_mismatch")

    assert not media_store.evict(vault, path)

    assert path.exists()


def _pooled(vault: Path, path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Move an offloaded original's bytes into the shared pool, as a pooled vault keeps them; return the object's digest."""
    content = path.read_bytes()
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    digest = hashlib.sha256(content).hexdigest()
    with original_pool.prepare(vault, digest, len(content), held_fs.Chunks(lambda: [content])) as prepared:
        prepared.publish()
    path.unlink()
    assert media_bindings.rebuild(vault)
    with media_store.open_original(vault, path):  # a read records the cache identity that an eviction needs
        pass
    return digest


def test_eviction_of_a_shared_original_keeps_its_object_and_quarantines_the_damaged_pointer(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pool's eviction path confirms each filing's pointer too: a damaged one must be a finding there as well."""
    values, _config = selected_store
    path = _preserved(vault)
    reference = _offload(vault, path)
    digest = _pooled(vault, path, monkeypatch)
    _damage_a_byte(Path(values["root"]) / reference.object / reference.version)

    assert not media_store.evict(vault, path)

    row = _replica_row(vault, store=reference.store)
    assert (row["binary_rel"], row["state"], row["failure"]) == (path.relative_to(vault).as_posix(),
                                                                 media_replicas.QUARANTINED, "digest_mismatch")
    assert original_pool.object_path(vault, digest).exists()


@pytest.mark.parametrize("failure", ["absent", "condemned"])
def test_eviction_of_a_shared_original_whose_pointer_is_absent_or_condemned_quarantines_it_and_queues_a_copy_again(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """A store with no scheduled check would never find a lost version later: eviction's own HEAD is the only finding."""
    values, _config = selected_store
    path = _preserved(vault)
    reference = _offload(vault, path)
    digest = _pooled(vault, path, monkeypatch)
    if failure == "absent":
        (Path(values["root"]) / reference.object / reference.version).unlink()
    else:
        head = media_store.FilesystemStore.head

        def condemned(self, name: str, version: str):
            found = head(self, name, version)
            return found if found is None else dataclasses.replace(found, condemned=True)

        monkeypatch.setattr(media_store.FilesystemStore, "head", condemned)

    assert not media_store.evict(vault, path)

    row = _replica_row(vault, store=reference.store)
    assert (row["state"], row["failure"]) == (media_replicas.QUARANTINED, failure)
    assert _requests(vault) == [(path.relative_to(vault).as_posix(), media_replicas.RECOPY)]
    assert original_pool.object_path(vault, digest).exists()  # the bytes that the copy again reads


def test_eviction_of_a_shared_original_records_a_damaged_pointer_that_another_filing_holds(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The filing under eviction may hold a healthy pointer: the damaged one of the object's other filing is still recorded."""
    values, _config = selected_store
    path = _preserved(vault)
    healthy = _offload(vault, path)
    digest = _pooled(vault, path, monkeypatch)
    stored = Path(values["root"]) / healthy.object / healthy.version
    other = healthy.version + "-other"
    damaged = bytearray(stored.read_bytes())
    damaged[len(damaged) // 2] ^= 0xFF
    (stored.parent / other).write_bytes(damaged)  # a second version of the object, whose bytes are not the recorded ones
    with closing(media_jobs.MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("INSERT INTO media_bindings SELECT ?, companion_rel, companion_hash, object_key, sha256, size, "
                     "resident_only, ? FROM media_bindings WHERE binary_rel = ?",
                     ("Other/holder-of-the-other-pointer.txt", json.dumps({**healthy.fields(), "version": other}),
                      path.relative_to(vault).as_posix()))

    assert not media_store.evict(vault, path)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        found = ledger.execute("SELECT binary_rel, version, failure FROM media_replicas WHERE state = ?",
                               (media_replicas.QUARANTINED,)).fetchall()
    assert found == [("Other/holder-of-the-other-pointer.txt", other, "digest_mismatch")]
    assert original_pool.object_path(vault, digest).exists()


def _quarantined_while_the_store_answers(vault: Path, relative: str, reference: originals.StoredObject,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """A check finds the pointer damaged at the moment eviction asks the store about it: the finding lands during the HEAD."""
    head = media_store.FilesystemStore.head

    def answering_after_a_finding(store, name, version):
        media_replicas.quarantine(vault, relative, reference, level=media_replicas.CONTENT, failure="digest_mismatch")
        return head(store, name, version)

    monkeypatch.setattr(media_store.FilesystemStore, "head", answering_after_a_finding)


def test_eviction_keeps_the_local_bytes_when_the_pointer_is_quarantined_while_the_store_answers(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate before the HEAD cannot see a finding that lands after it: the bytes would go with their only good copy doubted."""
    path = _preserved(vault)
    reference = _offload(vault, path)
    _quarantined_while_the_store_answers(vault, path.relative_to(vault).as_posix(), reference, monkeypatch)

    assert not media_store.evict(vault, path)

    assert path.exists()


def test_eviction_of_a_shared_original_keeps_its_object_when_the_pointer_is_quarantined_while_the_store_answers(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pool's unlink has its own transaction: it needs the same last look as a single original's."""
    path = _preserved(vault)
    reference = _offload(vault, path)
    digest = _pooled(vault, path, monkeypatch)
    _quarantined_while_the_store_answers(vault, path.relative_to(vault).as_posix(), reference, monkeypatch)

    assert not media_store.evict(vault, path)

    assert original_pool.object_path(vault, digest).exists()


# --- verify-stored-replicas: copying a quarantined replica again ---------------------------------------------


def _recopy_all(vault: Path) -> None:
    """Run the worker's copy units until the queue has nothing it can settle, as the service would."""
    while media_replicas.drain(vault):
        pass


def _queued(vault: Path) -> list[tuple[str, str, str, str | None]]:
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        return list(ledger.execute("SELECT store, kind, state, code FROM media_replica_queue ORDER BY store"))


def _requests(vault: Path) -> list[tuple[str, str]]:
    """The original and the kind of each request in the copy queue."""
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        return list(ledger.execute("SELECT binary_rel, kind FROM media_replica_queue ORDER BY binary_rel"))


def _entry(vault: Path, path: Path | str, store: str) -> originals.StoredObject:
    snapshot = originals.original_state(vault, path)
    return next(entry for entry in (snapshot.reference, *snapshot.replicas) if entry is not None and entry.store == store)


def test_a_replica_that_a_check_found_damaged_is_copied_again_from_the_local_original(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The drill's end: a finding that nothing repaired would leave the owner with one copy for good."""
    relative, damaged = _replicated(vault)
    _damage_a_byte(_stored_file(mirrored, damaged))
    _due_again(vault)
    media_replicas.check(vault)
    assert _replica_row(vault)["state"] == media_replicas.QUARANTINED

    _recopy_all(vault)

    fresh = _entry(vault, relative, MIRROR)
    assert (fresh.object, fresh.version) != (damaged.object, damaged.version)
    assert media_store.check_replica(vault, fresh) is None
    row = _replica_row(vault)
    assert (row["object"], row["version"], row["state"]) == (fresh.object, fresh.version, media_replicas.UPLOAD_VERIFIED)
    assert _stored_file(mirrored, damaged).exists()  # the damaged version stays in its store, named by the finding
    assert [item for item in _queued(vault) if item[0] == MIRROR] == []


@pytest.mark.parametrize("closed_first", [
    pytest.param(False, id="the-request-is-still-queued"),
    pytest.param(True, id="the-request-was-closed-and-is-made-again"),
])
def test_a_finished_copy_again_leaves_the_request_that_a_newer_finding_queued(
        vault: Path, selected_store, mirrored: Path, monkeypatch, closed_first: bool) -> None:
    """The copy again publishes version B; before the worker settles it, B is found bad and its repair is queued.
    Closing that request would leave B quarantined with nothing queued to repair it, for good."""
    relative, damaged = _replicated(vault)
    _damage_a_byte(_stored_file(mirrored, damaged))
    _due_again(vault)
    media_replicas.check(vault)
    recopy = media_store.recopy

    def recopy_then_find_the_new_version_bad(root: Path, path: Path, store: str) -> originals.StoredObject | None:
        fresh = recopy(root, path, store)
        if closed_first:
            with closing(sqlite3.connect(media_jobs.MediaJobStore(root).path)) as ledger, ledger:
                ledger.execute("DELETE FROM media_replica_queue")
        media_replicas.quarantine(root, relative, fresh, level=media_replicas.CONTENT, failure="digest_mismatch")
        return fresh

    monkeypatch.setattr(media_store, "recopy", recopy_then_find_the_new_version_bad)
    monkeypatch.setattr(media_replicas, "_DRAIN_BATCH", 1)
    assert media_replicas.drain(vault) == 1
    monkeypatch.setattr(media_store, "recopy", recopy)

    assert _queued(vault) == [(MIRROR, media_replicas.RECOPY, media_replicas.PENDING, None)]
    _recopy_all(vault)
    row = _replica_row(vault)
    assert (row["state"], _queued(vault)) == (media_replicas.UPLOAD_VERIFIED, [])
    assert (row["object"], row["version"]) != (damaged.object, damaged.version)


def test_a_replica_is_copied_again_as_ciphertext_from_another_replica_when_the_original_left_the_volume(
        vault: Path, selected_store, mirrored: Path) -> None:
    """An evicted original has no local bytes: the copy must come from a replica that is still good, and stay ciphertext."""
    values, _config = selected_store
    relative, damaged = _replicated(vault)
    pointer = originals.original_state(vault, relative).reference
    assert media_store.evict(vault, vault / relative)
    _damage_a_byte(_stored_file(mirrored, damaged))
    _due_again(vault)
    media_replicas.check(vault)

    _recopy_all(vault)

    fresh = _entry(vault, relative, MIRROR)
    assert (fresh.object, fresh.version) != (damaged.object, damaged.version)
    assert (fresh.key_version, fresh.ciphertext_sha256) == (pointer.key_version, pointer.ciphertext_sha256)
    assert media_store.check_replica(vault, fresh) is None
    assert originals.original_state(vault, relative).reference == pointer


def test_a_damaged_pointer_of_an_evicted_original_is_replaced_from_a_replica_and_the_original_reads_again(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The store that serves eviction is replaced the same way: otherwise a damaged pointer leaves the original unreadable."""
    values, _config = selected_store
    relative, _mirror = _replicated(vault)
    content = (vault / relative).read_bytes()
    pointer = originals.original_state(vault, relative).reference
    assert media_store.evict(vault, vault / relative)
    _damage_a_byte(Path(values["root"]) / pointer.object / pointer.version)
    _due_again(vault)
    media_replicas.check(vault)
    assert _replica_row(vault, store=pointer.store)["state"] == media_replicas.QUARANTINED

    _recopy_all(vault)

    fresh = originals.original_state(vault, relative).reference
    assert (fresh.object, fresh.version) != (pointer.object, pointer.version)
    with media_store.open_original(vault, relative) as source:
        assert source.stream.read() == content


def test_a_replica_that_fixity_downloads_and_finds_damaged_is_quarantined_with_the_failure(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A repair that skipped the finding would try the same bad copy at every later damage, and the owner never learn of it."""
    original = _stored(vault, "Recordings")
    reference = _offload(vault, original)
    _replicate_all(vault)
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    original.write_bytes(DAMAGED)
    first = Path(selected_store[0]["root"]) / reference.object / reference.version
    first.write_bytes(os.urandom(first.stat().st_size))

    assert original_fixity.run_pass(vault, budget=0).outcomes == {"restored": 1}

    row = _replica_row(vault, store=reference.store)
    assert (row["state"], row["level"], row["failure"]) == (media_replicas.QUARANTINED, media_replicas.DECRYPTED,
                                                            "digest_mismatch")
    assert [item[:2] for item in _queued(vault) if item[0] == reference.store] == [(reference.store, media_replicas.RECOPY)]


def test_a_repair_that_finds_the_binding_rebound_since_its_row_was_read_quarantines_nothing(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The replicas of a rebound binding hold its new bytes: judged against the old digest they would read as damaged."""
    original = _stored(vault, "Recordings")
    _offload(vault, original)
    _replicate_all(vault)
    sidecar = original.with_name(original.name + ".md")
    held, stale = hashlib.sha256(TRANSCRIPT).hexdigest(), hashlib.sha256(DAMAGED).hexdigest()
    sidecar.write_text(sidecar.read_text().replace(held, stale))  # the row is read while the binding names another digest
    original_fixity.walk(vault)
    copies = original_fixity._copies

    def rebound_after_the_row_was_read(*arguments):
        found = copies(*arguments)
        sidecar.write_text(sidecar.read_text().replace(stale, held))  # the binding names what its replicas hold
        return found

    monkeypatch.setattr(original_fixity, "_copies", rebound_after_the_row_was_read)

    result = original_fixity.run_pass(vault)

    states = [_replica_row(vault, store=store)["state"] for store in ("test-store", MIRROR)]
    assert (result.rescheduled, states) == (1, [media_replicas.UPLOAD_VERIFIED] * 2)


def test_fixity_uses_the_current_size_of_the_binding_that_names_the_replica(
        vault: Path, selected_store, mirrored: Path) -> None:
    original = _stored(vault, "Recordings")
    pointer = _offload(vault, original)
    _replicate_all(vault)
    sidecar = original.with_name(original.name + ".md")
    current = sidecar.read_text()
    stale = current.replace(f"binary_size: {len(TRANSCRIPT)}", f"binary_size: {len(TRANSCRIPT) + 1}")
    stale = stale.replace(f"artifact_size: {len(TRANSCRIPT)}", f"artifact_size: {len(TRANSCRIPT) + 1}")
    assert stale != current
    sidecar.write_text(stale)
    original_fixity.walk(vault)
    sidecar.write_text(current)
    original.write_bytes(DAMAGED)

    assert original_fixity.run_pass(vault, budget=len(TRANSCRIPT)).outcomes == {"restored": 1}

    assert original.read_bytes() == TRANSCRIPT
    assert _replica_row(vault, store=pointer.store)["state"] == media_replicas.UPLOAD_VERIFIED


def test_a_repair_does_not_read_a_quarantined_replica_and_names_it_in_the_route(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The finding is a promise: a quarantined copy is not downloaded again for every repair."""
    original = _stored(vault, "Recordings")
    reference = _offload(vault, original)
    _replicate_all(vault)
    media_replicas.quarantine(vault, original.relative_to(vault).as_posix(), reference, level=media_replicas.CONTENT,
                              failure="digest_mismatch")
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    original.write_bytes(DAMAGED)
    reads = _count_downloads(monkeypatch)

    assert original_fixity.run_pass(vault, budget=0).outcomes == {"restored": 1}

    assert reads == [MIRROR]
    [finding] = _findings(vault)
    [tried, used] = finding.meta["route"]
    assert (tried["source"], tried["failed"], used["source"]) == ("media_store", "quarantined", "replica")


def test_a_damaged_local_original_is_not_the_source_of_a_copy_again(
        vault: Path, selected_store, mirrored: Path) -> None:
    """Encrypting what the volume holds would pass the damage on as a fresh, verified-looking replica."""
    relative, damaged = _replicated(vault)
    path = vault / relative
    content = path.read_bytes()
    pointer = originals.original_state(vault, relative).reference
    _damage_a_byte(_stored_file(mirrored, damaged))
    _due_again(vault)
    media_replicas.check(vault)
    path.write_bytes(bytes(reversed(content)))  # the same size, other bytes: the original is damaged too

    _recopy_all(vault)

    fresh = _entry(vault, relative, MIRROR)
    assert fresh.ciphertext_sha256 == pointer.ciphertext_sha256  # the pointer's ciphertext, not the damaged file's
    expected = media_store.ObjectHead(len(content), hashlib.sha256(content).hexdigest())
    assert media_store.sample_replica(vault, fresh, expected) is None


def test_a_cached_original_whose_pointer_was_replaced_can_still_be_evicted(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The cache row keys on the pointer: left on the damaged one, eviction would refuse the original for good."""
    values, _config = selected_store
    relative, _mirror = _replicated(vault)
    pointer = originals.original_state(vault, relative).reference
    _damage_a_byte(Path(values["root"]) / pointer.object / pointer.version)
    _due_again(vault)
    media_replicas.check(vault)
    _recopy_all(vault)
    assert originals.original_state(vault, relative).reference != pointer

    assert media_store.evict(vault, vault / relative)


def test_a_copy_again_reads_a_healthy_replica_when_the_shared_object_of_a_pooled_original_is_short(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A short local object is damage to route around: refusing it at the open would strand the original behind a good copy."""
    path = _preserved(vault)
    pointer = _offload(vault, path)
    _replicate_all(vault)
    relative = path.relative_to(vault).as_posix()
    shared = original_pool.object_path(vault, _pooled(vault, path, monkeypatch))
    shared.write_bytes(shared.read_bytes()[:-1])
    media_replicas.quarantine(vault, relative, pointer, level=media_replicas.CONTENT, failure="digest_mismatch")

    replaced = media_store.recopy(vault, path, pointer.store)

    assert (replaced is not None, originals.original_state(vault, relative).reference == replaced != pointer) == (True, True)


def test_a_replica_that_went_bad_since_its_last_check_is_not_copied_it_is_quarantined(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A source is trusted only as far as its bytes hash: copying an unchecked damaged replica would spread the damage."""
    values, _config = selected_store
    relative, damaged = _replicated(vault)
    pointer = originals.original_state(vault, relative).reference
    assert media_store.evict(vault, vault / relative)
    _damage_a_byte(_stored_file(mirrored, damaged))
    _due_again(vault)
    media_replicas.check(vault)
    _damage_a_byte(Path(values["root"]) / pointer.object / pointer.version)  # after the check: the table still trusts it
    assert _replica_row(vault, store=pointer.store)["state"] != media_replicas.QUARANTINED

    _recopy_all(vault)

    assert _replica_row(vault, store=pointer.store)["state"] == media_replicas.QUARANTINED
    assert _entry(vault, relative, MIRROR) == damaged
    assert [item[2:] for item in _queued(vault) if item[0] == MIRROR] == [(media_replicas.REFUSED, "no_verified_source")]


@pytest.mark.parametrize("failure", ["absent", media_replicas.PLAINTEXT_MISMATCH])
def test_a_copy_again_publishes_nothing_when_the_replica_it_copied_was_found_damaged_meanwhile(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """The copy trusted its source when it chose it: a finding since then must not reach a filing that eviction trusts."""
    relative, damaged = _replicated(vault)
    pointer = originals.original_state(vault, relative).reference
    assert media_store.evict(vault, vault / relative)
    _damage_a_byte(_stored_file(mirrored, damaged))
    _due_again(vault)
    media_replicas.check(vault)
    upload = media_store.FilesystemStore.upload

    def source_found_wrong_after_the_upload(store, *args, **kwargs):
        version = upload(store, *args, **kwargs)
        level = media_replicas.DECRYPTED if failure == media_replicas.PLAINTEXT_MISMATCH else media_replicas.CONTENT
        media_replicas.quarantine(vault, relative, pointer, level=level, failure=failure)
        return version

    monkeypatch.setattr(media_store.FilesystemStore, "upload", source_found_wrong_after_the_upload)

    _recopy_all(vault)

    assert _entry(vault, relative, MIRROR) == damaged  # the binding is the one the finding named
    [(_store, kind, state, _code)] = [item for item in _queued(vault) if item[0] == MIRROR]
    assert (kind, state) == (media_replicas.RECOPY, media_replicas.PENDING)


def test_first_replica_is_not_published_when_its_source_is_quarantined_after_upload(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _preserved(vault)
    pointer = _offload(vault, path)
    assert media_store.evict(vault, path)
    upload = media_store.FilesystemStore.upload

    def source_found_wrong_after_the_upload(store, *args, **kwargs):
        version = upload(store, *args, **kwargs)
        media_replicas.quarantine(vault, path.relative_to(vault).as_posix(), pointer,
                                  level=media_replicas.DECRYPTED, failure=media_replicas.PLAINTEXT_MISMATCH)
        return version

    monkeypatch.setattr(media_store.FilesystemStore, "upload", source_found_wrong_after_the_upload)

    with pytest.raises(media_store.MediaStoreError) as stopped:
        media_store.replicate(vault, path, MIRROR)

    assert stopped.value.code == media_store.STORE_BUSY_CODE
    assert originals.original_state(vault, path).replicas == ()


def test_a_copy_that_its_source_was_found_wrong_under_while_it_was_published_is_published_quarantined(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A finding that lands after the publication check reaches the copy through its row, never as a trusted version.

    The copy is byte-for-byte the source's ciphertext, so a source whose plaintext was found wrong makes it wrong too.
    """
    relative, damaged = _replicated(vault)
    pointer = originals.original_state(vault, relative).reference
    assert media_store.evict(vault, vault / relative)
    _damage_a_byte(_stored_file(mirrored, damaged))
    _due_again(vault)
    media_replicas.check(vault)
    seal = media_store._seal

    def source_found_wrong_as_the_copy_is_sealed(*arguments):
        media_replicas.quarantine(vault, relative, pointer, level=media_replicas.DECRYPTED,
                                  failure=media_replicas.PLAINTEXT_MISMATCH)
        return seal(*arguments)

    monkeypatch.setattr(media_store, "_seal", source_found_wrong_as_the_copy_is_sealed)

    _recopy_all(vault)

    published = _entry(vault, relative, MIRROR)
    assert published != damaged  # the finding came too late to stop the write
    row = _replica_row(vault)
    assert row["object"] == published.object
    assert (row["state"], row["failure"]) == (media_replicas.QUARANTINED, media_replicas.PLAINTEXT_MISMATCH)
    assert media_replicas.has_quarantined(vault, (published,))  # so eviction does not trust it
    assert (relative, media_replicas.RECOPY) in _requests(vault)


def test_a_replica_of_an_evicted_original_is_not_copied_from_a_pointer_that_was_found_wrong(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The pointer is the only entry of an evicted original: its wrong plaintext would be copied into a second store."""
    original = _preserved(vault)
    pointer = _offload(vault, original)
    relative = original.relative_to(vault).as_posix()
    assert media_store.evict(vault, original)
    media_replicas.quarantine(vault, relative, pointer, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)

    _replicate_all(vault)

    assert [item[2:] for item in _queued(vault) if item[0] == MIRROR] == [(media_replicas.REFUSED, "no_verified_source")]
    assert _objects(mirrored) == []
    assert all(entry.store != MIRROR for entry in originals.original_state(vault, original).replicas)


def test_a_replica_request_refused_for_want_of_a_verified_source_is_made_again_by_the_rebuild_after_the_pointer_is_repaired(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The owner asked once for the replica; a repaired pointer is a source now, and no second request should be needed."""
    original = _preserved(vault)
    pointer = _offload(vault, original)
    relative = original.relative_to(vault).as_posix()
    assert media_store.evict(vault, original)
    media_replicas.quarantine(vault, relative, pointer, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)
    media_replicas.request(vault, MIRROR)
    while media_replicas.work(vault):
        pass
    assert [item[1:] for item in _queued(vault) if item[0] == MIRROR] == [
        (media_replicas.REPLICATE, media_replicas.REFUSED, "no_verified_source")]
    _unproven(vault)
    media_replicas.sample(vault)  # the pointer decrypts to the binding: the finding was a false alarm
    assert _replica_row_of(vault, original, pointer.store)["state"] == media_replicas.UPLOAD_VERIFIED

    media_replicas.rebuild(vault)

    assert [item[1:] for item in _queued(vault) if item[0] == MIRROR] == [
        (media_replicas.REPLICATE, media_replicas.PENDING, None)]
    while media_replicas.work(vault):
        pass
    assert _entry(vault, relative, MIRROR).store == MIRROR
    assert len(_objects(mirrored)) == 1


def test_a_quarantined_replica_with_no_verified_source_stays_quarantined_and_the_binding_is_unchanged(
        vault: Path, selected_store, mirrored: Path) -> None:
    """With both copies damaged and the original gone, a copy of either would spread the damage under a fresh name."""
    values, _config = selected_store
    relative, damaged = _replicated(vault)
    pointer = originals.original_state(vault, relative).reference
    assert media_store.evict(vault, vault / relative)
    _damage_a_byte(_stored_file(mirrored, damaged))
    _damage_a_byte(Path(values["root"]) / pointer.object / pointer.version)
    before = originals.original_state(vault, relative)
    _due_again(vault)
    media_replicas.check(vault)

    _recopy_all(vault)

    assert originals.original_state(vault, relative) == before
    assert [(store, state, code) for store, _kind, state, code in _queued(vault)] == [
        (MIRROR, media_replicas.REFUSED, "no_verified_source"),
        (pointer.store, media_replicas.REFUSED, "no_verified_source"),
    ]


def test_a_binding_edited_while_a_replica_is_copied_again_is_not_overwritten(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The copy runs outside the guard: its publication must replace the entry it found damaged, never a newer one."""
    relative, damaged = _replicated(vault)
    _damage_a_byte(_stored_file(mirrored, damaged))
    _due_again(vault)
    media_replicas.check(vault)
    monkeypatch.setattr(media_replicas, "_RETRY_SECONDS", 3600)
    newer = originals.StoredObject(1, MIRROR, damaged.key_version, "e" * 64, "a-newer-version", 9, "f" * 64)
    upload = media_store.FilesystemStore.upload

    def edit_the_binding_after_the_upload(self, name, stream, *, size, sha256):
        result = upload(self, name, stream, size=size, sha256=sha256)
        if self.root == mirrored:
            media_store._publishing(vault, "test_concurrent_edit", lambda: media_store._commit_reference(
                vault, media_store.prepare_replica(vault, originals.original_state(vault, relative), newer)))
        return result

    monkeypatch.setattr(media_store.FilesystemStore, "upload", edit_the_binding_after_the_upload)
    _recopy_all(vault)
    monkeypatch.setattr(media_store.FilesystemStore, "upload", upload)

    assert _entry(vault, relative, MIRROR) == newer  # the concurrent edit stands
    assert _queued(vault) == [(MIRROR, media_replicas.RECOPY, media_replicas.PENDING, "MEDIA_ORIGINAL_CHANGED")]

    monkeypatch.setattr(media_replicas, "_RETRY_SECONDS", 0)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("UPDATE media_replica_queue SET retry_at = 0")
    _recopy_all(vault)

    assert _entry(vault, relative, MIRROR) == newer  # the retry finds the finding stale and writes nothing
    assert _queued(vault) == []


def _two_filings_of_one_version(vault: Path, selected_store) -> tuple[Path, Path, originals.StoredObject]:
    """Two filings of equal bytes, whose offload reused one stored version; the version is damaged and found by one of them."""
    values, _config = selected_store
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    second = _preserved(vault, category="second")
    assert _offload(vault, second) == pointer
    _damage_a_byte(Path(values["root"]) / pointer.object / pointer.version)
    media_replicas.quarantine(vault, first.relative_to(vault).as_posix(), pointer, level=media_replicas.CONTENT,
                              failure="digest_mismatch")
    return first, second, pointer


def test_a_copy_again_of_a_version_that_two_filings_share_uploads_it_once_and_keeps_the_allowance(
        vault: Path, selected_store) -> None:
    """The second filing reuses the first's replacement: two uploads would count twice against an allowance that fits one."""
    first, second, pointer = _two_filings_of_one_version(vault, selected_store)
    size = first.stat().st_size
    _configure(selected_store, vault, allowance_bytes=size)
    assert media_allowance.reference_usage(vault).used_bytes == size

    _recopy_all(vault)

    fresh = _entry(vault, first, pointer.store)
    assert (fresh.object, fresh.version) != (pointer.object, pointer.version)
    assert _entry(vault, second, pointer.store) == fresh
    assert media_allowance.reference_usage(vault).used_bytes == size
    assert len(_objects(Path(selected_store[0]["root"]))) == 2  # the damaged version and its one replacement


def test_eviction_recovers_an_absent_shared_version_at_the_existing_allowance(vault: Path, selected_store) -> None:
    """Eviction and a replayed offload must both leave the resident pointer available for shared repair."""
    resident = _preserved(vault, category="resident")
    source = resident.read_bytes()
    ledger = media_jobs.MediaJobStore(vault)
    claimed = ledger.claim_next()
    assert claimed is not None and claimed.binary_path == resident and claimed.do_offload
    lost = media_store.offload(vault, resident, job=claimed)
    assert lost is not None  # Publication succeeded, but the worker stopped before acknowledging this job.
    sibling = _preserved(vault, category="sibling")
    assert _offload(vault, sibling) == lost
    assert media_store.evict(vault, sibling)
    _configure(selected_store, vault, allowance_bytes=len(source))
    (Path(selected_store[0]["root"]) / lost.object / lost.version).unlink()

    assert not media_store.evict(vault, resident)
    assert resident.read_bytes() == source
    assert originals.original_state(vault, resident).reference == lost
    for filing in (resident, sibling):
        [replica] = media_replicas.original_view(vault, filing.relative_to(vault).as_posix())["replicas"]
        assert replica["state"] == media_replicas.QUARANTINED
        assert replica["finding"]["failure"] == "absent"

    assert ledger.recover_interrupted() == 1
    replayed = ledger.claim_next()
    assert replayed is not None and replayed.binary_path == resident
    assert media_store.offload(vault, resident, batch=media_store.OffloadBatch(vault), job=replayed) is None
    ledger.complete_offload(replayed)
    ledger.complete(replayed)

    _recopy_all(vault)

    replacement = _entry(vault, resident, lost.store)
    assert replacement != lost
    assert _entry(vault, sibling, lost.store) == replacement
    assert media_allowance.reference_usage(vault).used_bytes == len(source)
    with media_store.open_original(vault, sibling) as original:
        assert original.stream.read() == source


def test_a_copy_again_after_a_key_rotation_is_still_shared_by_the_filings_of_one_version(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A copy of ciphertext keeps its source's key version: a rotated key must not make each filing upload its own."""
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    second = _preserved(vault, category="second")
    assert _offload(vault, second) == pointer
    _replicate_all(vault)
    size = originals.original_state(vault, first).binary_size
    for original in (first, second):
        assert media_store.evict(vault, original)  # no local bytes: the new version is copied from the mirror's ciphertext
    _damage_a_byte(Path(selected_store[0]["root"]) / pointer.object / pointer.version)
    media_replicas.quarantine(vault, first.relative_to(vault).as_posix(), pointer, level=media_replicas.CONTENT,
                              failure="digest_mismatch")
    _configure(selected_store, vault, key_version="2", allowance_bytes=size)

    _recopy_all(vault)

    fresh = _entry(vault, first, pointer.store)
    assert (fresh.key_version, fresh != pointer) == (pointer.key_version, True)
    assert _entry(vault, second, pointer.store) == fresh
    assert media_allowance.reference_usage(vault).used_bytes == size


def test_equal_bytes_filed_while_a_version_is_recorded_damaged_get_a_version_of_their_own(
        vault: Path, selected_store) -> None:
    """Reusing it would bind the new filing to bytes that a finding condemned, and eviction could then drop its only good copy."""
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    media_replicas.quarantine(vault, first.relative_to(vault).as_posix(), pointer, level=media_replicas.CONTENT,
                              failure="digest_mismatch")  # the store's HEAD still answers well: nothing else says so
    second = _preserved(vault, category="second")

    fresh = _offload(vault, second)

    assert fresh.object != pointer.object
    assert media_store.check_replica(vault, fresh) is None
    media_store.forget_original(vault, first)  # the filing that held the finding is gone; the finding is not
    assert media_replicas.has_quarantined(vault, (pointer,))


def test_the_filing_that_skipped_a_damaged_version_is_where_the_filings_that_hold_it_look_for_a_copy(
        vault: Path, selected_store) -> None:
    """Without it each filing that still lists the damaged version would upload its own copy of the same bytes."""
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    media_replicas.quarantine(vault, first.relative_to(vault).as_posix(), pointer, level=media_replicas.CONTENT,
                              failure="digest_mismatch")
    second = _preserved(vault, category="second")
    fresh = _offload(vault, second)

    media_store.recopy(vault, first, pointer.store)

    assert _entry(vault, first, pointer.store) == fresh


def test_offload_that_finds_the_version_it_would_reuse_damaged_uploads_its_own_and_records_the_finding(
        vault: Path, selected_store) -> None:
    """The finding is made on the way: failing the offload would retry it against the same version for good."""
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    _damage_a_byte(Path(selected_store[0]["root"]) / pointer.object / pointer.version)
    second = _preserved(vault, category="second")

    fresh = _offload(vault, second)

    assert fresh.object != pointer.object
    row = _replica_row(vault, store=pointer.store)
    assert (row["binary_rel"], row["state"], row["failure"]) == (first.relative_to(vault).as_posix(),
                                                                 media_replicas.QUARANTINED, "digest_mismatch")


def test_a_filing_whose_row_was_lost_stays_gated_and_is_quarantined_by_the_next_rebuild(
        vault: Path, selected_store) -> None:
    """The record is the version's: a filing that no row lists, or that a rebuild writes late, must not lose it."""
    first, second, pointer = _two_filings_of_one_version(vault, selected_store)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("DELETE FROM media_replicas WHERE binary_rel = ?", (second.relative_to(vault).as_posix(),))
    media_store.recopy(vault, first, pointer.store)  # the one filing that the table lists heals; `second` still lists the version

    assert media_replicas.has_quarantined(vault, (pointer,))
    media_replicas.rebuild(vault)

    assert (second.relative_to(vault).as_posix(), media_replicas.RECOPY) in _requests(vault)
    _recopy_all(vault)
    media_replicas.rebuild(vault)
    assert _entry(vault, second, pointer.store) == _entry(vault, first, pointer.store)


def test_a_rebuild_whose_walk_skips_a_filing_keeps_the_record_of_the_damaged_version_that_filing_lists(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The walk skips a binding whose Trash retirement is unconfirmed: no row lists the version, a filing still does.

    Forgetting the record then lets the next rebuild write that filing as sound, and eviction would drop the bytes that
    were its only good copy.
    """
    first, second, pointer = _two_filings_of_one_version(vault, selected_store)
    media_store.recopy(vault, first, pointer.store)  # `first` heals; `second` is the one filing that still lists the version
    held = second.relative_to(vault).as_posix()
    walk = media_store.bound_originals
    monkeypatch.setattr(media_store, "bound_originals", lambda root, **found: (
        snapshot for snapshot in walk(root, **found) if snapshot.path.relative_to(root).as_posix() != held))

    media_replicas.rebuild(vault)

    assert media_replicas.has_quarantined(vault, (pointer,))
    monkeypatch.setattr(media_store, "bound_originals", walk)  # the next walk reads every binding
    media_replicas.rebuild(vault)
    assert _replica_row_of(vault, second, pointer.store)["state"] == media_replicas.QUARANTINED
    assert (held, media_replicas.RECOPY) in _requests(vault)


def test_a_copy_again_that_stops_after_its_first_filing_keeps_the_other_quarantined_and_ends_an_active_census(
        vault: Path, selected_store) -> None:
    """The record of the damage must outlive the copy that found it, and a batch's census must see the new version."""
    first, second, pointer = _two_filings_of_one_version(vault, selected_store)
    one, another = _filed(vault, 2)
    size = first.stat().st_size
    _configure(selected_store, vault, allowance_bytes=2 * size + one.stat().st_size)  # fits both versions and one member
    batch = media_store.OffloadBatch(vault)
    media_store.offload(vault, one, batch=batch)  # the batch reads its census here: one version of the shared bytes, and this member

    media_store.recopy(vault, first, pointer.store)  # the worker stops here: the second filing is still on the damaged version

    assert _entry(vault, second, pointer.store) == pointer
    assert media_replicas.has_quarantined(vault, (pointer,))  # eviction of the second filing is still refused
    assert (second.relative_to(vault).as_posix(), "recopy") in _requests(vault)
    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.offload(vault, another, batch=batch)
    assert refused.value.code == media_allowance.MEDIA_ALLOWANCE_EXCEEDED

    _recopy_all(vault)  # the retry: the second filing reuses the replacement and the damaged version is bound by no one

    assert _entry(vault, second, pointer.store) == _entry(vault, first, pointer.store)
    assert media_allowance.reference_usage(vault).used_bytes == size + one.stat().st_size


@pytest.mark.parametrize("deleted", [
    pytest.param(True, id="the-filing-of-the-replacement-was-deleted"),
    pytest.param(False, id="the-store-condemned-it-while-it-was-bound"),
])
def test_a_copy_again_does_not_reuse_a_replacement_that_no_live_filing_confirms(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch, deleted: bool) -> None:
    """A condemned version is deleted by the bucket rule: a filing that took it would lose its original."""
    first, second, pointer = _two_filings_of_one_version(vault, selected_store)
    media_store.recopy(vault, first, pointer.store)
    replacement = _entry(vault, first, pointer.store)
    if deleted:
        media_store.forget_original(vault, first)  # alone: the store still holds the version, and answers well
    else:
        head = media_store.FilesystemStore.head

        def condemned_replacement(self, name: str, version: str):
            found = head(self, name, version)
            return found if found is None or name != replacement.object else dataclasses.replace(found, condemned=True)

        monkeypatch.setattr(media_store.FilesystemStore, "head", condemned_replacement)

    media_store.recopy(vault, second, pointer.store)

    fresh = _entry(vault, second, pointer.store)
    assert fresh.object not in {pointer.object, replacement.object}
    assert media_store.check_replica(vault, fresh) is None  # it reads back


def test_a_copy_again_does_not_reuse_the_version_of_a_filing_that_holds_other_bytes(
        vault: Path, selected_store) -> None:
    """A hint is only a place to look: the filing that it names may have been given other content since."""
    first, second, pointer = _two_filings_of_one_version(vault, selected_store)
    stranger = _filed(vault, 1)[0]
    stranger_pointer = _offload(vault, stranger)
    media_replicas.note_repair(vault, stranger.relative_to(vault).as_posix(), [pointer])

    media_store.recopy(vault, second, pointer.store)

    fresh = _entry(vault, second, pointer.store)
    assert fresh.object not in {pointer.object, stranger_pointer.object}


def test_a_copy_again_stops_when_the_filing_of_the_replacement_changes_before_it_publishes(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The binding is read again under the vault boundary, as offload does for the version that it reuses."""
    first, second, pointer = _two_filings_of_one_version(vault, selected_store)
    media_store.recopy(vault, first, pointer.store)
    chosen = media_store._repaired_sibling

    def deleted_after_it_was_chosen(*arguments):
        sibling = chosen(*arguments)
        media_store.forget_original(vault, first)
        return sibling

    monkeypatch.setattr(media_store, "_repaired_sibling", deleted_after_it_was_chosen)

    with pytest.raises(media_store.MediaStoreError) as stopped:
        media_store.recopy(vault, second, pointer.store)

    assert stopped.value.code == media_store.STORE_BUSY_CODE
    assert _entry(vault, second, pointer.store) == pointer


def test_a_reuse_that_stops_after_it_protected_the_replacement_publishes_nothing_and_leaves_no_hold_on_it(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hold on a version that no sidecar of this filing names would keep it from being collected once its filing is gone."""
    first, second, pointer = _two_filings_of_one_version(vault, selected_store)
    media_store.recopy(vault, first, pointer.store)
    replacement = _entry(vault, first, pointer.store)
    confirmed = media_store._confirmed
    asked: list[object] = []

    def filing_deleted_after_the_hold_was_taken(backend, reference):
        found = confirmed(backend, reference)
        if reference == replacement:
            asked.append(reference)
            if len(asked) == 2:  # the first answer picks the replacement; the second comes after the hold, before the write
                media_store.forget_original(vault, first)
        return found

    monkeypatch.setattr(media_store, "_confirmed", filing_deleted_after_the_hold_was_taken)

    with pytest.raises(media_store.MediaStoreError) as stopped:
        media_store.recopy(vault, second, pointer.store)

    assert stopped.value.code == media_store.STORE_BUSY_CODE
    assert _entry(vault, second, pointer.store) == pointer
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        held = ledger.execute("SELECT 1 FROM media_protections WHERE binary_rel = ? AND object = ?",
                              (second.relative_to(vault).as_posix(), replacement.object)).fetchall()
    assert held == []


def test_a_copy_again_publishes_nothing_when_the_replacement_it_reuses_was_found_damaged_meanwhile(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second filing chose the replacement as good; a finding before its write makes it a version eviction must distrust."""
    first, second, pointer = _two_filings_of_one_version(vault, selected_store)
    media_store.recopy(vault, first, pointer.store)
    replacement = _entry(vault, first, pointer.store)
    confirmed = media_store._confirmed

    def found_wrong_right_after_the_store_answered_well(backend, reference):
        answer = confirmed(backend, reference)
        if reference == replacement:
            media_replicas.quarantine(vault, first.relative_to(vault).as_posix(), replacement,
                                      level=media_replicas.DECRYPTED, failure=media_replicas.PLAINTEXT_MISMATCH)
        return answer

    monkeypatch.setattr(media_store, "_confirmed", found_wrong_right_after_the_store_answered_well)

    with pytest.raises(media_store.MediaStoreError) as stopped:
        media_store.recopy(vault, second, pointer.store)

    assert stopped.value.code == media_store.STORE_BUSY_CODE
    assert _entry(vault, second, pointer.store) == pointer
    assert (second.relative_to(vault).as_posix(), media_replicas.RECOPY) in _requests(vault)


@pytest.mark.parametrize("through", [
    pytest.param("offload", id="offload-of-a-filing-that-no-version-serves"),
    pytest.param("recopy", id="copy-again-of-a-damaged-version"),
])
def test_an_upload_drops_the_hold_that_a_stopped_reuse_left_on_a_version_it_does_not_publish(
        vault: Path, selected_store, through: str) -> None:
    """A crash after the hold leaves it behind; no sidecar of the filing names the version, so nothing would collect it."""
    if through == "offload":
        filing = _preserved(vault, category="first")
    else:
        filing, _second, pointer = _two_filings_of_one_version(vault, selected_store)
    relative = filing.relative_to(vault).as_posix()
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("INSERT INTO media_protections(token, binary_rel, object, created_at, uploadable, store) "
                       "VALUES ('stale', ?, ?, ?, 0, '')", (relative, "c" * 64, time.time()))

    if through == "offload":
        _offload(vault, filing)
    else:
        media_store.recopy(vault, filing, pointer.store)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        assert ledger.execute("SELECT object FROM media_protections WHERE binary_rel = ?", (relative,)).fetchall() == []


def test_a_filing_with_local_bytes_does_not_take_a_replacement_of_an_older_key(vault: Path, selected_store) -> None:
    """It uploads its own copy under the current key: a version of an older key may be one that this install cannot decrypt."""
    first, second, pointer = _two_filings_of_one_version(vault, selected_store)
    media_store.recopy(vault, first, pointer.store)
    replacement = _entry(vault, first, pointer.store)
    _configure(selected_store, vault, key_version="2")

    media_store.recopy(vault, second, pointer.store)

    fresh = _entry(vault, second, pointer.store)
    assert fresh.key_version == "2"
    assert fresh.object not in {pointer.object, replacement.object}


def test_a_filing_whose_local_bytes_are_damaged_takes_the_replacement_of_an_older_key(vault: Path, selected_store) -> None:
    """Bytes that do not verify are no source: the older-key replacement is all it can take, and the filings stay one version."""
    first, second, pointer = _two_filings_of_one_version(vault, selected_store)
    media_store.recopy(vault, first, pointer.store)
    replacement = _entry(vault, first, pointer.store)
    _configure(selected_store, vault, key_version="2")
    _damage_a_byte(second)

    media_store.recopy(vault, second, pointer.store)

    assert _entry(vault, second, pointer.store) == replacement
    assert replacement.key_version == "1"


def test_offload_that_finds_a_candidate_gone_from_the_store_records_it_when_its_filing_has_no_bytes_left(
        vault: Path, selected_store) -> None:
    """Nothing else will find it: the filing cannot be evicted again, and the new version is its replacement."""
    lost = _preserved(vault, category="a-lost")
    gone = _offload(vault, lost)
    assert media_store.evict(vault, lost)
    (Path(selected_store[0]["root"]) / gone.object / gone.version).unlink()
    other = _preserved(vault, category="b-other")

    fresh = _offload(vault, other)

    assert fresh.object != gone.object
    row = _replica_row_of(vault, lost, gone.store)
    assert (row["state"], row["failure"]) == (media_replicas.QUARANTINED, "absent")
    _recopy_all(vault)
    assert _entry(vault, lost, gone.store) == fresh


def test_offload_that_finds_a_candidate_gone_from_the_store_reuses_the_next_version_of_equal_bytes(
        vault: Path, selected_store) -> None:
    """A version that the store lost is passed over, not the end of reuse for these bytes: the next one serves."""
    lost = _preserved(vault, category="a-lost")
    gone = _offload(vault, lost)
    media_replicas.quarantine(vault, lost.relative_to(vault).as_posix(), gone, level=media_replicas.CONTENT,
                              failure="digest_mismatch")
    sound = _preserved(vault, category="b-sound")
    held = _offload(vault, sound)  # the recorded damage gives these equal bytes a version of their own
    assert held.object != gone.object
    root = Path(selected_store[0]["root"])
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("DELETE FROM media_damaged_versions")  # the damage is no longer on record, and the store lost it
        ledger.execute("UPDATE media_replicas SET state = ?, level = NULL, failure = NULL", (media_replicas.UPLOAD_VERIFIED,))
    (root / gone.object / gone.version).unlink()
    stored = _objects(root)
    third = _preserved(vault, category="c-third")

    assert _offload(vault, third) == held

    assert _objects(root) == stored  # nothing was uploaded for the third filing
    assert _replica_row_of(vault, lost, gone.store)["state"] == media_replicas.UPLOAD_VERIFIED  # its bytes are here: eviction releases it


def test_offload_does_not_publish_a_version_that_a_finding_condemned_after_it_was_chosen(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Between the store's answer and the binding's write another worker may find the version bad: eviction trusts the record."""
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    second = _preserved(vault, category="second")
    confirmed = media_store._confirmed

    def found_bad_right_after_it_answered_well(backend, replica):
        answer = confirmed(backend, replica)
        if replica == pointer and answer:
            media_replicas.quarantine(vault, first.relative_to(vault).as_posix(), pointer,
                                      level=media_replicas.CONTENT, failure="digest_mismatch")
        return answer

    monkeypatch.setattr(media_store, "_confirmed", found_bad_right_after_it_answered_well)

    with pytest.raises(media_store.MediaStoreError) as stopped:
        media_store.offload(vault, second)

    assert stopped.value.code == media_store.STORE_BUSY_CODE
    assert originals.original_state(vault, second).reference is None
    monkeypatch.setattr(media_store, "_confirmed", confirmed)

    assert _offload(vault, second).object != pointer.object  # the retry gives these bytes a version of their own

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:  # and keeps no hold on the refused one
        assert ledger.execute("SELECT 1 FROM media_protections WHERE binary_rel = ?",
                              (second.relative_to(vault).as_posix(),)).fetchall() == []


def test_a_filing_published_on_a_version_that_a_finding_condemned_meanwhile_is_written_quarantined_with_its_request(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The finding can also land after the binding was written and before its row: the row inherits the version's damage."""
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    second = _preserved(vault, category="second")
    note = media_store._note_replica

    def found_bad_before_the_row_is_written(root: Path, path: Path, reference: originals.StoredObject, verified_at):
        media_replicas.quarantine(root, first.relative_to(root).as_posix(), pointer, level=media_replicas.CONTENT,
                                  failure="digest_mismatch")
        note(root, path, reference, verified_at)

    monkeypatch.setattr(media_store, "_note_replica", found_bad_before_the_row_is_written)

    assert _offload(vault, second) == pointer

    row = _replica_row_of(vault, second, pointer.store)
    assert (row["state"], row["failure"]) == (media_replicas.QUARANTINED, "digest_mismatch")
    assert (second.relative_to(vault).as_posix(), media_replicas.RECOPY) in _requests(vault)


def test_offload_reads_the_census_and_the_damage_once_however_many_candidates_the_store_lost(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each retry that re-read the census and re-scanned the candidates would make eight lost versions cost eight censuses."""
    lost = 8
    root = Path(selected_store[0]["root"])
    gone = []
    for number in range(lost):
        filing = _preserved(vault, category=f"lost-{number:02d}")
        gone.append(_offload(vault, filing))
        media_replicas.quarantine(vault, filing.relative_to(vault).as_posix(), gone[-1], level=media_replicas.CONTENT,
                                  failure="digest_mismatch")  # the next filing of these bytes gets a version of its own
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("DELETE FROM media_damaged_versions")  # the damage is no longer on record, and the store lost it
        ledger.execute("UPDATE media_replicas SET state = ?, level = NULL, failure = NULL", (media_replicas.UPLOAD_VERIFIED,))
    for reference in gone:
        (root / reference.object / reference.version).unlink()
    censuses = _count_censuses(monkeypatch)
    _offload(vault, _filed(vault, 1)[0])  # bytes of their own, so no candidate: the censuses that an offload reads without any
    baseline = len(censuses)
    recorded, visited = [], []
    damaged_among, head_failure = media_replicas.damaged_among, media_store._head_failure
    monkeypatch.setattr(media_replicas, "damaged_among", lambda *arguments: recorded.append(1) or damaged_among(*arguments))
    monkeypatch.setattr(media_store, "_head_failure",
                        lambda backend, reference: visited.append(reference) or head_failure(backend, reference))

    assert _offload(vault, _preserved(vault, category="last")).object not in {reference.object for reference in gone}

    assert (len(censuses) - baseline, len(recorded)) == (baseline, 1)
    assert sorted(visited, key=lambda reference: reference.object) == sorted(gone, key=lambda reference: reference.object)


# --- verify-stored-replicas: what the owner sees ----------------------------------------------------------


def _mirror_stats(vault: Path) -> dict[str, object]:
    return {stats["name"]: stats for stats in media_replicas.status(vault)["stores"]}[MIRROR]


def test_the_status_keeps_each_proof_level_apart_and_never_calls_a_presence_check_content(
        vault: Path, selected_store, tmp_path: Path) -> None:
    """A store that only echoes the digest it was given must not raise the owner's count of content-verified copies."""
    _declare(vault, selected_store, tmp_path / "mirror", head_recomputes=False)
    _replicated(vault)
    media_replicas.rebuild(vault)
    media_replicas.check(vault)

    status = media_replicas.status(vault)

    levels = {name: {level: proof["replicas"] for level, proof in stats["checks"].items()}
              for name, stats in {stats["name"]: stats for stats in status["stores"]}.items()}
    assert (levels[MIRROR][media_replicas.PRESENCE], levels[MIRROR][media_replicas.CONTENT]) == (1, 0)
    assert levels["test-store"][media_replicas.CONTENT] == 1  # the eviction store recomputes, so its check is content
    assert status["totals"]["checks"][media_replicas.CONTENT]["replicas"] == 1
    assert status["totals"]["never_checked"] == 0


def test_the_status_reports_a_replica_of_an_unscheduled_store_with_the_reason_and_not_as_never_checked(
        vault: Path, selected_store) -> None:
    """A store that states no `head_recomputes` is never checked by design: reading that as a backlog would never clear."""
    _offload(vault, _preserved(vault))
    media_replicas.rebuild(vault)
    media_replicas.check(vault)

    status = media_replicas.status(vault)

    [store] = [stats for stats in status["stores"] if stats["replicas"]]
    assert store["checks_not_scheduled"] == {"replicas": 1, "reasons": [media_replicas.HEAD_NOT_DECLARED]}
    assert store["never_checked"] == 0
    assert status["totals"]["checks_not_scheduled"] == store["checks_not_scheduled"]


def test_the_status_counts_a_replica_that_no_check_has_reached_as_never_checked(
        vault: Path, selected_store, mirrored: Path) -> None:
    _replicated(vault)
    media_replicas.rebuild(vault)
    _due_again(vault)  # as a replica that a new version or a rebuild left with no history

    mirror = _mirror_stats(vault)

    assert (mirror["never_checked"], mirror["checks_not_scheduled"], mirror["quarantined"]) == (1, None, 0)


def test_the_status_gives_the_sample_budget_what_it_spent_and_the_replicas_skipped_for_size(
        vault: Path, selected_store, tmp_path: Path) -> None:
    """A budget that the owner cannot see used up would read as sampling that silently stopped working."""
    mirror_root = tmp_path / "mirror"
    _declare(vault, selected_store, mirror_root)
    paths = [_preserved(vault, category=category) for category in ("a", "b", "c")]
    for path in paths:
        _offload(vault, path)
    _replicate_all(vault)
    one_replica = originals.original_state(vault, paths[0]).replicas[0].ciphertext_size
    _declare(vault, selected_store, mirror_root, sample_bytes_per_month=one_replica + 1)
    _unproven(vault)
    media_replicas.sample(vault)
    media_replicas.rebuild(vault)

    sample = _mirror_stats(vault)["sample"]

    assert (sample["budget_bytes"], sample["spent_bytes"], sample["skipped_for_size"]) == (one_replica + 1, one_replica, 2)
    assert sample["not_sampled"] == []
    assert _mirror_stats(vault)["checks"][media_replicas.DECRYPTED]["replicas"] == 1


def test_the_status_gives_the_reason_for_each_replica_that_is_not_sampled(
        vault: Path, selected_store, tmp_path: Path) -> None:
    _declare(vault, selected_store, tmp_path / "mirror", sample_bytes_per_month=0)
    _replicated(vault)
    media_replicas.rebuild(vault)

    sample = _mirror_stats(vault)["sample"]

    assert (sample["budget_bytes"], sample["not_sampled"]) == (0, [{"reason": media_replicas.SAMPLING_OFF, "replicas": 1}])


def test_a_finding_reaches_the_owner_through_the_status_and_the_findings_list_with_its_copy_again_pending(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The drill's last step: a damaged byte the owner is never told about is the same as no check."""
    relative, damaged = _replicated(vault)
    _damage_a_byte(_stored_file(mirrored, damaged))
    _due_again(vault)
    media_replicas.check(vault)
    media_replicas.rebuild(vault)

    assert _mirror_stats(vault)["quarantined"] == 1
    [finding] = media_replicas.findings(vault)["findings"]

    assert (finding["path"], finding["store"], finding["failed_level"], finding["failure"]) == (
        relative, MIRROR, media_replicas.CONTENT, "digest_mismatch")
    assert (finding["object"], finding["version"]) == (damaged.object, damaged.version)  # the version that stays
    assert finding["found_at"] is not None
    assert finding["recopy"] == {"state": media_replicas.PENDING, "code": None}


def test_the_findings_list_reads_a_page_at_a_time_by_path_and_store(vault: Path, selected_store, mirrored: Path) -> None:
    paths = [_preserved(vault, category=category) for category in ("a", "b", "c")]
    for path in paths:
        _offload(vault, path)
    _replicate_all(vault)
    for path in paths:
        [replica] = originals.original_state(vault, path).replicas
        _damage_a_byte(_stored_file(mirrored, replica))
    _due_again(vault)
    media_replicas.check(vault)

    first = media_replicas.findings(vault, limit=2)
    second = media_replicas.findings(vault, after=tuple(first["next_after"]), limit=2)

    listed = [item["path"] for item in (*first["findings"], *second["findings"])]
    assert listed == sorted(path.relative_to(vault).as_posix() for path in paths)
    assert (len(first["findings"]), second["next_after"]) == (2, None)


def test_a_finding_that_no_copy_can_replace_names_the_refusal(vault: Path, selected_store, mirrored: Path) -> None:
    values, _config = selected_store
    relative, damaged = _replicated(vault)
    pointer = originals.original_state(vault, relative).reference
    assert media_store.evict(vault, vault / relative)
    _damage_a_byte(_stored_file(mirrored, damaged))
    _damage_a_byte(Path(values["root"]) / pointer.object / pointer.version)
    _due_again(vault)
    media_replicas.check(vault)
    _recopy_all(vault)

    recopies = {item["store"]: item["recopy"] for item in media_replicas.findings(vault)["findings"]}

    assert recopies == {MIRROR: {"state": media_replicas.REFUSED, "code": media_store.NO_VERIFIED_SOURCE},
                        pointer.store: {"state": media_replicas.REFUSED, "code": media_store.NO_VERIFIED_SOURCE}}


def test_the_per_original_view_and_the_page_show_each_replicas_check_and_its_finding(
        vault: Path, selected_store, mirrored: Path) -> None:
    relative, damaged = _replicated(vault)
    media_replicas.rebuild(vault)
    _due_again(vault)
    media_replicas.check(vault)
    [passed] = [item for item in media_replicas.original_view(vault, relative)["replicas"] if item["store"] == MIRROR]
    assert (passed["last_check"]["level"], passed["finding"]) == (media_replicas.CONTENT, None)

    _damage_a_byte(_stored_file(mirrored, damaged))
    _due_again(vault)
    media_replicas.check(vault)

    for view in (media_replicas.original_view(vault, relative)["replicas"],
                 media_replicas.page(vault)["originals"][0]["replicas"]):
        [found] = [item for item in view if item["store"] == MIRROR]
        assert (found["state"], found["last_check"], found["finding"]["failure"]) == (
            media_replicas.QUARANTINED, None, "digest_mismatch")


def test_the_findings_pick_reads_the_index_of_quarantined_rows_in_path_order(vault: Path, selected_store) -> None:
    _offload(vault, _preserved(vault))
    media_replicas.rebuild(vault)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        plan = " ".join(row[3] for row in ledger.execute("EXPLAIN QUERY PLAN " + media_replicas._FINDINGS, ("", "", 8)))

    assert "USING INDEX media_replicas_findings" in plan and "TEMP B-TREE" not in plan
    assert "USING PRIMARY KEY" in plan or "USING COVERING INDEX" in plan  # the queue is read by its key


def test_the_findings_command_prints_the_page_that_the_owner_reads(
        vault: Path, selected_store, mirrored: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from exomem import __main__ as cli

    relative, damaged = _replicated(vault)
    _damage_a_byte(_stored_file(mirrored, damaged))
    _due_again(vault)
    media_replicas.check(vault)

    assert cli._media_replicas_main(["findings", "--vault", str(vault)]) == 0

    [finding] = json.loads(capsys.readouterr().out)["findings"]
    assert (finding["path"], finding["store"]) == (relative, MIRROR)


# --- verify-stored-replicas, review round 1: what is not damage, one bad row, the round's size -------------------


def _another_identity(directory: Path) -> bytes:
    """The text of an age identity that is not the vault's: a key that opens none of its ciphertext."""
    path = directory / "another-identity"
    subprocess.run(["age-keygen", "-o", str(path)], capture_output=True, check=True)
    return path.read_bytes()


def _age_stopped_by_a_signal(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put an `age` ahead of the real one on PATH that dies by SIGTERM, as a memory kill or a shutdown ends it."""
    shim = directory / "shim"
    shim.mkdir()
    (shim / "age").write_text("#!/bin/sh\nkill -TERM $$\n")
    (shim / "age").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")


def _hand_edit_mirror_version(vault: Path, relative: str, version: str) -> None:
    """Rewrite the version that an original's binding lists for the mirror, as a person editing the sidecar would."""
    snapshot = originals.original_state(vault, relative)
    mirror = next(entry for entry in snapshot.replicas if entry.store == MIRROR)
    edited = originals.StoredObject(1, MIRROR, mirror.key_version, mirror.object, version, mirror.ciphertext_size,
                                    mirror.ciphertext_sha256)
    media_store._publishing(vault, "test_hand_edit", lambda: media_store._commit_reference(
        vault, media_store.prepare_replica(vault, snapshot, edited)))


def test_a_store_with_no_declared_descriptor_is_never_sampled_and_no_bytes_are_charged(
        vault: Path, selected_store) -> None:
    """A Cloud cell's single store declares no `stores` list: a sample there would read and bill a store checks skip."""
    _offload(vault, _preserved(vault))
    media_replicas.rebuild(vault)

    assert media_replicas.sample(vault) == 0

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        assert ledger.execute("SELECT COUNT(*) FROM media_replica_samples").fetchone() == (0,)
        assert ledger.execute("SELECT decrypted_at FROM media_replicas").fetchall() == [(0.0,)]


def test_the_status_gives_a_store_whose_checks_are_not_scheduled_as_the_reason_it_is_not_sampled(
        vault: Path, selected_store) -> None:
    _offload(vault, _preserved(vault))
    media_replicas.rebuild(vault)

    [store] = [stats for stats in media_replicas.status(vault)["stores"] if stats["replicas"]]

    assert store["sample"]["not_sampled"] == [{"reason": media_replicas.HEAD_NOT_DECLARED, "replicas": 1}]


def test_a_sample_whose_identity_refuses_matching_ciphertext_is_a_key_finding_and_no_quarantine(
        vault: Path, selected_store, mirrored: Path, tmp_path: Path) -> None:
    """A wrong identity file is the owner's to fix: it must not read as damage and queue a copy again of every replica."""
    values, _config = selected_store
    _replicated(vault)
    _unproven(vault)
    Path(values["identity_path"]).write_bytes(_another_identity(tmp_path))

    media_replicas.sample(vault)

    row = _replica_row(vault)
    assert (row["state"], row["failure"]) == (media_replicas.UPLOAD_VERIFIED, media_store.KEY_REFUSED)
    assert [item for item in _queued(vault) if item[1] == media_replicas.RECOPY] == []
    assert _mirror_stats(vault)["failed_attempts"] == [{"failure": media_store.KEY_REFUSED, "replicas": 1}]


def test_a_repair_restores_an_original_from_a_replica_after_a_wrong_identity_was_put_right(
        vault: Path, selected_store, mirrored: Path, tmp_path: Path) -> None:
    """The point of not quarantining: the replicas that a wrong key refused are still the owner's copies once it is right."""
    values, _config = selected_store
    original = _stored(vault, "Recordings")
    reference = _offload(vault, original)
    _replicate_all(vault)
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    identity = Path(values["identity_path"])
    right = identity.read_bytes()
    identity.write_bytes(_another_identity(tmp_path))
    _unproven(vault)
    media_replicas.sample(vault)
    identity.write_bytes(right)
    original.write_bytes(DAMAGED)
    first = Path(values["root"]) / reference.object / reference.version
    first.write_bytes(os.urandom(first.stat().st_size))

    assert original_fixity.run_pass(vault, budget=0).outcomes == {"restored": 1}

    assert original.read_bytes() == TRANSCRIPT


def test_a_sample_whose_age_is_stopped_by_a_signal_quarantines_nothing(
        vault: Path, selected_store, mirrored: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A killed decryption says nothing about the bytes: a nightly memory kill must not condemn every replica it meets."""
    _replicated(vault)
    _unproven(vault)
    _age_stopped_by_a_signal(tmp_path, monkeypatch)

    media_replicas.sample(vault)

    row = _replica_row(vault)
    assert (row["state"], row["failure"]) == (media_replicas.UPLOAD_VERIFIED, media_replicas.READ_FAILED)
    assert [item for item in _queued(vault) if item[1] == media_replicas.RECOPY] == []


def test_a_sample_that_stops_part_way_still_has_its_bytes_charged_to_the_month(
        vault: Path, selected_store, mirrored: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The egress bill counts what was read, not what finished: an uncharged failure could repeat all month for free."""
    _relative, replica = _replicated(vault)
    _unproven(vault)
    _age_stopped_by_a_signal(tmp_path, monkeypatch)

    media_replicas.sample(vault)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        assert ledger.execute("SELECT bytes FROM media_replica_samples WHERE store = ?", (MIRROR,)).fetchone() == (
            replica.ciphertext_size,)


def test_a_sample_that_decrypts_a_version_lifts_every_filing_that_a_plaintext_finding_quarantined_with_it(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The proof is the version's: a filing left quarantined would upload a copy of a version just proven good."""
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    second = _preserved(vault, category="second")
    assert _offload(vault, second) == pointer
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("UPDATE media_replicas SET ciphertext_sha256 = NULL WHERE binary_rel = ?",
                       (second.relative_to(vault).as_posix(),))  # a row of an earlier release: no digest to be found by
    media_replicas.quarantine(vault, first.relative_to(vault).as_posix(), pointer, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)
    assert _replica_row_of(vault, second, pointer.store)["state"] == media_replicas.QUARANTINED  # found by its version
    assert (second.relative_to(vault).as_posix(), media_replicas.RECOPY) in _requests(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("UPDATE media_replicas SET ciphertext_sha256 = ? WHERE binary_rel = ?",
                       (pointer.ciphertext_sha256, second.relative_to(vault).as_posix()))  # as a rebuild fills it
    _unproven(vault)
    stored = _objects(Path(selected_store[0]["root"]))

    media_replicas.sample(vault)  # one round decrypts one of the two quarantined filings
    _recopy_all(vault)

    for original in (first, second):
        assert _replica_row_of(vault, original, pointer.store)["state"] == media_replicas.UPLOAD_VERIFIED
        assert _entry(vault, original, pointer.store) == pointer
    assert _requests(vault) == []
    assert _objects(Path(selected_store[0]["root"])) == stored  # no copy of the version was uploaded
    assert not media_replicas.has_quarantined(vault, (pointer,))


def _vm_steps_of_a_release(vault: Path, unrelated: int, monkeypatch: pytest.MonkeyPatch) -> int:
    """Release one version of two filings beside `unrelated` quarantined replicas of other versions; count SQLite's steps."""
    version = originals.StoredObject(1, MIRROR, "1", f"{unrelated:064x}", "version", 10, "0" * 64)
    quarantined = ("INSERT INTO media_replicas(binary_rel, store, object, version, key_version, size, verified_at, state, "
                   "level, failure, seen, ciphertext_sha256) VALUES (?, ?, ?, ?, '1', 10, 0, 'quarantined', 'decrypted', "
                   f"'{media_replicas.PLAINTEXT_MISMATCH}', 1, ?)")
    with closing(media_jobs.MediaJobStore(vault)._connect()) as ledger, ledger:
        ledger.executemany(quarantined, [(f"Originals/{unrelated}/{number}.bin", MIRROR, f"{number:064x}", f"other-{number}",
                                          f"{number:064x}") for number in range(unrelated)])
        ledger.executemany(quarantined, [(f"Originals/{unrelated}/pair-{number}.bin", MIRROR, version.object, version.version,
                                          version.ciphertext_sha256) for number in range(2)])
        ledger.execute("INSERT INTO media_damaged_versions(store, object, version, level, failure, ciphertext_sha256) "
                       f"VALUES (?, ?, ?, 'decrypted', '{media_replicas.PLAINTEXT_MISMATCH}', ?)",
                       (MIRROR, version.object, version.version, version.ciphertext_sha256))
        ledger.execute("INSERT INTO media_damaged_digests(ciphertext_sha256, generation) VALUES (?, 7)",
                       (version.ciphertext_sha256,))
    steps = [0]
    connect = media_jobs.MediaJobStore._connect

    def counting(self, **options):
        conn = connect(self, **options)
        conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1), 1)
        return conn

    monkeypatch.setattr(media_jobs.MediaJobStore, "_connect", counting)
    media_replicas._released(vault, {"binary_rel": f"Originals/{unrelated}/pair-0.bin", "store": MIRROR, "generation": 7},
                             version)
    assert not media_replicas.has_quarantined(vault, (version,))
    return steps[0]


def test_a_release_costs_what_it_costs_beside_many_quarantines_of_other_versions(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The release reads the filings of its own version: a scan of every plaintext quarantine would make each one cost n."""
    few = _vm_steps_of_a_release(vault, 20, monkeypatch)
    many = _vm_steps_of_a_release(vault, 10_000, monkeypatch)

    assert many < few * 2 + 500


def test_a_replica_quarantined_for_damaged_ciphertext_stays_quarantined_through_a_sample(
        vault: Path, selected_store, mirrored: Path) -> None:
    """Only a new copy replaces damaged bytes: a sample must not lift the finding that a check or a repair made."""
    relative, replica = _replicated(vault)
    media_replicas.quarantine(vault, relative, replica, level=media_replicas.CONTENT, failure="digest_mismatch")
    _unproven(vault)

    media_replicas.sample(vault)

    assert _replica_row(vault)["state"] == media_replicas.QUARANTINED
    assert [item[:2] for item in _queued(vault) if item[0] == MIRROR] == [(MIRROR, media_replicas.RECOPY)]


@pytest.mark.parametrize(("level", "failure"), [
    pytest.param(media_replicas.CONTENT, "digest_mismatch", id="the-ciphertext-is-found-damaged"),
    pytest.param(media_replicas.DECRYPTED, media_replicas.PLAINTEXT_MISMATCH, id="the-plaintext-is-found-wrong-again"),
])
def test_a_sample_that_decrypts_a_version_leaves_a_finding_that_another_filing_recorded_after_it_was_picked(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch, level: str, failure: str) -> None:
    """A filing's request does not name a finding made through its sibling: only the version's record does.

    The sample picks one filing and decrypts the version; a sibling then records a newer finding on it. The proof is
    older than that finding, so it lifts nothing: the filing and the sibling stay quarantined with their requests.
    """
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    second = _preserved(vault, category="second")
    assert _offload(vault, second) == pointer
    held = {original: original.relative_to(vault).as_posix() for original in (first, second)}
    media_replicas.quarantine(vault, held[first], pointer, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)
    _unproven(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("UPDATE media_replicas SET sample_from = ? WHERE binary_rel = ?", (time.time() + 3600, held[second]))
    picked = []
    sample_replica = media_store.sample_replica

    def sample_then_the_sibling_finds_it_bad(root: Path, entry: originals.StoredObject, head: media_store.ObjectHead):
        damage = sample_replica(root, entry, head)
        if not picked:
            picked.append(entry)
            media_replicas.quarantine(root, held[second], entry, level=level, failure=failure)
        return damage

    monkeypatch.setattr(media_store, "sample_replica", sample_then_the_sibling_finds_it_bad)

    media_replicas.sample(vault)

    assert picked  # the sample ran: the version was decrypted before the sibling's finding settled
    for original in (first, second):
        row = _replica_row_of(vault, original, pointer.store)
        assert (row["state"], row["failure"]) == (media_replicas.QUARANTINED, failure)  # the version's newest finding
        assert (held[original], media_replicas.RECOPY) in _requests(vault)
    assert media_replicas.has_quarantined(vault, (pointer,))


def test_a_sample_that_decrypts_a_copy_leaves_a_finding_that_the_pointer_recorded_again_after_it_was_picked(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The release answers every filing of the ciphertext, so a newer finding on any version of it must stop it.

    The pointer and the mirror's copy hold the same ciphertext under two names and each has its own damage record.
    The sample picks the copy; the pointer's wrong plaintext is then found again. The copy's proof is older than
    that finding, so it releases neither filing and removes no record.
    """
    relative, pointer, mirror = _evicted_with_a_pointer_found_wrong(vault)
    _unproven(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("UPDATE media_replicas SET sample_from = ? WHERE store = ?", (time.time() + 3600, pointer.store))
    found_again = []
    sample_replica = media_store.sample_replica

    def sample_then_the_pointer_is_found_wrong_again(root: Path, entry: originals.StoredObject, head: media_store.ObjectHead):
        damage = sample_replica(root, entry, head)
        if not found_again:
            found_again.append(entry)
            media_replicas.quarantine(root, relative, pointer, level=media_replicas.DECRYPTED,
                                      failure=media_replicas.PLAINTEXT_MISMATCH)
        return damage

    monkeypatch.setattr(media_store, "sample_replica", sample_then_the_pointer_is_found_wrong_again)

    media_replicas.sample(vault)

    assert found_again == [mirror]  # the copy was the pick: the pointer's finding settled after its download
    assert _replica_row(vault)["state"] == media_replicas.QUARANTINED
    assert media_replicas.has_quarantined(vault, (pointer,)) and media_replicas.has_quarantined(vault, (mirror,))
    assert sorted(item[0] for item in _queued(vault) if item[1] == media_replicas.RECOPY) == sorted([MIRROR, pointer.store])


def _evicted_with_a_pointer_found_wrong(vault: Path) -> tuple[str, originals.StoredObject, originals.StoredObject]:
    """An evicted original, its pointer and its mirror replica (a byte-for-byte copy), then a wrong-plaintext finding on the pointer."""
    original = _preserved(vault)
    pointer = _offload(vault, original)
    assert media_store.evict(vault, original)
    _replicate_all(vault)
    relative = original.relative_to(vault).as_posix()
    [mirror] = originals.original_state(vault, original).replicas
    media_replicas.quarantine(vault, relative, pointer, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)
    return relative, pointer, mirror


def test_a_pointer_is_not_copied_back_from_a_replica_that_was_a_copy_of_the_version_found_wrong(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The mirror holds the pointer's ciphertext under another name: its bytes decrypt wrong exactly as the pointer's do."""
    relative, pointer, _mirror = _evicted_with_a_pointer_found_wrong(vault)

    media_replicas.drain(vault)  # one unit, not `_recopy_all`: a copy that was trusted would be queued for copying again, for ever

    assert _entry(vault, relative, pointer.store) == pointer
    assert [item[2:] for item in _queued(vault) if item[0] == pointer.store] == [
        (media_replicas.REFUSED, "no_verified_source")]


def test_a_finding_quarantines_the_copy_of_its_ciphertext_under_another_name_at_once(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The copy has its own object name, so only its ciphertext says that it is as wrong as the pointer found wrong."""
    relative, _pointer, mirror = _evicted_with_a_pointer_found_wrong(vault)

    row = _replica_row(vault)  # no rebuild ran: the finding itself reached the copy through its row
    assert (row["state"], row["failure"]) == (media_replicas.QUARANTINED, media_replicas.PLAINTEXT_MISMATCH)
    assert (MIRROR, media_replicas.RECOPY) in [item[:2] for item in _queued(vault)]
    assert media_replicas.has_quarantined(vault, (mirror,))
    assert (relative, media_replicas.RECOPY) in _requests(vault)


def test_a_finding_on_a_filing_that_has_no_row_writes_its_row_with_the_ciphertext_digest(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A row without the digest is reached by no later finding and no release until a rebuild fills it."""
    pointer = originals.StoredObject(1, MIRROR, "1", "a" * 64, "version", 10, "b" * 64)

    media_replicas.quarantine(vault, "Originals/lost.bin", pointer, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        assert ledger.execute("SELECT state, ciphertext_sha256 FROM media_replicas").fetchall() == [
            (media_replicas.QUARANTINED, pointer.ciphertext_sha256)]


def test_a_rebuild_quarantines_the_copy_whose_row_an_earlier_release_wrote_with_no_digest(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A row without a digest cannot be found by it: the rebuild that fills the digest is where the copy is reached."""
    relative, _pointer, mirror = _evicted_with_a_pointer_found_wrong(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("UPDATE media_replicas SET state = ?, level = NULL, failure = NULL, ciphertext_sha256 = NULL "
                       "WHERE store = ?", (media_replicas.UPLOAD_VERIFIED, MIRROR))
        ledger.execute("DELETE FROM media_damaged_versions WHERE store = ?", (MIRROR,))
        ledger.execute("DELETE FROM media_replica_queue WHERE store = ?", (MIRROR,))

    media_replicas.rebuild(vault)

    row = _replica_row(vault)
    assert (row["state"], row["failure"], row["ciphertext_sha256"]) == (
        media_replicas.QUARANTINED, media_replicas.PLAINTEXT_MISMATCH, mirror.ciphertext_sha256)
    assert (relative, media_replicas.RECOPY) in _requests(vault)


def test_a_later_finding_of_another_cause_on_a_version_found_wrong_does_not_make_a_later_copy_of_its_bytes_good(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The store losing the version says nothing about its bytes: a copy of them made since still decrypts wrong."""
    pointer = originals.StoredObject(1, MIRROR, "1", "a" * 64, "version", 10, "b" * 64)
    media_replicas.quarantine(vault, "Originals/first.bin", pointer, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)
    media_replicas.quarantine(vault, "Originals/first.bin", pointer, level=media_replicas.CONTENT, failure="absent")
    later = originals.StoredObject(1, MIRROR, "1", "c" * 64, "later", 10, pointer.ciphertext_sha256)

    media_replicas.record(vault, "Originals/later.bin", later, verified_at=time.time())

    assert media_replicas.has_quarantined(vault, (later,))


def test_a_sample_that_decrypts_a_version_found_wrong_releases_its_copies_under_other_names(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The proof is about the ciphertext: a copy that the finding quarantined with it must not stay and be repaired."""
    relative, pointer, mirror = _evicted_with_a_pointer_found_wrong(vault)
    assert _replica_row(vault)["state"] == media_replicas.QUARANTINED
    _unproven(vault)

    media_replicas.sample(vault)

    assert _replica_row(vault)["state"] == media_replicas.UPLOAD_VERIFIED
    assert not media_replicas.has_quarantined(vault, (pointer, mirror))
    assert [item for item in _requests(vault) if item[0] == relative] == []


def test_a_finding_of_another_cause_moves_the_filings_of_a_wrong_plaintext_to_that_cause_and_no_sample_lifts_them(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The version's record names one cause, the newest: a filing that kept the old one would be lifted by a proof of bytes."""
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    second = _preserved(vault, category="second")
    assert _offload(vault, second) == pointer
    held = {original: original.relative_to(vault).as_posix() for original in (first, second)}
    media_replicas.quarantine(vault, held[first], pointer, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)
    media_replicas.quarantine(vault, held[second], pointer, level=media_replicas.CONTENT, failure="absent")
    _unproven(vault)

    media_replicas.sample(vault)

    for original in (first, second):
        row = _replica_row_of(vault, original, pointer.store)
        assert (row["state"], row["level"], row["failure"]) == (media_replicas.QUARANTINED, media_replicas.CONTENT, "absent")
        assert (held[original], media_replicas.RECOPY) in _requests(vault)
    assert media_replicas.has_quarantined(vault, (pointer,))


def test_a_finding_that_changes_the_cause_of_a_version_moves_every_quarantined_filing_of_it_so_no_release_strands_one(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The version's record names one cause, the newest: a filing that kept an older one would outlive the record's release.

    Two filings share a version that a check found damaged. A later finding of a wrong plaintext changes the record's
    cause, and the filing of the other name that stayed quarantined as damaged would be left with no record when a sample
    proves the ciphertext and releases it: no gate would see that row, and eviction would drop the bytes that it must copy.
    """
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    second = _preserved(vault, category="second")
    assert _offload(vault, second) == pointer
    held = {original: original.relative_to(vault).as_posix() for original in (first, second)}
    media_replicas.quarantine(vault, held[first], pointer, level=media_replicas.CONTENT, failure="digest_mismatch")
    assert _replica_row_of(vault, second, pointer.store)["failure"] == "digest_mismatch"

    media_replicas.quarantine(vault, held[first], pointer, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)

    for original in (first, second):  # the record names a wrong plaintext now, so each filing says the same
        assert _replica_row_of(vault, original, pointer.store)["failure"] == media_replicas.PLAINTEXT_MISMATCH
    _unproven(vault)

    media_replicas.sample(vault)  # the ciphertext decrypts to the binding: a false alarm for the whole version

    for original in (first, second):
        assert _replica_row_of(vault, original, pointer.store)["state"] == media_replicas.UPLOAD_VERIFIED
    assert not media_replicas.has_quarantined(vault, (pointer,))
    assert _requests(vault) == []


def test_a_sample_that_decrypts_a_copy_of_a_version_found_wrong_leaves_the_filing_whose_version_was_found_lost_since(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The proof is that the ciphertext decrypts. It answers nothing about a version that the store has since lost."""
    first = _preserved(vault, category="first")
    pointer = _offload(vault, first)
    second = _preserved(vault, category="second")
    assert _offload(vault, second) == pointer
    assert media_store.evict(vault, first) and media_store.evict(vault, second)
    _replicate_all(vault)  # each mirror copy is the pointer's ciphertext under another name
    held = {original: original.relative_to(vault).as_posix() for original in (first, second)}
    media_replicas.quarantine(vault, held[first], pointer, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)
    media_replicas.quarantine(vault, held[second], pointer, level=media_replicas.CONTENT, failure="absent")

    def recopies() -> list[str]:
        return sorted(item[0] for item in _queued(vault) if item[1] == media_replicas.RECOPY)

    assert recopies() == sorted([MIRROR, MIRROR, pointer.store, pointer.store])  # the mirror's copies were found wrong with it
    _unproven(vault)

    media_replicas.sample(vault)  # the mirror's copy decrypts to the binding: a false alarm for the ciphertext

    for original in (first, second):
        assert _replica_row_of(vault, original, MIRROR)["state"] == media_replicas.UPLOAD_VERIFIED
        row = _replica_row_of(vault, original, pointer.store)
        assert (row["state"], row["failure"]) == (media_replicas.QUARANTINED, "absent")
    assert recopies() == [pointer.store] * 2
    assert media_replicas.has_quarantined(vault, (pointer,))


def test_the_status_and_the_original_view_count_a_copy_of_ciphertext_found_wrong_as_damaged_before_any_rebuild(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The owner's count of surviving copies cannot lag a finding: the copy under another name is as wrong as the pointer."""
    original = _preserved(vault)
    pointer = _offload(vault, original)
    assert media_store.evict(vault, original)
    _replicate_all(vault)  # the mirror's copy is the pointer's ciphertext under another name
    media_replicas.confirm_key_export(vault)
    media_replicas.rebuild(vault)
    relative = original.relative_to(vault).as_posix()
    assert media_replicas.status(vault)["totals"]["recoverable"] == 2

    media_replicas.quarantine(vault, relative, pointer, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)  # no rebuild after it

    status = media_replicas.status(vault)
    shown = {item["store"]: (item["recoverable"], item["reason"])
             for item in media_replicas.original_view(vault, relative)["replicas"]}
    assert (status["totals"]["recoverable"], status["totals"]["not_recoverable"]) == (0, 2)
    assert {stats["name"]: stats["quarantined"] for stats in status["stores"]} == {MIRROR: 1, pointer.store: 1}
    assert shown == {MIRROR: (False, media_replicas.QUARANTINED), pointer.store: (False, media_replicas.QUARANTINED)}
    assert sorted(item["store"] for item in media_replicas.findings(vault)["findings"]) == sorted([MIRROR, pointer.store])


def _vm_steps_of_a_reproof_pick(vault: Path, stale: int) -> int:
    """Quarantine `stale` filings of one version for a wrong plaintext, then for a lost version; count the steps of the pick."""
    with closing(media_jobs.MediaJobStore(vault)._connect()) as ledger, ledger:  # an earlier size's rows would be picked first
        ledger.execute("DELETE FROM media_replicas")
        ledger.execute("DELETE FROM media_damaged_versions")
    version = originals.StoredObject(1, MIRROR, "1", f"{stale:064x}", "version", 10, f"{stale + 1:064x}")
    live = originals.StoredObject(1, MIRROR, "1", f"{stale + 2:064x}", "version", 10, f"{stale + 3:064x}")
    filings = [f"Originals/{stale}/{number}.bin" for number in range(stale)]
    media_replicas.quarantine_all(vault, filings, version, level=media_replicas.DECRYPTED,
                                  failure=media_replicas.PLAINTEXT_MISMATCH)
    media_replicas.quarantine_all(vault, filings[:1], version, level=media_replicas.CONTENT, failure="absent")
    media_replicas.quarantine_all(vault, [f"Originals/{stale}/live.bin"], live, level=media_replicas.DECRYPTED,
                                  failure=media_replicas.PLAINTEXT_MISMATCH)
    steps = [0]
    with closing(media_jobs.MediaJobStore(vault)._connect()) as ledger:
        ledger.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1), 1)
        picked = ledger.execute(media_replicas._REPROOF, (MIRROR, "1", time.time() + 60)).fetchone()
    assert picked["object"] != version.object  # a filing that a wrong plaintext still holds, never one moved on
    return steps[0]


def test_the_pick_of_a_quarantine_to_release_costs_what_it_costs_beside_many_filings_that_a_later_finding_moved_on(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A filing that a later finding of another cause left in the pick's index would be read and rejected, one by one."""
    few = _vm_steps_of_a_reproof_pick(vault, 20)
    many = _vm_steps_of_a_reproof_pick(vault, 10_000)

    assert many < few * 2 + 500


def test_one_unreadable_row_does_not_stop_the_checks_of_the_rows_behind_it(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A row that always fails and always comes first would starve every other replica of its store of a check."""
    paths = [_preserved(vault, category=category) for category in ("a", "b", "c")]
    for path in paths:
        _offload(vault, path)
    _replicate_all(vault)
    relatives = sorted(path.relative_to(vault).as_posix() for path in paths)
    _hand_edit_mirror_version(vault, relatives[0], "a/b")  # the first row of the round is the unreadable one
    media_replicas.rebuild(vault)
    _due_again(vault)

    media_replicas.check(vault)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        rows = {path: (state, failure, checked_at > 0) for path, state, failure, checked_at in ledger.execute(
            "SELECT binary_rel, state, failure, checked_at FROM media_replicas WHERE store = ?", (MIRROR,))}
    assert rows == {relatives[0]: (media_replicas.UPLOAD_VERIFIED, media_replicas.RECORD_INVALID, False),
                    relatives[1]: (media_replicas.UPLOAD_VERIFIED, None, True),
                    relatives[2]: (media_replicas.UPLOAD_VERIFIED, None, True)}
    assert _mirror_stats(vault)["failed_attempts"] == [{"failure": media_replicas.RECORD_INVALID, "replicas": 1}]


def test_a_binding_that_cannot_be_read_now_does_not_make_its_replica_unsampleable(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A moment's read error is not a fact about the replica: marked unsampleable it would never be sampled again."""
    _replicated(vault)
    _unproven(vault)
    original_state = originals.original_state

    def busy(*args, **kwargs):
        raise OSError("busy")

    monkeypatch.setattr(originals, "original_state", busy)
    media_replicas.sample(vault)
    monkeypatch.setattr(originals, "original_state", original_state)

    row = _replica_row(vault)
    assert (row["sample_month"], row["failure"]) == (0, media_replicas.UNREADABLE)
    assert not any(item["reason"] == media_replicas.NO_EXACT_DIGEST for item in _mirror_stats(vault)["sample"]["not_sampled"])


def test_a_quarantined_replica_is_not_recoverable_in_the_status_and_in_the_original_view(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The count is the owner's answer to "do I survive losing this machine": a damaged copy is not one of the survivors."""
    relative, replica = _replicated(vault)
    media_replicas.confirm_key_export(vault)
    media_replicas.quarantine(vault, relative, replica, level=media_replicas.CONTENT, failure="digest_mismatch")

    totals = _measured(vault)["totals"]
    shown = {item["store"]: (item["recoverable"], item["reason"])
             for item in media_replicas.original_view(vault, relative)["replicas"]}

    assert (totals["recoverable"], totals["not_recoverable"]) == (1, 1)
    assert _mirror_stats(vault)["not_recoverable_reasons"] == [media_replicas.QUARANTINED]
    assert shown[MIRROR] == (False, media_replicas.QUARANTINED)
    assert shown["test-store"] == (True, None)


def test_a_check_round_takes_every_pick_of_its_share_and_not_only_the_first(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A round that took one pick of 64 could never check a store whose share is larger within the cycle."""
    for category in ("a", "b", "c"):
        _offload(vault, _preserved(vault, category=category))
    _replicate_all(vault)
    media_replicas.rebuild(vault)
    _due_again(vault)
    monkeypatch.setattr(media_replicas, "_CHECK_BATCH", 1)
    monkeypatch.setattr(media_replicas, "_CHECK_FLOOR_ROWS", 3)  # the store's share is three replicas, a pick is one

    media_replicas.check(vault)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        assert ledger.execute("SELECT COUNT(*) FROM media_replicas WHERE store = ? AND checked_at > 0",
                              (MIRROR,)).fetchone() == (3,)


def test_the_round_at_the_default_settings_checks_two_stores_of_the_sizing_target_within_the_cycle(
        vault: Path, selected_store, mirrored: Path) -> None:
    """Two stores of 500,000 replicas and 25 TB share one read cap: too short a round leaves the second behind its cycle."""
    ledger = media_jobs.MediaJobStore(vault)
    stores = ["test-store", MIRROR]
    with closing(ledger._connect()) as conn, conn:
        for store in stores:
            conn.execute("INSERT OR REPLACE INTO media_replica_store_totals(store, replicas, bytes) VALUES (?, ?, ?)",
                         (store, 500_000, 25 * 10**12))
    cycle, rate = media_replicas.check_cycle(), media_replicas.check_read_rate()

    rows, size = media_replicas._store_budget(ledger, MIRROR, cycle)
    rounds = cycle / media_replicas._CHECK_INTERVAL

    # The byte share is a whole number of bytes, so it is short of the exact share by under a millionth.
    assert (rows * rounds >= 500_000, size * rounds >= 25 * 10**12 * 0.999999) == (True, True)
    assert media_replicas._round_seconds(ledger, stores, cycle, rate) * rate >= 2 * size  # both shares, under the one cap


def test_a_round_checks_its_whole_share_when_each_read_takes_the_time_a_real_store_takes(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wait added after each read holds the average under the cap, so the round ends early and the cycle runs long."""
    ledger = media_jobs.MediaJobStore(vault)
    size = 50 * 10**6  # the sizing target's average object: 25 TB over 500,000 replicas
    with closing(ledger._connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO media_replica_store_totals(store, replicas, bytes) VALUES (?, ?, ?)",
                     (MIRROR, 500_000, 25 * 10**12))
        for number in range(150):
            conn.execute("INSERT INTO media_replicas(binary_rel, store, object, version, key_version, size, state, seen) "
                         "VALUES (?, ?, ?, '1', '1', ?, ?, (SELECT value FROM media_replica_generation WHERE id = 1))",
                         (f"Originals/file-{number}.bin", MIRROR, f"object-{number}", size, media_replicas.UPLOAD_VERIFIED))
    clock = [0.0]
    monkeypatch.setattr(media_replicas, "time", types.SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(media_replicas.foreground_priority, "wait_for_retry",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def read_by_the_store(_vault, replica):
        clock[0] += replica.ciphertext_size / (50 * 10**6)  # a NAS that serves 50 MB a second: about 1 s a replica
        return None

    monkeypatch.setattr(media_replicas, "_bound", lambda _vault, row: (None, originals.StoredObject(
        1, row["store"], "1", row["object"], row["version"], size, "0" * 64)))
    monkeypatch.setattr(media_store, "check_replica", read_by_the_store)
    cycle, rate = media_replicas.check_cycle(), media_replicas.check_read_rate()
    limit = clock[0] + media_replicas._round_seconds(ledger, [MIRROR], cycle, rate)
    share, _bytes = media_replicas._store_budget(ledger, MIRROR, cycle)

    checked = media_replicas._check_store(vault, ledger, MIRROR, media_replicas.CONTENT, cycle,
                                          media_replicas._ReadCap(rate, limit), limit)

    assert checked == share == 116
    assert clock[0] < limit  # the whole share, at the cap, fits the round with the margin that sizes it


def test_a_read_too_large_for_one_round_is_paid_for_before_the_next_round_reads_again(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A round that ends before it can wait out a huge read must hand the wait on: a fresh cap each round forgave it, and
    two such reads ten minutes apart ran at several times the configured rate."""
    gib = 1024**3
    with closing(media_jobs.MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO media_replica_store_totals(store, replicas, bytes) VALUES (?, ?, ?)",
                     (MIRROR, 500_000, 25 * 10**12))
        for number in range(2):
            conn.execute("INSERT INTO media_replicas(binary_rel, store, object, version, key_version, size, state, seen) "
                         "VALUES (?, ?, ?, '1', '1', ?, ?, (SELECT value FROM media_replica_generation WHERE id = 1))",
                         (f"Originals/file-{number}.bin", MIRROR, f"object-{number}", 100 * gib,
                          media_replicas.UPLOAD_VERIFIED))
    clock, reads = [0.0], []
    monkeypatch.setattr(media_replicas, "time", types.SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(media_replicas.foreground_priority, "wait_for_retry",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(media_replicas, "_bound", lambda _vault, row: (None, originals.StoredObject(
        1, row["store"], "1", row["object"], row["version"], 100 * gib, "0" * 64)))
    monkeypatch.setattr(media_store, "check_replica", lambda _vault, replica: reads.append(
        (clock[0], replica.ciphertext_size)))

    for _round in range(8):  # the worker's check interval is ten minutes
        media_replicas.check(vault)
        clock[0] += 600.0

    assert len(reads) == 2
    rate = media_replicas.check_read_rate()
    for number, (started, _size) in enumerate(reads):
        assert sum(size for _started, size in reads[:number]) <= rate * started  # what was read before is paid for


def test_reads_that_fail_after_moving_their_bytes_still_use_up_the_read_cap(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A store that moves a huge object and then drops the connection costs the same bandwidth as one that finishes: a
    cap that only the verified reads advanced let it run three such reads back to back, again every round."""
    gib = 1024**3
    ledger = media_jobs.MediaJobStore(vault)
    with closing(ledger._connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO media_replica_store_totals(store, replicas, bytes) VALUES (?, ?, ?)",
                     (MIRROR, 500_000, 10**15))
        for number in range(3):
            conn.execute("INSERT INTO media_replicas(binary_rel, store, object, version, key_version, size, state, seen) "
                         "VALUES (?, ?, ?, '1', '1', ?, ?, (SELECT value FROM media_replica_generation WHERE id = 1))",
                         (f"Originals/file-{number}.bin", MIRROR, f"object-{number}", 10 * gib,
                          media_replicas.UPLOAD_VERIFIED))
    clock = [0.0]
    monkeypatch.setattr(media_replicas, "time", types.SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(media_replicas.foreground_priority, "wait_for_retry",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(media_replicas, "_bound", lambda _vault, row: (None, originals.StoredObject(
        1, row["store"], "1", row["object"], row["version"], 10 * gib, "0" * 64)))

    def drops_the_connection(_vault, _replica):
        raise media_store.MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the connection dropped")

    monkeypatch.setattr(media_store, "check_replica", drops_the_connection)
    cycle, rate = media_replicas.check_cycle(), media_replicas.check_read_rate()
    cap = media_replicas._ReadCap(rate, 10**9)

    attempted = media_replicas._check_store(vault, ledger, MIRROR, media_replicas.CONTENT, cycle, cap, 10**9)

    assert attempted == 3
    assert cap.due == pytest.approx(3 * 10 * gib / rate)


def test_samples_that_fail_after_moving_their_bytes_still_use_up_the_read_cap(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The third failure in a row ends the round: it moved its bytes as much as the two before it, and must be paid for."""
    for category in ("a", "b", "c"):
        _offload(vault, _preserved(vault, category=category))
    _replicate_all(vault)
    _unproven(vault)
    clock, read = [time.monotonic()], []  # the replication above already ran a round on the real clock
    began = clock[0]
    monkeypatch.setattr(media_replicas, "time", types.SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(media_replicas.foreground_priority, "wait_for_retry",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def drops_the_connection(_vault, replica, _expected):
        read.append(replica.ciphertext_size)
        raise media_store.MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the connection dropped")

    monkeypatch.setattr(media_store, "sample_replica", drops_the_connection)

    media_replicas.sample(vault)

    assert len(read) == 6  # three failures in a row end the turn of each of the two stores
    assert media_replicas._owed[(vault.resolve(), "sample")] - began == pytest.approx(
        sum(read) / media_replicas.check_read_rate())


def test_a_store_that_the_last_round_left_waiting_is_checked_first_in_the_next(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a fixed order, a round that the shared cap ends early would leave the same store last, and short, every time."""
    _replicated(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger, ledger:
        ledger.execute("UPDATE media_replicas SET check_from = 200 WHERE store = 'test-store'")
        ledger.execute("UPDATE media_replicas SET check_from = 100 WHERE store = ?", (MIRROR,))
    visited, check_store = [], media_replicas._check_store

    def visiting(vault_root, ledger, name, *rest):
        visited.append(name)
        return check_store(vault_root, ledger, name, *rest)

    monkeypatch.setattr(media_replicas, "_check_store", visiting)

    media_replicas.check(vault)

    assert visited == [MIRROR, "test-store"]  # the declared order lists test-store first


def test_replacing_a_damaged_pointer_releases_its_old_name_for_collection(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The damaged version's name left in the store for good would cost its bytes for as long as the vault lives."""
    values, _config = selected_store
    relative, _mirror = _replicated(vault)
    pointer = originals.original_state(vault, relative).reference
    assert media_store.evict(vault, vault / relative)
    _damage_a_byte(Path(values["root"]) / pointer.object / pointer.version)
    _due_again(vault)
    media_replicas.check(vault)

    _recopy_all(vault)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        assert ledger.execute("SELECT object FROM media_releases").fetchall() == [(pointer.object,)]


def test_a_copy_again_does_not_read_a_replica_in_a_store_that_is_not_immediate(
        vault: Path, selected_store, tmp_path: Path) -> None:
    """A retrieval of minutes or hours is the owner's to ask for: a background repair must never start one."""
    values, _config = selected_store
    _declare(vault, selected_store, tmp_path / "mirror", retrieval_class="hours")
    relative, _replica = _replicated(vault)
    pointer = originals.original_state(vault, relative).reference
    assert media_store.evict(vault, vault / relative)
    _damage_a_byte(Path(values["root"]) / pointer.object / pointer.version)
    _due_again(vault)
    media_replicas.check(vault)

    _recopy_all(vault)

    assert originals.original_state(vault, relative).reference == pointer
    assert [item[2:] for item in _queued(vault) if item[0] == pointer.store] == [
        (media_replicas.REFUSED, media_store.NO_VERIFIED_SOURCE)]


def test_a_cache_row_that_failed_to_follow_a_replaced_pointer_is_recorded_by_the_retry_so_the_original_can_be_evicted(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The binding changed but the cache row stayed on the old pointer: eviction refuses a cache row that names another pointer."""
    values, _config = selected_store
    relative, _mirror = _replicated(vault)
    pointer = originals.original_state(vault, relative).reference
    _damage_a_byte(Path(values["root"]) / pointer.object / pointer.version)
    _due_again(vault)
    media_replicas.check(vault)
    repoint = media_store._repoint_cache
    failures = []

    def fail_once(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise sqlite3.OperationalError("database is locked")
        return repoint(*args, **kwargs)

    monkeypatch.setattr(media_store, "_repoint_cache", fail_once)
    monkeypatch.setattr(media_replicas, "_RETRY_SECONDS", 0)
    for _ in range(3):  # the first attempt fails after publishing, the retry finds the finding stale
        media_replicas.drain(vault)
    assert originals.original_state(vault, relative).reference != pointer

    assert media_store.evict(vault, vault / relative)


def test_a_store_that_goes_away_during_a_round_ends_its_round_and_leaves_the_rows_it_never_read(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mount that drops mid-round is the store's outage: its next rows are no finding and wait no extra day."""
    for category in ("a", "b", "c"):
        _offload(vault, _preserved(vault, category=category))
    _replicate_all(vault)
    media_replicas.rebuild(vault)
    _due_again(vault)
    check_replica, reads = media_store.check_replica, []

    def unmounted_after_the_first_read(vault_root, replica):
        if replica.store == MIRROR:
            reads.append(replica.object)
            if len(reads) == 2:
                (mirrored / media_store.STORE_MARKER).unlink()
        return check_replica(vault_root, replica)

    monkeypatch.setattr(media_store, "check_replica", unmounted_after_the_first_read)

    media_replicas.check(vault)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        rows = ledger.execute("SELECT checked_at > 0, failure, check_from FROM media_replicas WHERE store = ? "
                              "ORDER BY checked_at DESC", (MIRROR,)).fetchall()
    assert rows == [(1, None, rows[0][2]), (0, None, 0), (0, None, 0)]


def test_a_failed_attempt_does_not_land_on_a_version_that_a_copy_again_published_during_it(
        vault: Path, selected_store, mirrored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A finding about the replaced version, written onto its successor, would hide the new copy for a day for nothing."""
    relative, replica = _replicated(vault)
    _due_again(vault)
    newer = dataclasses.replace(replica, version="a-newer-version")

    def republished_then_unavailable(vault_root, checked):
        if checked.store == MIRROR:
            media_replicas.record(vault, relative, newer, verified_at=None)
        raise media_store.MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the store did not answer")

    monkeypatch.setattr(media_store, "check_replica", republished_then_unavailable)
    monkeypatch.setattr(media_store, "store_reachable", lambda *args: None)  # a store with no cheap check: a read failure

    media_replicas.check(vault)

    row = _replica_row(vault)
    assert (row["version"], row["failure"], row["check_from"]) == ("a-newer-version", None, 0)


def test_a_sample_that_the_store_refuses_before_reading_anything_is_not_billed_to_the_month(
        vault: Path, selected_store, mirrored: Path) -> None:
    """The egress bill counts bytes read: a version the store cannot address would otherwise spend the budget on nothing."""
    relative, _replica = _replicated(vault)
    _hand_edit_mirror_version(vault, relative, "a/b")
    media_replicas.rebuild(vault)
    _unproven(vault)

    media_replicas.sample(vault)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        billed = ledger.execute("SELECT COALESCE(SUM(bytes), 0) FROM media_replica_samples WHERE store = ?",
                                (MIRROR,)).fetchone()
    assert (billed, _replica_row(vault)["failure"]) == ((0,), media_replicas.RECORD_INVALID)


def _large_replicated(vault: Path, mirrored: Path) -> tuple[str, originals.StoredObject, Path]:
    """An evicted original of 300 KiB, more than a pipe holds, with its replica: age can stop before it reads it all."""
    original = _stored(vault, "Recordings", (TRANSCRIPT * 5000)[:300 * 1024])
    _offload(vault, original)
    _replicate_all(vault)
    snapshot = originals.original_state(vault, original)
    replica = next(entry for entry in snapshot.replicas if entry.store == MIRROR)
    return original.relative_to(vault).as_posix(), replica, _stored_file(mirrored, replica)


def test_a_sample_under_a_wrong_key_still_finds_a_large_replica_that_is_damaged(
        vault: Path, selected_store, mirrored: Path, tmp_path: Path) -> None:
    """age stops at the header of a wrong key; a sample that stopped reading there would never see the damaged bytes behind it."""
    values, _config = selected_store
    _relative, _replica, stored = _large_replicated(vault, mirrored)
    _damage_a_byte(stored)
    _unproven(vault)
    Path(values["identity_path"]).write_bytes(_another_identity(tmp_path))

    media_replicas.sample(vault)

    row = _replica_row(vault)
    assert (row["state"], row["failure"]) == (media_replicas.QUARANTINED, "digest_mismatch")


def test_a_repair_quarantines_a_large_damaged_replica_that_the_key_refuses_and_goes_on_to_the_next_copy(
        vault: Path, selected_store, mirrored: Path, tmp_path: Path) -> None:
    """A repair that stopped reading at a refusal would call the bad copy unreadable and try it again at every damage."""
    values, _config = selected_store
    relative, _replica, stored = _large_replicated(vault, mirrored)
    original = vault / relative
    original_fixity.walk(vault)
    original_fixity.run_pass(vault)
    _damage_a_byte(stored)
    Path(values["identity_path"]).write_bytes(_another_identity(tmp_path))
    original.write_bytes(DAMAGED)

    original_fixity.run_pass(vault, budget=0)

    row = _replica_row(vault)
    assert (row["state"], row["failure"]) == (media_replicas.QUARANTINED, "digest_mismatch")


@pytest.mark.parametrize(("prove", "pulled_all"), [
    pytest.param(False, False, id="hydration-and-export-stop-when-age-stops"),
    pytest.param(True, True, id="a-sample-and-a-repair-read-the-rest-to-judge-it"),
])
def test_a_read_downloads_the_rest_of_the_ciphertext_after_age_stops_only_when_it_proves(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prove: bool, pulled_all: bool) -> None:
    """A hydration that the key refuses cannot quarantine anything: the whole download after the refusal is only cost."""
    root = tmp_path / "store"
    root.mkdir()
    media_store.write_store_marker(root, "solo")
    backend = media_store.FilesystemStore(root, name="solo")
    data = os.urandom(5 * 2**20)  # five chunks of the store's read
    digest = hashlib.sha256(data).hexdigest()
    reference = originals.StoredObject(1, "solo", "1", digest, backend.upload(digest, io.BytesIO(data), size=len(data),
                                                                             sha256=digest), len(data), digest)
    identity = tmp_path / "identity"
    subprocess.run(["age-keygen", "-o", str(identity)], capture_output=True, check=True)
    pulled, stored_chunks = [0], media_store._stored_chunks

    def counting(backend_, reference_):
        for chunk in stored_chunks(backend_, reference_):
            pulled[0] += len(chunk)
            yield chunk

    monkeypatch.setattr(media_store, "_stored_chunks", counting)

    with pytest.raises(media_store.MediaStoreError):
        list(media_store._decrypted_chunks(backend, reference, str(identity), prove=prove))

    assert (pulled[0] == len(data)) == pulled_all
