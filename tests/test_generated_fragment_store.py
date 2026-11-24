"""Canonical Source-file custody survives interrupted publication and cache replacement."""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager

import pytest

from exomem import generated_fragment_store as generated
from exomem import memory_refs, writer_lease
from exomem import vault as vault_module
from exomem.governance.principal import owner_principal, request_scope
from exomem.source_custody import OwnerLocator, ProducerBirth


def reference():
    return memory_refs.memory_ref(str(uuid.uuid4()))


@pytest.fixture
def store(vault):
    with request_scope(owner_principal(surface="mcp")), writer_lease.active_manager().mutation_guard(vault):
        value = generated.GeneratedFragmentStore(vault)
        value.initialize()
        yield value
        value.close()


def ready(store, ref, *, horizon=0):
    data = b"Exact retained bytes"
    return store.begin_opaque_image(source_refs=(ref,), image_sha256=hashlib.sha256(data).hexdigest(),
                                    image_size=len(data), unknown_through_epoch=horizon).image_id


def test_copy_reservation_survives_partial_metadata_install_and_replica_census(store, monkeypatch, tmp_path):
    birth = store.allocate_birth()
    ref = reference()
    image_id = ready(store, ref)
    owner = OwnerLocator(owner="retained-history", locator=uuid.uuid4().hex, version=uuid.uuid4().hex)
    origin = writer_lease.active_manager().idempotency.capture_attempts.ledger_id
    assert len(origin) == 64
    original = vault_module.batch_atomic_write
    seen = []

    def crash_after_install(writes, **kwargs):
        writes = tuple(writes)
        result = original(writes, **kwargs)
        seen.extend(str(write.path) for write in writes)
        if any("/copies/" in str(write.path) for write in writes):
            raise RuntimeError("process lost after copy reservation install")
        return result

    with monkeypatch.context() as crashing:
        crashing.setattr(vault_module, "batch_atomic_write", crash_after_install)
        with pytest.raises(RuntimeError, match="process lost"):
            store.reserve_copy(image_id, owner=owner, origin_ledger=origin,
                               expected_header=store.image_header(image_id).value, expected_root=store.root)
    assert store.head().pending is not None
    assert any("/copies/" in path for path in seen)
    store.close()
    # A different local state root models canonical replica transfer without the old lookup.
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "replica-state"))
    replica = generated.GeneratedFragmentStore(store.root)
    assert replica.reconcile().state == "pending"
    for _ in range(30):
        if replica.census(limit=2).state == "ready":
            break
    found = replica.lookup(ref, birth)
    assert found.state == "pending"
    assert len(found.copies) == 1 and found.copies[0].owner == owner
    assert found.copies[0].outcome == "reserved"
    assert found.copies[0].origin_ledger == origin
    assert replica.head().pending is None
    replica.close()


def test_legacy_horizon_allows_later_birth_but_observed_loss_is_unknown(store):
    early = store.allocate_birth()
    unrelated = reference()
    legacy = ready(store, unrelated, horizon=early.birth_epoch)
    later = store.allocate_birth()
    target = reference()
    exact = ready(store, target)
    assert store.lookup(target, later).state == "ready"
    assert store.lookup(unrelated, early).state == "unknown"
    # An observed loss raises the image horizon past the later birth.
    store.mark_uncertain(exact)
    found = store.lookup(target, later)
    assert found.state == "unknown"
    assert exact in found.uncertain_images
    assert legacy not in found.uncertain_images
    with store._connect() as conn:
        assert conn.execute("SELECT 1 FROM members WHERE source_ref=? AND image_id=?", (target, exact)).fetchone()


