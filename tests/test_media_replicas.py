"""Replicas of an original in a second store: how they are made, found, protected and used to repair.

Each case runs the real offload, replicate and fixity code on a temporary vault with two filesystem stores
and the real `age`. The data is invented.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_media_store import _offload, _preserved
from test_media_store import selected_store as selected_store
from test_original_fixity import DAMAGED, TRANSCRIPT, _findings, _fixity_on, _stored  # noqa: F401

from exomem import commands, media_jobs, media_replicas, media_store, original_fixity, originals

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
    assert media_replicas.status(vault)["totals"]["replicas"] == 4


def test_the_status_reads_one_index_and_the_page_returns_originals_by_path(vault: Path, selected_store, mirrored: Path) -> None:
    for category in ("a", "b", "c"):
        _offload(vault, _preserved(vault, category=category))
    _replicate_all(vault)
    _measured(vault)

    with closing(sqlite3.connect(media_jobs.MediaJobStore(vault).path)) as ledger:
        plan = " ".join(row[3] for row in ledger.execute(
            "EXPLAIN QUERY PLAN SELECT store, key_version, state, COUNT(*), COUNT(verified_at), MIN(verified_at) "
            "FROM media_replicas GROUP BY store, key_version, state"))
    assert "COVERING INDEX media_replicas_status" in plan
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
    assert "media_replica_queue_pick" in plan and "TEMP B-TREE" not in plan


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
