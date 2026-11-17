"""Issued Source original declarations outlive their physical copy metadata."""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811
from __future__ import annotations

import hashlib
import io
from contextlib import contextmanager

import pytest
from pydantic import ValidationError
from test_capture_attempts import attempts, authored, reserve  # noqa: F401
from test_source_custody import registered_custody  # noqa: F401

from exomem import (
    add,
    generated_fragment_store,
    held_fs,
    memory_refs,
    native_copy_custody,
    source_custody,
    source_native_custody,
    source_payload_custody,
    vocabulary_auxiliaries,
)
from exomem import (
    vault as vault_module,
)
from exomem.cli_ops import OpError
from exomem.governance.principal import owner_principal, request_scope


def test_captured_original_declaration_survives_native_retirement_and_catalogue_collection(
    vault, source_schema, attempts, registered_custody,
):
    """Collecting copy metadata cannot erase the issued Source's original identity."""
    manager, _ = attempts
    original = b"\x00\xffsealed original bytes"
    digest = hashlib.sha256(original).hexdigest()
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        saved = add.add(
            vault, source_schema, title="Attached record", content="", source_type="session",
            artifact=add.SourceArtifact(io.BytesIO(original), "record.bin", "application/octet-stream"),
            source_identity={"system": "archive", "record_id": "attached-record", "revision": "one"},
        )
        manifest, _ = source_custody.read_manifest(vault, saved.ref)
        assert manifest.originals == (source_custody.OriginalBinding(sha256=digest, size=len(original)),)
        assert manifest.publication == "published"
        path = vault / saved.artifact_path
        assert path.read_bytes() == original
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        native = source_native_custody.NativeCustody(vault)
        copies = native_copy_custody.NativeCopyCustody(vault, store=store, native=native)
        try:
            retained, cursor = [], None
            for _ in range(16):
                page = store.lookup(saved.ref, None, cursor=cursor)
                retained.extend(page.copies)
                cursor = page.cursor
                if cursor is None:
                    break
            assert cursor is None
            original_copy = next(copy for copy in retained
                                 if store.image_header(copy.image_id).value.image_sha256 == digest)
            reservation = native.get(original_copy.owner.version)
            allocation = native_copy_custody.CopyAllocation(original_copy, reservation)
            with held_fs.acquire(vault).require() as filesystem:
                with filesystem.parent(path.parent.relative_to(vault).as_posix()).require() as parent:
                    held_fs.publish_bytes(
                        filesystem, parent, path.name, b"",
                        expected_identity=reservation.version.identity,
                        after_retire=lambda witness: copies.retired(allocation, witness),
                    ).require()
            assert native.observe(reservation.binding_id, destination=True).outcome == "removed"
            for _ in range(6):
                if store.collect(original_copy.image_id).state == "missing":
                    break
            assert store.image_header(original_copy.image_id).state == "missing"
            retained, _ = source_custody.read_manifest(vault, saved.ref)
            assert retained.originals == manifest.originals
        finally:
            store.close()


def test_text_capture_declares_known_empty_originals(vault, attempts, registered_custody):
    _, call = attempts
    args = authored()
    saved = call("capture_source", **args, capture_reservation=reserve(call, args))
    reference = memory_refs.ref_from_markdown((vault / saved["source"]["path"]).read_text())
    with request_scope(owner_principal(surface="mcp")):
        manifest, _ = source_custody.read_manifest(vault, reference)
    assert manifest.originals == ()
    assert manifest.publication == "published"


def test_failed_parent_retains_sealed_original_allocation_without_publication(
    vault, source_schema, attempts, registered_custody, monkeypatch,
):
    manager, _ = attempts
    original = b"unpublished original"
    captured = {}

    def fail_parent(writes, **kwargs):
        captured.update(primary=writes[0], writes=writes, auxiliaries=kwargs["_vocabulary_auxiliaries"])
        raise RuntimeError("parent unavailable")

    monkeypatch.setattr(add, "batch_atomic_write", fail_parent)
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with pytest.raises(RuntimeError, match="^parent unavailable$"):
            add.add(
                vault, source_schema, title="Unpublished attachment", content="", source_type="session",
                artifact=add.SourceArtifact(io.BytesIO(original), "record.bin", "application/octet-stream"),
                source_identity={"system": "archive", "record_id": "unpublished-record", "revision": "one"},
            )
        primary = captured["primary"]
        reference = memory_refs.ref_from_markdown(primary.content)
        manifest, _ = source_custody.read_manifest(vault, reference)
        assert manifest.publication == "allocated"
        assert manifest.allocated_hash == vault_module.content_hash(primary.content)
        assert manifest.originals == (
            source_custody.OriginalBinding(sha256=hashlib.sha256(original).hexdigest(), size=len(original)),
        )
        assert not primary.path.exists()
        original_write = next(write for write in captured["writes"]
                              if isinstance(write.content, vault_module.PreparedBinaryContent))
        assert not original_write.path.exists()
        replacement = b"different original"
        changed = vault_module.PlannedWrite(
            original_write.path,
            vault_module.PreparedBinaryContent(io.BytesIO(replacement), len(replacement), hashlib.sha256(replacement).hexdigest()),
            create_only=True, expected_hash=vault_module.MISSING_CONTENT_HASH,
        )
        sealed = vocabulary_auxiliaries.seal(
            vault, primary=primary, derived=[(vocabulary_auxiliaries.SOURCE_ORIGINAL, changed)],
        )
        with pytest.raises(OpError, match="SOURCE_CUSTODY_TARGET_CHANGED"):
            with source_payload_custody.capture(
                vault, reference=reference, primary=primary, writes=(primary, changed), auxiliaries=sealed, enabled=True,
            ):
                pytest.fail("changed originals acquired a publication right")
        retained, _ = source_custody.read_manifest(vault, reference)
        assert retained == manifest