@pytest.mark.parametrize("loss_scope", ("unscoped", "missing-image"))
def test_direct_uncertainty_summary_survives_cache_loss_and_has_a_finite_global_floor(store, monkeypatch, tmp_path, loss_scope):
    image_id = ready(store, reference())
    scoped = store.mark_uncertain(image_id)
    assert store.image_header(image_id).value.unknown_through_epoch == scoped.unknown_through_epoch
    missing_image = uuid.uuid4().hex if loss_scope == "missing-image" else None
    global_loss = store.mark_uncertain(missing_image)
    assert store.head().unknown_through_epoch == global_loss.unknown_through_epoch
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "lost-summary-cache"))
    replica = generated.GeneratedFragmentStore(store.root)
    monkeypatch.setattr(replica, "_census_keys", lambda: pytest.fail("direct reads scanned history"))
    with monkeypatch.context() as no_cache:
        no_cache.setattr(replica, "_connect", lambda: pytest.fail("direct reads queried derived cache"))
        assert replica.image_header(image_id).unknown_through_epoch == global_loss.unknown_through_epoch
    replica.allocate_birth()
    assert replica.image_header(image_id).unknown_through_epoch == global_loss.unknown_through_epoch
    replica.close()


def test_legacy_head_without_summary_uses_prior_epoch_once_without_history_scan(store, monkeypatch, tmp_path):
    image_id = ready(store, reference())
    prior_epoch = store.head().epoch
    path = store.path / "head.json"
    legacy = json.loads(path.read_text())
    legacy.pop("unknown_through_epoch", None)
    legacy.pop("summarized_through_epoch", None)
    path.write_text(json.dumps(legacy))
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "legacy-summary-cache"))
    replica = generated.GeneratedFragmentStore(store.root)
    monkeypatch.setattr(replica, "_census_keys", lambda: pytest.fail("legacy read scanned history"))
    assert replica.image_header(image_id).unknown_through_epoch == prior_epoch
    later = replica.allocate_birth()
    assert later.birth_epoch > prior_epoch
    assert replica.head().unknown_through_epoch == prior_epoch
    assert replica.head().summarized_through_epoch == later.birth_epoch
    replica.allocate_birth()
    assert replica.image_header(image_id).unknown_through_epoch == prior_epoch
    replica.close()


@pytest.mark.parametrize("loss_scope", ("image", "global"))
def test_interrupted_uncertainty_fold_replays_before_pending_head_clears(store, monkeypatch, loss_scope):
    image_id = ready(store, reference())

    def lose_before_fold(head, change):
        raise RuntimeError("lost before horizon fold")

    with monkeypatch.context() as crashing:
        crashing.setattr(store, "_summarize", lose_before_fold)
        with pytest.raises(RuntimeError, match="before horizon fold"):
            store.mark_uncertain(image_id if loss_scope == "image" else None)
    epoch = store.head().epoch
    replica = generated.GeneratedFragmentStore(store.root)
    set_head = replica._set_head

    def lose_before_pending_clear(expected, value):
        if expected.pending is not None and value.pending is None:
            if loss_scope == "image":
                assert replica.image_header(image_id).value.unknown_through_epoch == expected.epoch
            else:
                assert value.unknown_through_epoch == expected.epoch
            raise RuntimeError("lost after horizon fold")
        return set_head(expected, value)

    with monkeypatch.context() as crashing:
        crashing.setattr(replica, "_set_head", lose_before_pending_clear)
        with pytest.raises(RuntimeError, match="horizon fold"):
            replica.reconcile()
    assert replica.head().pending is not None
    replica.reconcile()
    assert replica.head().pending is None and replica.head().summarized_through_epoch == epoch
    assert replica.image_header(image_id).unknown_through_epoch == epoch
    replica.allocate_birth()
    assert replica.image_header(image_id).unknown_through_epoch == epoch
    replica.close()


