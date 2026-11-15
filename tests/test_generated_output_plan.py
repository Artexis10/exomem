"""Renderers recover exact ownership without trusting a content hash or local index."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from exomem import generated_fragment_store as generated
from exomem import (
    generated_fragments,
    generated_output_plan,
    held_fs,
    native_copy_custody,
    source_native_custody,
    writer_lease,
)
from exomem import vault as vault_module
from exomem.governance.principal import owner_principal, request_scope


@pytest.fixture
def installed(vault):
    with request_scope(owner_principal(surface="mcp")), writer_lease.active_manager().mutation_guard(vault):
        store = generated.GeneratedFragmentStore(vault)
        store.initialize()
        native = source_native_custody.NativeCustody(vault)
        copies = native_copy_custody.NativeCopyCustody(vault, store=store, native=native)
        text = "A retained café entry.\r\n"
        reference = generated_fragments.SourceRef(f"exomem://memory/{uuid.UUID(int=41)}")
        rendered = generated_fragments._Image(text).own(reference).plan(hashlib.sha256(b"").hexdigest())
        build = store.begin_image(text.encode(), rendered, unknown_through_epoch=0)
        for page in build.pages:
            store.append_image(page)
        image = store.seal_image(build.header.image_id)
        path = vault / "generated.md"
        copy = copies.reserve(image, logical_path=path, relative_path="prepared.tmp", destination="generated.md", removable=False)
        publication = store.prepare_slot(owner=copy.canonical.owner.owner, locator=copy.canonical.owner.locator,
            physical_scope=copies.physical_scope, expected_revision=None, expected_current_copy_id=None,
            candidate=copy.canonical, publication_id=uuid.uuid4().hex)
        with held_fs.acquire(vault).require() as fs, fs.parent(".").require() as parent:
            with fs.file(parent, "prepared.tmp", access="write", create=True, exclusive=True).require() as file:
                native.bind(copy.native, file)
                fs.write(file, text.encode()).require()
                copies.verified(copy, file, rendered.after.image_sha256)
                version = file.version()
            held_fs.install_temporary(fs, parent, "prepared.tmp", path.name, expected_identity=version.identity,
                expected_version=version, after_install=lambda witness: copies.installed(copy, witness)).require()
        copies.present(copy)

        def witnessed(binding):
            assert native.observe(copy.native.binding_id, destination=True).matching is True
            return generated.NativeSlotWitness(binding.slot.slot_id, binding.slot.revision, copies.physical_scope,
                binding.slot.pending.publication_id, "candidate")

        store.settle_slot(publication, witness=witnessed)
        try:
            yield store, native, copy, path, reference
        finally:
            store.close()


def test_cold_renderer_preserves_exact_ownership_and_seals_the_final_write(installed, monkeypatch):
    store, native, copy, path, reference = installed
    monkeypatch.setattr(store, "_connect", lambda: pytest.fail("renderer consulted the derived index"))
    text, guard = vault_module.read_guarded_text(store.root, path)
    before = generated_output_plan.read_before(store, native, path=path, text=text, guard=guard)
    assert before.current.canonical.copy_id == copy.canonical.copy_id
    assert before.mapping.unknown_through_epoch == 0
    image = generated_fragments._Image.before(text, before.mapping, before.mapping.before_sha256)
    image.splice(len(image.text), len(image.text), generated_fragments._Image("Unowned footer.\r\n"))
    rendered = image.plan(before.mapping.before_sha256)
    plan = generated_output_plan.seal(before, content=image.text, rendered=rendered)
    write = vault_module.PlannedWrite(path, image.text, guard=guard)
    plan.validate(store.root, write)
    assert plan.after.after.fragments[0].source_ref == reference.value
    assert image.text.encode()[plan.after.after.fragments[0].start_byte:plan.after.after.fragments[0].end_byte] == text.encode()
    write.content += "changed after sealing"
    with pytest.raises(ValueError, match="fragment_image_hash_mismatch"):
        plan.validate(store.root, write)


@pytest.mark.parametrize("loss", ["private-row", "equal-byte-rewrite", "map-page"])
def test_lost_proof_cannot_select_a_map_by_equal_bytes_or_erase_old_obligations(installed, loss):
    store, native, copy, path, reference = installed
    if loss == "private-row":
        with native._connect() as conn:
            conn.execute("DELETE FROM source_native_bindings WHERE binding_id=?", (copy.native.binding_id,))
    elif loss == "equal-byte-rewrite":
        path.write_bytes(path.read_bytes())
        assert native.observe(copy.native.binding_id, destination=True).matching is False
    else:
        page = store._object_path(generated.ObjectChange(kind="page", object_id=copy.canonical.image_id,
            image_id=copy.canonical.image_id, page_number=0))
        page.write_bytes(b"invalid canonical page")
    text, guard = vault_module.read_guarded_text(store.root, path)
    before = generated_output_plan.read_before(store, native, path=path, text=text, guard=guard)
    assert before.current is None and before.mapping.after.fragments == ()
    assert before.mapping.unknown_through_epoch == store.head().epoch
    assert before.binding.current.copy_id == copy.canonical.copy_id and not before.binding.current.closed
    image = generated_fragments._Image.before(text, before.mapping, before.mapping.before_sha256)
    plan = generated_output_plan.seal(before, content=text, rendered=image.plan(before.mapping.before_sha256))
    assert plan.after.unknown_through_epoch == before.checkpoint


def test_missing_provenance_stays_unknown_but_an_absent_new_file_has_no_old_prose(installed):
    store, native, _, _, _ = installed
    path = store.root / "legacy.md"
    path.write_text("Untracked old prose.")
    text, guard = vault_module.read_guarded_text(store.root, path)
    legacy = generated_output_plan.read_before(store, native, path=path, text=text, guard=guard)
    assert legacy.mapping.unknown_through_epoch == store.head().epoch
    path = store.root / "fresh.md"
    guard = vault_module.PathGuard.capture(store.root, path.name, leaf_policy="absent")
    fresh = generated_output_plan.read_before(store, native, path=path, text="", guard=guard)
    assert fresh.mapping.unknown_through_epoch == 0 and fresh.mapping.coverage_complete
    assert fresh.mapping.after.fragments == ()


def test_equal_byte_pending_candidate_cannot_replace_the_native_current_map(installed):
    store, native, old, path, old_reference = installed
    text, guard = vault_module.read_guarded_text(store.root, path)
    reference = generated_fragments.SourceRef(f"exomem://memory/{uuid.UUID(int=42)}")
    rendered = generated_fragments._Image(text).own(reference).plan(hashlib.sha256(b"").hexdigest())
    build = store.begin_image(text.encode(), rendered, unknown_through_epoch=0)
    for page in build.pages:
        store.append_image(page)
    image = store.seal_image(build.header.image_id)
    copies = native_copy_custody.NativeCopyCustody(store.root, store=store, native=native)
    candidate = copies.reserve(image, logical_path=path, relative_path="next.tmp", destination=path.name, removable=False)
    binding = store.read_slot(owner=old.canonical.owner.owner, locator=old.canonical.owner.locator,
                              physical_scope=copies.physical_scope)
    store.prepare_slot(owner=old.canonical.owner.owner, locator=old.canonical.owner.locator,
        physical_scope=copies.physical_scope, expected_revision=binding.slot.revision,
        expected_current_copy_id=old.canonical.copy_id, candidate=candidate.canonical, publication_id=uuid.uuid4().hex)
    before = generated_output_plan.read_before(store, native, path=path, text=text, guard=guard)
    assert before.current.canonical.copy_id == old.canonical.copy_id
    assert before.binding.candidate.copy_id == candidate.canonical.copy_id
    assert {item.source_ref for item in before.mapping.after.fragments} == {old_reference.value}
    assert before.mapping.after.image_sha256 == rendered.after.image_sha256


def test_equal_byte_change_while_loading_map_preserves_uncertainty(installed, monkeypatch):
    store, native, copy, path, _ = installed
    text, guard = vault_module.read_guarded_text(store.root, path)
    image_page = store.image_page

    def changed(image_id, number):
        page = image_page(image_id, number)
        path.write_bytes(path.read_bytes())
        return page

    monkeypatch.setattr(store, "image_page", changed)
    before = generated_output_plan.read_before(store, native, path=path, text=text, guard=guard)
    assert before.current is None and before.mapping.after.fragments == ()
    assert before.mapping.unknown_through_epoch == before.checkpoint
    assert before.binding.current.copy_id == copy.canonical.copy_id


def test_auxiliary_replay_binds_ownership_even_when_output_bytes_are_equal(installed):
    from exomem import relation_review

    store, native, _, path, _ = installed
    text, guard = vault_module.read_guarded_text(store.root, path)
    before = generated_output_plan.read_before(store, native, path=path, text=text, guard=guard)
    current_plan = generated_output_plan.seal(before, content=text, rendered=before.mapping)
    current = vault_module.PlannedWrite(path, text, guard=guard, generated_image=current_plan)
    reference = generated_fragments.SourceRef(f"exomem://memory/{uuid.UUID(int=43)}")
    other_map = generated_fragments._Image(text).own(reference).plan(before.mapping.before_sha256)
    different_plan = generated_output_plan.seal(before, content=text, rendered=other_map)
    different = vault_module.PlannedWrite(path, text, guard=guard, generated_image=different_plan)
    assert relation_review._auxiliary_hash((current,), store.root) != relation_review._auxiliary_hash((different,), store.root)
    assert relation_review._all_auxiliaries_match((current,), store.root)
    assert not relation_review._all_auxiliaries_match((different,), store.root)
    detached, = relation_review._detach_auxiliaries(store.root, (current,), primary="primary.md", artifact="record.json")
    assert detached.generated_image is current_plan
    assert detached.generated_image.fingerprint == current_plan.fingerprint


@pytest.mark.parametrize("rollback", [False, True])
def test_common_batch_publishes_equal_bytes_with_new_ownership_and_fresh_inverse(installed, monkeypatch, rollback):
    """Equal hashes cannot preserve a displaced map or reuse the old allocation during rollback."""
    store, native, old, path, old_reference = installed
    text, guard = vault_module.read_guarded_text(store.root, path)
    before = generated_output_plan.read_before(store, native, path=path, text=text, guard=guard)
    reference = generated_fragments.SourceRef(f"exomem://memory/{uuid.UUID(int=44)}")
    rendered = generated_fragments._Image(text).own(reference).plan(before.mapping.before_sha256)
    plan = generated_output_plan.seal(before, content=text, rendered=rendered)
    write = vault_module.PlannedWrite(path, text, guard=guard, generated_image=plan)
    published = []

    def after_install(target):
        if target == path:
            current = native.lookup(limit=128).reservations
            candidates = [copy for copy in current if copy.destination_relative == path.name
                          and native.observe(copy.binding_id, destination=True).matching is True]
            assert len(candidates) == 1
            published.append(candidates[0])
            if rollback:
                raise RuntimeError("parent operation failed")

    monkeypatch.setattr(vault_module, "_after_batch_destination_published", after_install)
    if rollback:
        with pytest.raises(RuntimeError, match="parent operation failed"):
            vault_module.batch_atomic_write([write], vault_root=store.root, post_commit_fanout=False)
    else:
        vault_module.batch_atomic_write([write], vault_root=store.root, post_commit_fanout=False)
    text, guard = vault_module.read_guarded_text(store.root, path)
    final = generated_output_plan.read_before(store, native, path=path, text=text, guard=guard)
    assert text == "A retained café entry.\r\n"
    assert final.current is not None and final.binding.slot.pending is None
    assert final.current.canonical.copy_id != old.canonical.copy_id
    assert {item.source_ref for item in final.mapping.after.fragments} == {old_reference.value if rollback else reference.value}
    assert store.copy_record(old.canonical.copy_id).value.outcome == "retired"
    assert len(published) == 1
    if rollback:
        assert final.current.canonical.copy_id != published[0].generated_copy_id
        assert store.copy_record(published[0].generated_copy_id).value.outcome == "retired"
    else:
        assert final.current.canonical.copy_id == published[0].generated_copy_id
    assert native.observe(final.current.native.binding_id, destination=True).matching is True


def test_native_retry_uses_a_fresh_candidate_and_closes_the_refused_copy(installed, monkeypatch):
    """A sharing refusal must not rebind a closed candidate to the retry's new allocation."""
    store, native, old, path, _ = installed
    text, guard = vault_module.read_guarded_text(store.root, path)
    before = generated_output_plan.read_before(store, native, path=path, text=text, guard=guard)
    reference = generated_fragments.SourceRef(f"exomem://memory/{uuid.UUID(int=45)}")
    plan = generated_output_plan.seal(before, content=text,
        rendered=generated_fragments._Image(text).own(reference).plan(before.mapping.before_sha256))
    with held_fs.acquire(store.root).require() as filesystem:
        backend = type(filesystem)
    rename = backend.rename
    refused = []

    def refuse_first(filesystem, source, parent, leaf, **options):
        if leaf == path.name and not refused:
            binding = store.read_slot(owner=old.canonical.owner.owner, locator=old.canonical.owner.locator,
                                      physical_scope=native.physical_scope)
            refused.append(binding.candidate)
            return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "transient sharing refusal",
                                                               cause=PermissionError("sharing refusal")))
        return rename(filesystem, source, parent, leaf, **options)

    def retry_once(publish, *, recheck):
        # Exercise the existing retry seam on this host; native Windows qualification remains separate.
        try:
            publish()
        except PermissionError:
            recheck()
            publish()

    monkeypatch.setattr(backend, "rename", refuse_first)
    monkeypatch.setattr(vault_module, "replace_tolerating_transient_sharing", retry_once)
    write = vault_module.PlannedWrite(path, text, guard=guard, generated_image=plan)
    vault_module.batch_atomic_write([write], vault_root=store.root, post_commit_fanout=False)
    current = store.read_slot(owner=old.canonical.owner.owner, locator=old.canonical.owner.locator,
                              physical_scope=native.physical_scope)
    assert len(refused) == 1 and store.copy_record(refused[0].copy_id).value.closed
    assert current.slot.pending is None and current.current.copy_id != refused[0].copy_id
    assert current.current.owner.version != refused[0].owner.version
    observed = native.observe(current.current.owner.version, destination=True)
    assert observed.matching is True and observed.state == "installed"
    text, guard = vault_module.read_guarded_text(store.root, path)
    final = generated_output_plan.read_before(store, native, path=path, text=text, guard=guard)
    assert {item.source_ref for item in final.mapping.after.fragments} == {reference.value}


