"""Exact canonical filing evidence for opted-in storage protection."""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import closing

from test_managed_collections_migration import coordinator_url as coordinator_url
from test_vault_store_migration import migrate_apply
from test_vault_store_migration import vault as store_vault  # noqa: F401

from exomem import (
    artifact_lifecycle,
    media_bindings,
    media_jobs,
    media_protection,
    media_store,
    original_pool,
    originals,
    preserve,
)
from exomem.collection_store import connection
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope
from exomem.vault_store import write


def _configuration(vault, tmp_path, monkeypatch):
    path = tmp_path / "media.json"
    monkeypatch.setenv(originals.CONFIG_ENV, str(path))
    values = {"store": "primary", "enabled": True, "adapter": "filesystem", "root": str(tmp_path / "objects")}

    def update():
        path.write_text(json.dumps({"vaults": {str(vault): values}}))

    update()
    return values, update


def _policy() -> dict:
    return {"minimum_verified_copies": 1, "minimum_failure_domains": 1,
            "replica_stores": ["primary"], "copy_bytes_per_month": 1_000_000}


def _replica(version: str, store: str = "mirror") -> originals.StoredObject:
    return originals.StoredObject(1, store, "key-1", "a" * 64, version, 220, "b" * 64)


def test_nonpooled_opt_in_follows_publication_and_budget_edits_keep_the_frontier(vault, tmp_path, monkeypatch):
    values, update = _configuration(vault, tmp_path, monkeypatch)
    with library_scope():
        item = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="first.txt",
                                       stream=io.BytesIO(b"the original"))
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.policy_keys(vault, conn, limit=8) is None
        assert conn.execute("SELECT COUNT(*) FROM media_policy_filings").fetchone()[0] == 0

    values[media_protection.POLICY_FIELD] = _policy()
    update()
    assert not original_pool.enabled(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.policy_filings(vault, conn, (item.path,)) is None
    assert media_bindings.recover(vault)

    replica = _replica("mirror-version")
    with media_store._guard(vault, "test_binding_replica_publication"):
        publication = media_store.prepare_replica(vault, originals.original_state(vault, item.path), replica)
        media_store._commit_reference(vault, publication)

    values[media_protection.POLICY_FIELD]["copy_bytes_per_month"] = 10
    update()
    with closing(ledger._connect(readonly=True)) as conn:
        page = media_bindings.policy_filings(vault, conn, (item.path, "absent"))
        [filing] = page[item.path]
        assert page["absent"] == ()
        assert filing.references == (replica,)
        assert filing.companion_hash == hashlib.sha256((vault / item.sidecar_path).read_bytes()).hexdigest()
        assert media_bindings.current(vault, conn)
    media_bindings.external_change(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.policy_filings(vault, conn, (item.path,)) is None


def test_two_shared_filings_keep_their_own_exact_reference_lists(vault, tmp_path, monkeypatch):
    values, update = _configuration(vault, tmp_path, monkeypatch)
    values[media_protection.POLICY_FIELD] = _policy()
    update()
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.policy_keys(vault, conn, limit=8) is None
    data = b"the physical original with two canonical companions"
    with library_scope():
        one = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="one.txt",
                                      stream=io.BytesIO(data))
        two = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="two.txt",
                                      stream=io.BytesIO(data), raw_protection=True)
    primary, mirror = _replica("primary-version", "primary"), _replica("mirror-version")
    with media_store._guard(vault, "test_binding_reference_publication"):
        snapshot = originals.original_state(vault, one.path)
        media_store._commit_reference(vault, media_store.prepare_reference(vault, snapshot, primary))
        snapshot = originals.original_state(vault, one.path)
        media_store._commit_reference(vault, media_store.prepare_replica(vault, snapshot, mirror))

    assert media_bindings.rebuild(vault)
    key = original_pool.object_path(vault, hashlib.sha256(data).hexdigest()).relative_to(vault).as_posix()
    with closing(ledger._connect()) as conn:
        page = media_bindings.policy_filings(vault, conn, (key,))
        assert {item.binary_rel: item.references for item in page[key]} == {one.path: (primary, mirror), two.path: ()}
        # A stale projection row cannot transfer the first filing's witness to the second.
        conn.execute("UPDATE media_policy_filings SET companion_hash = '' WHERE binary_rel = ?", (two.path,))
        assert media_bindings.policy_filings(vault, conn, (key,)) == {key: None}