def test_pending_uncertainty_without_installed_marker_recovers_its_horizon_once(store, monkeypatch):
    image_id = ready(store, reference())
    install = store._install

    def lose_before_uncertainty_install(writes):
        if any("/uncertain/" in str(write.path) for write in writes):
            raise RuntimeError("lost before uncertainty installation")
        return install(writes)

    with monkeypatch.context() as crashing:
        crashing.setattr(store, "_install", lose_before_uncertainty_install)
        with pytest.raises(RuntimeError, match="before uncertainty installation"):
            store.mark_uncertain(image_id)
    epoch = store.head().epoch
    assert store.head().pending is not None
    unit_id = store._change(epoch).objects[0].object_id
    replica = generated.GeneratedFragmentStore(store.root)
    replica.reconcile()
    assert replica.head().pending is None
    assert replica.image_header(image_id).value.unknown_through_epoch == epoch
    replica.mark_uncertain(image_id, unit_id=unit_id)
    assert replica.head().epoch == epoch
    replica.allocate_birth()
    assert replica.image_header(image_id).unknown_through_epoch == epoch
    replica.close()


def test_orphan_descriptor_consumes_epoch_without_replaying_image(store, monkeypatch):
    before = store.head().epoch
    original = vault_module.batch_atomic_write

    def fail_before_head(writes, **kwargs):
        writes = tuple(writes)
        if any(str(write.path).endswith("/head.json") for write in writes):
            raise RuntimeError("process lost before head install")
        return original(writes, **kwargs)

    with monkeypatch.context() as crashing:
        crashing.setattr(vault_module, "batch_atomic_write", fail_before_head)
        with pytest.raises(RuntimeError, match="before head"):
            ready(store, reference())
    assert store.head().epoch == before
    store.reconcile()
    assert store.head().epoch == before + 1
    descriptor = json.loads((generated.canonical_root(store.root) / f"changes/{before + 1}.json").read_bytes())
    assert descriptor["operation"] == "no_effect" and descriptor["objects"] == []


def test_corrupt_pending_descriptor_settles_unknown_and_does_not_freeze_later_births(store, monkeypatch):
    early = store.allocate_birth()
    ref = reference()
    original = vault_module.batch_atomic_write

    def lose_after_pending(writes, **kwargs):
        writes = tuple(writes)
        result = original(writes, **kwargs)
        if any(str(write.path).endswith("/head.json") and '"pending":{' in write.content for write in writes):
            raise RuntimeError("process lost after pending head")
        return result

    with monkeypatch.context() as crashing:
        crashing.setattr(vault_module, "batch_atomic_write", lose_after_pending)
        with pytest.raises(RuntimeError, match="pending head"):
            ready(store, ref)
    pending = store.head()
    descriptor = generated.canonical_root(store.root) / f"changes/{pending.epoch}.json"
    descriptor.write_text("broken descriptor")
    store.reconcile()
    assert store.head().pending is None
    later = store.allocate_birth()
    for _ in range(30):
        if store.census(limit=2).state == "ready":
            break
    assert store.lookup(ref, early).state == "unknown"
    assert store.lookup(reference(), later).state == "ready"


def test_closed_copy_collection_retires_membership_and_journal_has_no_source(store):
    birth = store.allocate_birth()
    ref = reference()
    image_id = ready(store, ref)
    owner = OwnerLocator(owner="native-stage", locator=uuid.uuid4().hex, version=uuid.uuid4().hex)
    copy = store.reserve_copy(image_id, owner=owner, origin_ledger=uuid.uuid4().hex,
                              expected_header=store.image_header(image_id).value, expected_root=store.root)
    with pytest.raises(ValueError, match="outstanding"):
        store.collect(image_id)
    store.ack_copy(copy.copy_id, owner=owner, outcome="retired")
    for _ in range(6):
        if store.collect(image_id).state == "missing":
            break
    assert store.lookup(ref, birth).copies == ()
    assert store.image_header(image_id).state == "missing"
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM members WHERE source_ref=?", (ref,)).fetchone()[0] == 0
    for path in (generated.canonical_root(store.root) / "changes").glob("*.json"):
        assert ref.encode() not in path.read_bytes()
    for _ in range(10):
        if store.census(limit=2).state == "ready":
            break
    assert store.lookup(ref, birth).state == "ready"