def test_new_generated_destination_rollback_retires_its_copy_and_settles_absence(installed, monkeypatch):
    store, native, _, _, _ = installed
    path = store.root / "fresh-log.md"
    guard = vault_module.PathGuard.capture(store.root, path.name, leaf_policy="absent")
    before = generated_output_plan.read_before(store, native, path=path, text="", guard=guard)
    reference = generated_fragments.SourceRef(f"exomem://memory/{uuid.UUID(int=46)}")
    text = "A fresh generated entry.\r\n"
    plan = generated_output_plan.seal(before, content=text,
        rendered=generated_fragments._Image(text).own(reference).plan(before.mapping.before_sha256))
    installed_copy = []

    def fail_after_install(target):
        if target == path:
            binding = store.read_slot(owner=source_native_custody.OWNER,
                locator=generated.file_locator(before.catalogue_id, path.name), physical_scope=native.physical_scope)
            installed_copy.append(binding.candidate)
            raise RuntimeError("parent operation failed")

    monkeypatch.setattr(vault_module, "_after_batch_destination_published", fail_after_install)
    write = vault_module.PlannedWrite(path, text, guard=guard, create_only=True, generated_image=plan)
    with pytest.raises(RuntimeError, match="parent operation failed"):
        vault_module.batch_atomic_write([write], vault_root=store.root, post_commit_fanout=False)
    binding = store.read_slot(owner=source_native_custody.OWNER,
        locator=generated.file_locator(before.catalogue_id, path.name), physical_scope=native.physical_scope)
    assert not path.exists() and binding.current is None and binding.slot.pending is None
    assert len(installed_copy) == 1 and store.copy_record(installed_copy[0].copy_id).value.outcome == "retired"
    assert all(copy.destination_relative != path.name for copy in native.lookup(limit=128).reservations)