@pytest.mark.parametrize("damage", ["missing-original", "changed-original", "changed-primary"])
def test_capture_rejects_unmatched_sealed_claims_before_binding_declarations(
    vault, source_schema, attempts, registered_custody, monkeypatch, damage,
):
    manager, _ = attempts
    capture = source_payload_custody.capture
    attempted = {}

    @contextmanager
    def damaged_capture(root, *, reference, primary, writes, auxiliaries, enabled):
        attempted["reference"] = reference
        original = next(write for write in writes if isinstance(write.content, vault_module.PreparedBinaryContent))
        if damage == "missing-original":
            writes = tuple(write for write in writes if write is not original)
        elif damage == "changed-original":
            original.content.sha256 = "0" * 64
        else:
            primary.content += "\nChanged after sealing.\n"
        with capture(root, reference=reference, primary=primary, writes=writes, auxiliaries=auxiliaries, enabled=enabled) as adapter:
            yield adapter

    monkeypatch.setattr(source_payload_custody, "capture", damaged_capture)
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with pytest.raises(ValueError, match="differs from its sealed declaration"):
            add.add(
                vault, source_schema, title="Unmatched attachment", content="", source_type="session",
                artifact=add.SourceArtifact(io.BytesIO(b"original"), "record.bin", "application/octet-stream"),
                source_identity={"system": "archive", "record_id": "unmatched-record", "revision": "one"},
            )
        manifest, _ = source_custody.read_manifest(vault, attempted["reference"])
        assert manifest.originals is None
        assert manifest.publication == "allocated"


def test_original_binding_requires_actual_boundary_exact_allocation_and_bounded_typed_declarations(vault, attempts):
    manager, _ = attempts
    reference = memory_refs.memory_ref(memory_refs.new_id())
    with pytest.raises(OpError, match="SOURCE_CUSTODY_BOUNDARY_REQUIRED"):
        source_custody.bind_originals(vault, reference=reference, source_hash="1" * 64, originals=())
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with pytest.raises(OpError, match="SOURCE_CUSTODY_UNAVAILABLE"):
            source_custody.bind_originals(vault, reference=reference, source_hash="1" * 64, originals=())
        source_custody.publish(vault, source_custody.prepare_enrollment(
            vault, reference=reference, source_hash="1" * 64,
        ))
        with pytest.raises(OpError, match="SOURCE_CUSTODY_TARGET_CHANGED"):
            source_custody.bind_originals(vault, reference=reference, source_hash="2" * 64, originals=())
        with pytest.raises(ValueError, match="typed bindings"):
            source_custody.bind_originals(vault, reference=reference, source_hash="1" * 64,
                                         originals=({"sha256": "2" * 64, "size": 1},))
        with pytest.raises(ValidationError):
            source_custody.bind_originals(vault, reference=reference, source_hash="1" * 64,
                originals=(source_custody.OriginalBinding.model_construct(sha256="2" * 64, size=True),))
        too_many = tuple(source_custody.OriginalBinding(sha256=f"{number:064x}", size=1)
                         for number in range(source_custody.MAX_ORIGINAL_BINDINGS + 1))
        with pytest.raises(ValidationError, match="at most 64"):
            source_custody.bind_originals(vault, reference=reference, source_hash="1" * 64, originals=too_many)
        manifest, _ = source_custody.read_manifest(vault, reference)
        assert manifest.originals is None


def test_original_binding_canonicalizes_retry_and_keeps_the_retirement_fence(vault, attempts, monkeypatch):
    manager, _ = attempts
    reference = memory_refs.memory_ref(memory_refs.new_id())
    first = source_custody.OriginalBinding(sha256="a" * 64, size=2)
    second = source_custody.OriginalBinding(sha256="b" * 64, size=1)
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        source_custody.publish(vault, source_custody.prepare_enrollment(vault, reference=reference, source_hash="1" * 64))
        source_custody.bind_originals(vault, reference=reference, source_hash="1" * 64, originals=(second, first, second))
        manifest, guard = source_custody.read_manifest(vault, reference)
        assert manifest.originals == (first, second)
        retired = manifest.model_copy(update={"retirement": source_custody.RetirementFence(
            operation_id="withdrawal", source_hash=manifest.allocated_hash,
        )})
        retirement = source_custody.PlannedCustody((source_custody._write(
            vault, source_custody.manifest_path(vault, reference), retired, guard, source_custody.MAX_MANIFEST_BYTES,
        ),))
        publish = source_custody.publish
        monkeypatch.setattr(source_custody, "publish", lambda *a, **k: pytest.fail("identical retry rewrote issued declarations"))
        source_custody.bind_originals(vault, reference=reference, source_hash="1" * 64, originals=(first, second))
        publish(vault, retirement)
        with pytest.raises(OpError, match="SOURCE_RETIRED"):
            source_custody.bind_originals(vault, reference=reference, source_hash="1" * 64, originals=(first, second))


def test_unenrolled_default_capture_creates_no_declarations_or_catalogue(vault, attempts):
    _, call = attempts
    assert call("capture_source", **authored())["capture_outcome"] == "created"
    assert not source_custody.manifest_root(vault).exists()
    assert not generated_fragment_store.canonical_root(vault).exists()