def test_published_copy_cannot_close_with_a_no_effect_witness(store):
    image_id = ready(store, reference())
    owner = OwnerLocator(owner="retained-history", locator=uuid.uuid4().hex, version=uuid.uuid4().hex)
    copy = store.reserve_copy(image_id, owner=owner, origin_ledger="native-ledger",
                              expected_header=store.image_header(image_id).value, expected_root=store.root)
    store.ack_copy(copy.copy_id, owner=owner, outcome="present")
    with pytest.raises(ValueError, match="published"):
        store.ack_copy(copy.copy_id, owner=owner, outcome="no_effect")
    with pytest.raises(ValueError, match="outstanding"):
        store.collect(image_id)


def test_foreign_sqlite_sidecar_alias_is_rejected_before_sqlite_open(store, tmp_path):
    store.close()
    target = tmp_path / "foreign.sqlite"
    target.write_bytes(b"foreign database")
    wal = generated.cache_path(store.root).with_name(generated.cache_path(store.root).name + "-wal")
    wal.unlink(missing_ok=True)
    wal.symlink_to(target)
    with pytest.raises(RuntimeError, match="unsafe|ambiguous"):
        with store._connect():
            pytest.fail("foreign SQLite sidecar reached the connection")
    assert target.read_bytes() == b"foreign database"


def test_warm_lookup_query_work_does_not_grow_with_20000_unrelated_members(store, monkeypatch, request):
    birth = store.allocate_birth()
    ref = reference()
    ready(store, ref)
    connect = store._connect
    work = []

    @contextmanager
    def measured():
        with connect() as conn:
            steps = []
            conn.set_progress_handler(lambda: steps.append(None) or 0, 1)
            try:
                yield conn
            finally:
                conn.set_progress_handler(None, 0)
                work.append(len(steps))

    with monkeypatch.context() as measured_scope:
        measured_scope.setattr(store, "_connect", measured)
        assert store.lookup(ref, birth).state == "ready"
    baseline = sum(work)
    with connect() as conn:
        conn.executemany("INSERT INTO members(source_ref,image_id) VALUES (?,?)", (
            (reference(), uuid.uuid4().hex) for _ in range(20_000)
        ))
        conn.commit()
    work.clear()
    with monkeypatch.context() as measured_scope:
        measured_scope.setattr(store, "_connect", measured)
        assert store.lookup(ref, birth).state == "ready"
    expanded = sum(work)
    assert expanded <= baseline + 100
    request.node.user_properties.extend((("unrelated_members", 20_000),
                                       ("baseline_sqlite_vm_steps", baseline),
                                       ("expanded_sqlite_vm_steps", expanded)))


def test_lookup_pages_across_images_and_copies_without_repeating_obligations(store):
    birth = store.allocate_birth()
    ref = reference()
    expected = set()
    for _ in range(2):
        image_id = ready(store, ref)
        for _ in range(3):
            owner = OwnerLocator(owner="retained-history", locator=uuid.uuid4().hex, version=uuid.uuid4().hex)
            copy = store.reserve_copy(image_id, owner=owner, origin_ledger="native-ledger",
                                      expected_header=store.image_header(image_id).value, expected_root=store.root)
            store.ack_copy(copy.copy_id, owner=owner, outcome="present")
            expected.add(copy.copy_id)
    cursor, found = None, []
    for _ in range(10):
        page = store.lookup(ref, birth, limit=2, cursor=cursor)
        found.extend(copy.copy_id for copy in page.copies)
        cursor = page.cursor
        if cursor is None:
            break
        assert page.state == "pending"
    assert set(found) == expected and len(found) == len(expected)
    assert page.state == "ready"