def test_failed_stage_closes_a_publication_whose_native_reservation_never_started(installed, monkeypatch):
    store, native, _, _, _ = installed
    path = store.root / "not-staged.md"
    guard = vault_module.PathGuard.capture(store.root, path.name, leaf_policy="absent")
    before = generated_output_plan.read_before(store, native, path=path, text="", guard=guard)
    text = "The producer never staged these bytes."
    reference = generated_fragments.SourceRef(f"exomem://memory/{uuid.UUID(int=47)}")
    plan = generated_output_plan.seal(before, content=text,
        rendered=generated_fragments._Image(text).own(reference).plan(before.mapping.before_sha256))
    create = vault_module._BatchWorkspace.create_artifact
    refused = []

    def refuse_stage(workspace, name, content, **options):
        if content == text.encode():
            binding = store.read_slot(owner=source_native_custody.OWNER,
                locator=generated.file_locator(before.catalogue_id, path.name), physical_scope=native.physical_scope)
            refused.append(binding.candidate)
            raise RuntimeError("stage admission failed")
        return create(workspace, name, content, **options)

    monkeypatch.setattr(vault_module._BatchWorkspace, "create_artifact", refuse_stage)
    write = vault_module.PlannedWrite(path, text, guard=guard, create_only=True, generated_image=plan)
    with pytest.raises(RuntimeError, match="stage admission failed"):
        vault_module.batch_atomic_write([write], vault_root=store.root, post_commit_fanout=False)
    assert len(refused) == 1 and store.copy_record(refused[0].copy_id).value.outcome == "no_effect"
    with pytest.raises(source_native_custody.NativeCustodyUnavailable):
        native.get(refused[0].owner.version)
    binding = store.read_slot(owner=source_native_custody.OWNER,
        locator=generated.file_locator(before.catalogue_id, path.name), physical_scope=native.physical_scope)
    assert not path.exists() and binding.current is None and binding.slot.pending is None
