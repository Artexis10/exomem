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