def test_lookup_copy_page_work_stays_bounded_at_20000_copies_of_one_image(store, monkeypatch, request):
    birth = store.allocate_birth()
    ref = reference()
    image_id = ready(store, ref)
    owner = OwnerLocator(owner="retained-history", locator=uuid.uuid4().hex, version=uuid.uuid4().hex)
    copy = store.reserve_copy(image_id, owner=owner, origin_ledger="native-ledger",
                              expected_header=store.image_header(image_id).value, expected_root=store.root)
    connect, work = store._connect, []

    def rows(count):
        for _ in range(count):
            value = copy.model_copy(update={"copy_id": uuid.uuid4().hex})
            yield value.copy_id, image_id, 0, generated.encode(value).decode()

    @contextmanager
    def measured():
        with connect() as conn:
            steps = []
            conn.set_progress_handler(lambda: steps.append(None) or 0, 1)
            try:
                yield conn
            finally:
                conn.set_progress_handler(None, 0)
                work.append(len(steps))

    with connect() as conn:
        conn.executemany("INSERT INTO copies(copy_id,image_id,closed,data) VALUES (?,?,?,?)", rows(39))
        conn.commit()
    with monkeypatch.context() as measured_scope:
        measured_scope.setattr(store, "_connect", measured)
        assert len(store.lookup(ref, birth, limit=8).copies) == 8
    baseline = sum(work)
    with connect() as conn:
        conn.executemany("INSERT INTO copies(copy_id,image_id,closed,data) VALUES (?,?,?,?)", rows(19_960))
        conn.commit()
    work.clear()
    with monkeypatch.context() as measured_scope:
        measured_scope.setattr(store, "_connect", measured)
        assert len(store.lookup(ref, birth, limit=8).copies) == 8
    expanded = sum(work)
    assert expanded <= baseline + 200
    request.node.user_properties.extend((("same_image_copies", 20_000),
                                       ("baseline_sqlite_vm_steps", baseline),
                                       ("expanded_sqlite_vm_steps", expanded)))


def test_change_floor_precedes_prefix_collection_and_stale_cache_requires_census(store, monkeypatch):
    monkeypatch.setattr(generated, "CHANGE_WINDOW", 4)
    remove = store._remove
    observed = []

    def checked_remove(path, guard):
        if path.parent.name == "changes":
            floor = store.head().change_floor
            assert int(path.stem) < floor
            observed.append(floor)
        return remove(path, guard)

    monkeypatch.setattr(store, "_remove", checked_remove)
    for _ in range(9):
        birth = store.allocate_birth()
    assert observed
    assert len(tuple((generated.canonical_root(store.root) / "changes").glob("*.json"))) <= 4
    with store._connect() as conn:
        conn.execute("UPDATE checkpoint SET applied_epoch=0 WHERE singleton=1")
        conn.commit()
    assert store.reconcile().state == "pending"
    ref = reference()
    assert store.lookup(ref, birth).state == "pending"
    assert store.census(limit=1).state == "ready"
    assert store.lookup(ref, birth).state == "ready"