def test_unreadable_policy_withdraws_previous_proof_without_refusing_the_filing(vault, tmp_path, monkeypatch):
    values, update = _configuration(vault, tmp_path, monkeypatch)
    values[media_protection.POLICY_FIELD] = _policy()
    update()
    with library_scope():
        item = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="first.txt",
                                       stream=io.BytesIO(b"the original"))
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    (tmp_path / "media.json").write_text("{")
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.policy_filings(vault, conn, (item.path,)) is None
    with library_scope():
        preserve.update_sidecar_extraction(vault, vault / item.sidecar_path, text="New extraction.", engine="text")
    assert "New extraction." in (vault / item.sidecar_path).read_text()
    update()
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.policy_filings(vault, conn, (item.path,)) is None


def test_a_disabled_policy_cannot_restore_its_old_proof_after_a_companion_changes(vault, tmp_path, monkeypatch):
    values, update = _configuration(vault, tmp_path, monkeypatch)
    values[media_protection.POLICY_FIELD] = policy = _policy()
    update()
    with library_scope():
        item = preserve.preserve_stream(vault, scope="Archive", category="documents", filename="first.txt",
                                       stream=io.BytesIO(b"the original"))
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    values.pop(media_protection.POLICY_FIELD)
    update()
    with library_scope():
        preserve.update_sidecar_extraction(vault, vault / item.sidecar_path, text="Changed while off.", engine="text")
    values[media_protection.POLICY_FIELD] = policy
    update()
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.policy_filings(vault, conn, (item.path,)) is None
    assert media_bindings.recover(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        [filing] = media_bindings.policy_filings(vault, conn, (item.path,))[item.path]
        assert filing.companion_hash == hashlib.sha256((vault / item.sidecar_path).read_bytes()).hexdigest()


def test_nonpooled_store_audit_replays_unprojected_policy_facts(store_vault, tmp_path, monkeypatch):  # noqa: F811
    assert migrate_apply(store_vault) == 0
    values, update = _configuration(store_vault, tmp_path, monkeypatch)
    values[media_protection.POLICY_FIELD] = _policy()
    update()
    ledger = media_jobs.MediaJobStore(store_vault)
    assert media_bindings.rebuild(store_vault)
    binary = "Knowledge Base/Evidence/Archive/protected.bin"
    companion = binary + ".md"
    replica = _replica("unprojected-version")
    body = (f"---\nevidence_file: {binary}\nbinary_sha256: {'c' * 64}\nbinary_size: 14\n"
            f"replicas: {json.dumps([replica.fields()])}\n---\n").encode()
    with connection.open_writer(connection.store_path(store_vault), lease_check=lambda: True) as handle:
        writer = CollectionWriter(store_vault, handle)
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "test", puts=(write.PagePut(companion, body),)))
    assert not (store_vault / companion).exists()
    with closing(ledger._connect(readonly=True)) as conn:
        assert media_bindings.policy_filings(store_vault, conn, (binary,)) is None
    media_bindings.repair_paths(store_vault, (companion,))
    with closing(ledger._connect(readonly=True)) as conn:
        [filing] = media_bindings.policy_filings(store_vault, conn, (binary,))[binary]
        assert filing.references == (replica,)
        assert filing.companion_hash == hashlib.sha256(body).hexdigest()


def test_a_policy_page_stops_at_its_alias_bound_and_seeks_past_duplicate_keys(vault, tmp_path, monkeypatch, record_property):
    values, update = _configuration(vault, tmp_path, monkeypatch)
    values[media_protection.POLICY_FIELD] = _policy()
    update()
    ledger = media_jobs.MediaJobStore(vault)
    assert media_bindings.rebuild(vault)
    with closing(ledger._connect()) as conn:
        def add(key, start, end):
            conn.executemany("INSERT INTO media_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                (f"alias/{key}/{index}", f"companion/{key}/{index}", "a" * 64, key, "b" * 64, 10, 0, None)
                for index in range(start, end)
            ))
            conn.execute("INSERT OR IGNORE INTO media_policy_filings "
                         "SELECT binary_rel, companion_hash, '[]' FROM media_bindings WHERE object_key = ?", (key,))

        first, shared, last = "physical/a", "physical/b", "physical/c"
        add(first, 0, 1)
        add(shared, 0, 5)
        add(last, 0, 1)

        def run():
            steps = 0

            def tick():
                nonlocal steps
                steps += 1

            conn.set_progress_handler(tick, 1)
            try:
                keys = media_bindings.policy_keys(vault, conn, after="physical/", limit=3)
                assert keys == (first, shared, last)
                page = media_bindings.policy_filings(vault, conn, keys, limit=4)
                assert len(page[first]) == 1
                assert page[shared] is None and page[last] is None
                assert len(media_bindings.policy_filings(vault, conn, (last,))[last]) == 1
                return steps
            finally:
                conn.set_progress_handler(None, 0)

        baseline = run()
        add(shared, 5, 50_000)
        scaled = run()
        record_property("vm_steps_at_7_filings", baseline)
        record_property("vm_steps_at_50002_filings", scaled)
        assert scaled <= baseline + 500
