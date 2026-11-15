"""Canonical publication rights precede private native-name allocation."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from exomem import generated_fragment_store as generated
from exomem import (
    generated_fragments,
    held_fs,
    native_copy_custody,
    source_native_custody,
    writer_lease,
)
from exomem.governance.principal import owner_principal, request_scope


@pytest.fixture
def producer(vault):
    with request_scope(owner_principal(surface="mcp")), writer_lease.active_manager().mutation_guard(vault):
        store = generated.GeneratedFragmentStore(vault)
        store.initialize()
        native = source_native_custody.NativeCustody(vault)
        copies = native_copy_custody.NativeCopyCustody(vault, store=store, native=native)
        try:
            yield store, native, copies
        finally:
            store.close()


def test_slot_precedes_native_name_without_losing_a_later_uncertainty_summary(producer):
    store, native, copies = producer
    data = b"Exact shared entry."
    reference = generated_fragments.SourceRef(f"exomem://memory/{uuid.UUID(int=51)}")
    rendered = generated_fragments._Image(data.decode()).own(reference).plan(hashlib.sha256(b"").hexdigest())
    build = store.begin_image(data, rendered, unknown_through_epoch=0)
    for page in build.pages:
        store.append_image(page)
    image = store.seal_image(build.header.image_id)
    prepared = copies.prepare(image, logical_path=store.root / "shared.md")
    publication = store.prepare_slot(owner=prepared.canonical.owner.owner, locator=prepared.canonical.owner.locator,
        physical_scope=copies.physical_scope, expected_revision=None, expected_current_copy_id=None,
        candidate=prepared.canonical, publication_id=uuid.uuid4().hex)
    assert store.check_slot(publication).candidate.copy_id == prepared.canonical.copy_id
    with pytest.raises(source_native_custody.NativeCustodyUnavailable):
        native.get(prepared.canonical.owner.version)
    assert not (store.root / "ready.tmp").exists()
    uncertainty = store.mark_uncertain(image.image_id)
    allocated = copies.allocate(prepared, relative_path="ready.tmp", destination="shared.md", removable=False,
                                publication_id=publication.publication_id)
    assert allocated.canonical == prepared.canonical
    with held_fs.acquire(store.root).require() as fs, fs.parent(".").require() as parent:
        with fs.file(parent, "ready.tmp", access="write", create=True, exclusive=True).require() as file:
            native.bind(allocated.native, file)
            fs.write(file, data).require()
            copies.verified(allocated, file, image.image_sha256)
            version = file.version()
        held_fs.install_temporary(fs, parent, "ready.tmp", "shared.md", expected_identity=version.identity,
            expected_version=version, after_install=lambda witness: copies.installed(allocated, witness)).require()
    copies.present(allocated)

    def witnessed(binding):
        assert native.observe(allocated.native.binding_id, destination=True).matching is True
        return generated.NativeSlotWitness(binding.slot.slot_id, binding.slot.revision, copies.physical_scope,
                                            publication.publication_id, "candidate")

    settled = store.settle_slot(publication, witness=witnessed)
    assert settled.current.copy_id == prepared.canonical.copy_id
    assert settled.unknown_through_epoch == uncertainty.unknown_through_epoch
    assert (store.root / "shared.md").read_bytes() == data


def test_closed_canonical_reservation_cannot_acquire_a_native_name(producer):
    store, native, copies = producer
    data = b"Not published."
    image = store.begin_whole_image(whole_source_ref=f"exomem://memory/{uuid.UUID(int=52)}",
        image_sha256=hashlib.sha256(data).hexdigest(), image_size=len(data))
    prepared = copies.prepare(image, logical_path=store.root / "withdrawn.md")
    store.ack_copy(prepared.canonical.copy_id, owner=prepared.canonical.owner, outcome="no_effect")
    with pytest.raises(ValueError, match="differs from its canonical reservation"):
        copies.allocate(prepared, relative_path="never.tmp", destination="withdrawn.md")
    with pytest.raises(source_native_custody.NativeCustodyUnavailable):
        native.get(prepared.canonical.owner.version)
    assert not (store.root / "never.tmp").exists()