def test_owner_copy_selector_survives_replica_and_20000_unrelated_locators(store, monkeypatch, tmp_path, request):
    locator = uuid.uuid4().hex
    old_owner = OwnerLocator(owner="retained-history", locator=locator, version=uuid.uuid4().hex)
    replacement_owner = old_owner.model_copy(update={"version": uuid.uuid4().hex})
    origin = writer_lease.active_manager().idempotency.capture_attempts.ledger_id
    old_image = ready(store, reference())
    old = store.reserve_copy(old_image, owner=old_owner, origin_ledger=origin,
                             expected_header=store.image_header(old_image).value, expected_root=store.root)
    store.ack_copy(old.copy_id, owner=old_owner, outcome="present")
    replacement_image = ready(store, reference())
    replacement = store.reserve_copy(replacement_image, owner=replacement_owner, origin_ledger=origin,
                                     expected_header=store.image_header(replacement_image).value, expected_root=store.root)
    store.close()
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "replica-selector-state"))
    replica = generated.GeneratedFragmentStore(store.root)
    assert replica.owner_copies(owner=old_owner.owner, locator=locator).state == "pending"
    for _ in range(40):
        if replica.census(limit=2).state == "ready":
            break
    bindings = replica.owner_copies(owner=old_owner.owner, locator=locator)
    assert bindings.state == "ready"
    assert {copy.copy_id for copy in bindings.copies} == {old.copy_id, replacement.copy_id}
    assert {copy.outcome for copy in bindings.copies} == {"present", "reserved"}
    first = replica.owner_copies(owner=old_owner.owner, locator=locator, limit=1)
    assert first.state == "pending" and first.cursor is not None
    second = replica.owner_copies(owner=old_owner.owner, locator=locator, limit=1, cursor=first.cursor)
    assert second.state == "ready" and second.cursor is None
    assert first.copies[0].copy_id != second.copies[0].copy_id
    connect, work = replica._connect, []

    @contextmanager
    def measured():
        with connect() as conn:
            steps = []
            conn.set_progress_handler(lambda: steps.append(None) or 0, 1)
            try:
                yield conn
            finally:
                conn.set_progress_handler(None, 0)
                work.append(len(steps))

    with monkeypatch.context() as measured_scope:
        measured_scope.setattr(replica, "_connect", measured)
        assert len(replica.owner_copies(owner=old_owner.owner, locator=locator).copies) == 2
    baseline = sum(work)

    def unrelated_rows():
        for _ in range(20_000):
            owner = old_owner.model_copy(update={"locator": uuid.uuid4().hex})
            value = old.model_copy(update={"copy_id": uuid.uuid4().hex, "owner": owner})
            yield value.copy_id, value.image_id, 0, generated.encode(value).decode()

    with connect() as conn:
        conn.executemany("INSERT INTO copies(copy_id,image_id,closed,data) VALUES (?,?,?,?)", unrelated_rows())
        conn.commit()
    work.clear()
    with monkeypatch.context() as measured_scope:
        measured_scope.setattr(replica, "_connect", measured)
        assert len(replica.owner_copies(owner=old_owner.owner, locator=locator).copies) == 2
    expanded = sum(work)
    assert expanded <= baseline + 100
    request.node.user_properties.extend((("unrelated_locators", 20_000),
                                       ("baseline_sqlite_vm_steps", baseline),
                                       ("expanded_sqlite_vm_steps", expanded)))
    replica.close()


def test_copy_reservation_refuses_an_image_it_did_not_verify(store, tmp_path):
    image_id = ready(store, reference())
    owner = OwnerLocator(owner="retained-history", locator=uuid.uuid4().hex, version=uuid.uuid4().hex)
    header = store.image_header(image_id).value
    with pytest.raises(ValueError, match="binding changed"):
        store.reserve_copy(image_id, owner=owner, origin_ledger="native-ledger",
                           expected_header=header.model_copy(update={"revision": uuid.uuid4().hex}), expected_root=store.root)
    with pytest.raises(ValueError, match="binding changed"):
        store.reserve_copy(image_id, owner=owner, origin_ledger="native-ledger",
                           expected_header=header, expected_root=tmp_path / "another-root")


def test_whole_binary_original_copy_survives_a_cold_index(store, monkeypatch, tmp_path):
    birth = store.allocate_birth()
    ref = reference()
    binary = b"\x00\xff\xfe\x80sealed binary original\x00"
    digest = hashlib.sha256(binary).hexdigest()
    header = store.begin_whole_image(whole_source_ref=ref, image_sha256=digest, image_size=len(binary))
    assert header.whole_source_ref == ref and header.opaque_source_refs is None
    owner = OwnerLocator(owner="binary-original-owner", locator=uuid.uuid4().hex, version=uuid.uuid4().hex)
    copy = store.reserve_copy(header.image_id, owner=owner,
                              origin_ledger=writer_lease.active_manager().idempotency.capture_attempts.ledger_id,
                              expected_header=header, expected_root=store.root)
    store.close()
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "whole-binary-replica"))
    replica = generated.GeneratedFragmentStore(store.root)
    assert replica.lookup(ref, birth).state == "pending"
    for _ in range(20):
        if replica.census(limit=1).state == "ready":
            break
    found = replica.lookup(ref, birth)
    assert found.image_ids == (header.image_id,) and found.copies == (copy,)
    # Canonical reservation stays outstanding until its native owner supplies an acknowledgement.
    assert found.state == "pending" and found.copies[0].outcome == "reserved"
    assert replica.image_header(header.image_id).value.whole_source_ref == ref
    replica.close()


