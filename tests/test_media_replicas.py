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
from contextlib import closing
from pathlib import Path

import pytest
from test_media_store import _offload, _preserved
from test_media_store import selected_store as selected_store
from test_original_fixity import DAMAGED, TRANSCRIPT, _count_downloads, _findings, _fixity_on, _stored  # noqa: F401

from exomem import (
    artifact_lifecycle,
    commands,
    held_fs,
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
    _unproven(vault)

    assert media_replicas.sample(vault) == 2  # the eviction store's pointer and the mirror's replica

    assert [proven for _path, proven in _sampled(vault)] == [True]
    assert _replica_row(vault)["state"] == media_replicas.UPLOAD_VERIFIED


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


def test_the_eviction_gate_reads_the_index_of_quarantined_rows(vault: Path, selected_store, mirrored: Path) -> None:
    """Eviction asks this for every candidate: a scan of every replica would cost the size of the store each time."""
    _replicated(vault)
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        plan = " ".join(row[3] for row in ledger.execute("EXPLAIN QUERY PLAN " + media_replicas._DAMAGED, ("o", "v")))
    assert "USING COVERING INDEX media_replicas_damaged" in plan


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


def test_eviction_of_a_shared_original_keeps_its_object_and_quarantines_the_damaged_pointer(
        vault: Path, selected_store, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pool's eviction path confirms each filing's pointer too: a damaged one must be a finding there as well."""
    values, _config = selected_store
    path = _preserved(vault)
    reference = _offload(vault, path)
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
    _damage_a_byte(Path(values["root"]) / reference.object / reference.version)

    assert not media_store.evict(vault, path)

    row = _replica_row(vault, store=reference.store)
    assert (row["binary_rel"], row["state"], row["failure"]) == (path.relative_to(vault).as_posix(),
                                                                 media_replicas.QUARANTINED, "digest_mismatch")
    assert original_pool.object_path(vault, digest).exists()


# --- verify-stored-replicas: copying a quarantined replica again ---------------------------------------------


def _recopy_all(vault: Path) -> None:
    """Run the worker's copy units until the queue has nothing it can settle, as the service would."""
    while media_replicas.drain(vault):
        pass


def _queued(vault: Path) -> list[tuple[str, str, str, str | None]]:
    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        return list(ledger.execute("SELECT store, kind, state, code FROM media_replica_queue ORDER BY store"))


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


def test_a_replica_quarantined_for_a_plaintext_mismatch_leaves_quarantine_when_a_sample_decrypts_to_the_binding(
        vault: Path, selected_store, mirrored: Path) -> None:
    """A false alarm of the plaintext check must not queue a copy again for good: the next proof must be able to lift it."""
    relative, replica = _replicated(vault)
    media_replicas.quarantine(vault, relative, replica, level=media_replicas.DECRYPTED,
                              failure=media_replicas.PLAINTEXT_MISMATCH)
    _unproven(vault)

    media_replicas.sample(vault)

    row = _replica_row(vault)
    assert (row["state"], row["failure"], row["decrypted_at"] > 0) == (media_replicas.UPLOAD_VERIFIED, None, True)
    assert [item for item in _queued(vault) if item[0] == MIRROR] == []


def test_a_replica_quarantined_for_damaged_ciphertext_stays_quarantined_through_a_sample(
        vault: Path, selected_store, mirrored: Path) -> None:
    """Only a new copy replaces damaged bytes: a sample must not lift the finding that a check or a repair made."""
    relative, replica = _replicated(vault)
    media_replicas.quarantine(vault, relative, replica, level=media_replicas.CONTENT, failure="digest_mismatch")
    _unproven(vault)

    media_replicas.sample(vault)

    assert _replica_row(vault)["state"] == media_replicas.QUARANTINED
    assert [item[:2] for item in _queued(vault) if item[0] == MIRROR] == [(MIRROR, media_replicas.RECOPY)]


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