def test_equal_whole_bytes_for_different_sources_have_distinct_immutable_images(store):
    first, second = reference(), reference()
    binary = b"\xff\x00same opaque original"
    digest = hashlib.sha256(binary).hexdigest()
    left = store.begin_whole_image(whole_source_ref=first, image_sha256=digest, image_size=len(binary))
    right = store.begin_whole_image(whole_source_ref=second, image_sha256=digest, image_size=len(binary))
    assert left.image_id != right.image_id
    assert left.image_sha256 == right.image_sha256 and left.image_size == right.image_size
    assert store.lookup(first, None).image_ids == (left.image_id,)
    assert store.lookup(second, None).image_ids == (right.image_id,)


def test_opaque_multi_source_copy_survives_a_cold_index(store, monkeypatch, tmp_path):
    birth = store.allocate_birth()
    first, second = reference(), reference()
    payload = b"\x80\x00opaque retained result"
    header = store.begin_opaque_image(source_refs=(first, second),
        image_sha256=hashlib.sha256(payload).hexdigest(), image_size=len(payload), unknown_through_epoch=0)
    assert header.whole_source_ref is None
    assert set(header.opaque_source_refs) == {first, second}
    owner = OwnerLocator(owner="capture-result", locator=uuid.uuid4().hex, version=uuid.uuid4().hex)
    copy = store.reserve_copy(header.image_id, owner=owner,
        origin_ledger=writer_lease.active_manager().idempotency.capture_attempts.ledger_id,
        expected_header=header, expected_root=store.root)
    store.close()
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "opaque-result-replica"))
    replica = generated.GeneratedFragmentStore(store.root)
    try:
        assert replica.lookup(first, birth).state == "pending"
        for _ in range(20):
            if replica.census(limit=1).state == "ready":
                break
        for ref in (first, second):
            found = replica.lookup(ref, birth)
            assert found.image_ids == (header.image_id,) and found.copies == (copy,)
            assert found.state == "pending" and found.copies[0].outcome == "reserved"
    finally:
        replica.close()


def test_equal_opaque_bytes_keep_distinct_source_membership(store):
    first, second, third = reference(), reference(), reference()
    payload = b"\x80same retained response"
    digest = hashlib.sha256(payload).hexdigest()
    left = store.begin_opaque_image(source_refs=(first, second), image_sha256=digest,
        image_size=len(payload), unknown_through_epoch=0)
    right = store.begin_opaque_image(source_refs=(first, third), image_sha256=digest,
        image_size=len(payload), unknown_through_epoch=0)
    assert left.image_id != right.image_id
    cursor, found = None, set()
    for _ in range(3):
        page = store.lookup(first, None, cursor=cursor)
        found.update(page.image_ids)
        cursor = page.cursor
        if cursor is None:
            break
    assert found == {left.image_id, right.image_id} and cursor is None
    assert store.lookup(second, None).image_ids == (left.image_id,)
    assert store.lookup(third, None).image_ids == (right.image_id,)


def test_oversized_opaque_membership_leaves_catalogue_unchanged(store):
    before = store.head()
    with pytest.raises(ValueError, match="generated_metadata_bound_exceeded"):
        store.begin_opaque_image(source_refs=tuple(reference() for _ in range(1300)),
            image_sha256=hashlib.sha256(b"opaque reply").hexdigest(), image_size=12,
            unknown_through_epoch=0)
    assert store.head() == before


@pytest.mark.parametrize("birth_state", ("missing", "mismatched", "future"))
def test_unknown_birth_preserves_explicit_whole_and_opaque_copy_obligations(store, birth_state):
    birth = store.allocate_birth()
    ref = reference()
    opaque_id = ready(store, ref)
    whole = store.begin_whole_image(whole_source_ref=ref, image_sha256=hashlib.sha256(b"\xff").hexdigest(), image_size=1)
    unrelated = store.begin_whole_image(whole_source_ref=reference(), image_sha256=whole.image_sha256, image_size=1)
    expected = set()
    for image_id in (opaque_id, whole.image_id):
        owner = OwnerLocator(owner="exact-copy-owner", locator=uuid.uuid4().hex, version=uuid.uuid4().hex)
        copy = store.reserve_copy(image_id, owner=owner, origin_ledger="native-owner-ledger",
                                  expected_header=store.image_header(image_id).value, expected_root=store.root)
        expected.add(copy.copy_id)
    if birth_state == "missing":
        evidence = None
    elif birth_state == "mismatched":
        evidence = ProducerBirth(catalogue_id=uuid.uuid4().hex, birth_epoch=birth.birth_epoch)
    else:
        evidence = ProducerBirth(catalogue_id=birth.catalogue_id, birth_epoch=store.head().epoch + 1)
    cursor, copies, images = None, set(), set()
    for _ in range(4):
        found = store.lookup(ref, evidence, limit=1, cursor=cursor)
        assert found.state == "unknown"
        assert found.uncertain_images == () and found.uncertainty_cursor is None
        copies.update(copy.copy_id for copy in found.copies)
        images.update(found.image_ids)
        cursor = found.cursor
        if cursor is None:
            break
    assert copies == expected and images == {opaque_id, whole.image_id}
    assert unrelated.image_id not in images


@pytest.mark.parametrize("damage", ("corrupt", "reassociated"))
def test_whole_header_evidence_loss_keeps_prior_positive_copy_membership(store, damage):
    birth = store.allocate_birth()
    ref = reference()
    header = store.begin_whole_image(whole_source_ref=ref, image_sha256=hashlib.sha256(b"\xff").hexdigest(), image_size=1)
    owner = OwnerLocator(owner="binary-original-owner", locator=uuid.uuid4().hex, version=uuid.uuid4().hex)
    copy = store.reserve_copy(header.image_id, owner=owner, origin_ledger="native-owner-ledger",
                              expected_header=header, expected_root=store.root)
    path = generated.canonical_root(store.root) / "images" / header.image_id[:2] / header.image_id / "head.json"
    if damage == "corrupt":
        path.write_bytes(b"broken image header")
    else:
        changed = header.model_copy(update={"whole_source_ref": reference(), "revision": uuid.uuid4().hex})
        path.write_bytes(generated.encode(changed))
    for _ in range(30):
        if store.census(limit=2).state == "ready":
            break
    found = store.lookup(ref, birth)
    assert found.state == "unknown"
    assert found.image_ids == (header.image_id,) and found.copies == (copy,)
    assert header.image_id in found.uncertain_images
    assert store.lookup(ref, None).copies == (copy,)


def test_an_image_needs_exactly_one_canonical_source_membership(store):
    digest = hashlib.sha256(b"\xff").hexdigest()
    with pytest.raises(ValueError, match="memory_ref_not_canonical"):
        store.begin_whole_image(whole_source_ref="unallocated-record", image_sha256=digest, image_size=1)
    header = store.begin_whole_image(whole_source_ref=reference(), image_sha256=digest, image_size=1)
    # An image without membership would be invisible to every Source lookup, and so to erasure.
    with pytest.raises(ValueError, match="generated_image_membership_invalid"):
        generated.ImageHeader.model_validate({**header.model_dump(), "whole_source_ref": None})
    with pytest.raises(ValueError, match="generated_image_membership_invalid"):
        generated.ImageHeader.model_validate({**header.model_dump(), "opaque_source_refs": ()})
