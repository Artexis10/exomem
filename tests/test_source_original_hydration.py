"""Hydrated Source originals retain custody even when a download or cleanup fails."""

from __future__ import annotations

import hashlib
import io
from contextlib import ExitStack, closing

import pytest
from test_media_store import _offload
from test_media_store import selected_store as selected_store

from exomem import (
    add,
    generated_fragment_store,
    held_fs,
    media_jobs,
    media_store,
    origin_bindings,
    originals,
    provenance,
    source_custody,
    source_native_custody,
    writer_lease,
)
from exomem.cli_ops import OpError
from exomem.governance.principal import owner_principal, request_scope


@pytest.fixture
def enrolled_original(vault, source_schema, selected_store, monkeypatch):  # noqa: F811
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')
    expected = b"Original transcript with exact binary custody.\n"
    manager = writer_lease.active_manager()
    with request_scope(owner_principal(surface="mcp")):
        with manager.mutation_guard(vault):
            saved = add.add(vault, source_schema, title="Attached transcript", content="", source_type="session",
                artifact=add.SourceArtifact(io.BytesIO(expected), "transcript.txt", "text/plain"),
                source_identity={"system": "archive", "record_id": "transcript", "revision": "one"})
        path = vault / saved.artifact_path
        _offload(vault, path)
        assert media_store.evict(vault, path)
        assert originals.original_state(vault, path).state == "evicted"
        yield manager, saved.ref, path, expected


def test_hydration_installs_an_exact_source_copy_without_holding_the_download_boundary(vault, enrolled_original, monkeypatch):
    manager, reference, path, expected = enrolled_original
    decrypt, admitted = media_store._decrypt_into, []

    def decrypt_with_prior_custody(*args):
        boundary = manager._mutation_coordinator_for(vault)
        assert not boundary.current_thread_holds_boundary()
        with manager.mutation_guard(vault):
            native = source_native_custody.NativeCustody(vault)
            admitted.extend(item.binding_id for item in native.lookup(source_ref=reference).reservations
                            if item.identity is not None and item.version is None)
        assert len(admitted) == 2
        return decrypt(*args)

    monkeypatch.setattr(media_store, "_decrypt_into", decrypt_with_prior_custody)
    with media_store.open_original(vault, path) as opened:
        assert opened.stream.read() == expected
    with manager.mutation_guard(vault):
        native = source_native_custody.NativeCustody(vault)
        observed = [native.observe(binding, destination=True) for binding in admitted]
        installed = [value for value in observed if value.state == "installed" and value.matching is True]
        assert len(installed) == 1
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        try:
            reservation = native.get(installed[0].binding_id)
            copy = store.copy_record(reservation.generated_copy_id)
            assert copy.state == "observed"
            assert copy.value.owner.version == installed[0].binding_id and copy.value.outcome == "present"
        finally:
            store.close()
    with closing(media_jobs.MediaJobStore(vault)._connect()) as conn:
        assert conn.execute("SELECT count(*) FROM media_access").fetchone()[0] == 0


@pytest.mark.parametrize("cleanup_refused", [False, True])
def test_partial_hydration_keeps_unknown_copy_and_capacity_only_when_exact_cleanup_fails(
        vault, enrolled_original, monkeypatch, cleanup_refused):
    manager, reference, path, _expected = enrolled_original
    allocations = []
    with held_fs.acquire(vault).require() as filesystem:
        backend = type(filesystem)
        unlink = backend.unlink

    def fail_after_plaintext(_store, _reference, _identity, sink):
        sink.write(b"partial plaintext")
        sink.flush()
        with manager.mutation_guard(vault):
            native = source_native_custody.NativeCustody(vault)
            allocations.extend(item for item in native.lookup(source_ref=reference).reservations
                               if item.identity is not None and item.version is None)
        # A different NativeCustody instance cannot claim continuity for an initializing allocation.
        assert len(allocations) == 2
        if cleanup_refused:
            identities = {item.identity.inode for item in allocations}

            def refuse_our_unlink(filesystem, file):
                if file.identity.inode in identities:
                    return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "temporary cleanup refused"))
                return unlink(filesystem, file)

            monkeypatch.setattr(backend, "unlink", refuse_our_unlink)
        raise media_store.MediaStoreError("MEDIA_STORE_UNAVAILABLE", "download interrupted after plaintext")

    monkeypatch.setattr(media_store, "_decrypt_into", fail_after_plaintext)
    with pytest.raises(media_store.MediaStoreError, match="download interrupted"):
        with media_store.open_original(vault, path):
            pytest.fail("an interrupted download cannot supply an original")
    assert not path.exists()
    with closing(media_jobs.MediaJobStore(vault)._connect()) as conn:
        rows = conn.execute("SELECT * FROM media_access").fetchall()
    if cleanup_refused:
        assert len(rows) == 1 and rows[0]["source_custody_json"] is not None
        assert rows[0]["temporary_bytes"] > 0
        temporary = path.parent / rows[0]["temporary_name"]
        assert temporary.read_bytes() == b"partial plaintext"
        monkeypatch.setattr(media_store, "holder_alive", lambda *_: False)
        assert media_store.clear_dead_reservations(vault) == 0
        assert temporary.read_bytes() == b"partial plaintext"
        with manager.mutation_guard(vault):
            native = source_native_custody.NativeCustody(vault)
            assert all(native.observe(item.binding_id).state == "unknown" for item in allocations)
    else:
        assert rows == []
        assert not any(held_fs.is_publish_temporary(item.name) for item in path.parent.iterdir())
        with manager.mutation_guard(vault):
            native = source_native_custody.NativeCustody(vault)
            assert all(native.observe(item.binding_id).state == "closed" for item in allocations)


def test_acquired_original_rechecks_present_bytes_without_hydration_and_pins_until_commit(
        vault, enrolled_original, monkeypatch):
    _manager, reference, path, expected = enrolled_original
    body = provenance.encode_origin({"inputs": {"original": {"representation": "original",
        "reference": reference, "version": hashlib.sha256(expected).hexdigest(), "size": len(expected)}},
        "assessments": [], "bindings": []}) + "\nRetain the observation.\n"
    with ExitStack() as lifetime:
        prepared = origin_bindings.acquire_origin_inputs(vault, body, lifetime=lifetime)
        proof = prepared.inputs[0][1]
        assert isinstance(proof, origin_bindings.OriginalInputProof) and proof.materialize is False
        assert path.read_bytes() == expected
        assert media_store.evict(vault, path) is False

        def unexpected_hydration(*_args, **_kwargs):
            pytest.fail("commit revalidation cannot hydrate another original copy")

        monkeypatch.setattr(media_store, "_hydrate", unexpected_hydration)
        proof.recheck(vault)
        prepared.revalidate(vault)
        path.unlink()  # Direct owner interference remains detectable despite the managed eviction pin.
        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_UNAVAILABLE"):
            proof.recheck(vault)
        with pytest.raises(provenance.OriginError, match="ORIGIN_INPUT_UNAVAILABLE"):
            prepared.revalidate(vault)
    with closing(media_jobs.MediaJobStore(vault)._connect()) as conn:
        assert conn.execute("SELECT count(*) FROM media_access").fetchone()[0] == 0


def test_retired_source_refuses_hydration_before_any_copy_or_download(vault, enrolled_original, monkeypatch):
    manager, reference, path, expected = enrolled_original
    with manager.mutation_guard(vault):
        manifest, guard = source_custody.read_manifest(vault, reference)
        retired = manifest.model_copy(update={"retirement": source_custody.RetirementFence(
            operation_id="withdraw-original", source_hash=manifest.allocated_hash)})
        source_custody.publish(vault, source_custody.PlannedCustody((source_custody._write(
            vault, source_custody.manifest_path(vault, reference), retired, guard, source_custody.MAX_MANIFEST_BYTES),)))
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        before = store.head()

    def unexpected_download(*_args, **_kwargs):
        pytest.fail("retired Source started a plaintext download")

    monkeypatch.setattr(media_store, "_decrypt_into", unexpected_download)
    with pytest.raises(OpError, match="SOURCE_RETIRED"):
        with media_store.open_original(vault, path):
            pytest.fail("retired Source supplied an original")
    body = provenance.encode_origin({"inputs": {"original": {"representation": "original",
        "reference": reference, "version": hashlib.sha256(expected).hexdigest(), "size": len(expected)}},
        "assessments": [], "bindings": []}) + "\nRetain the observation.\n"
    with ExitStack() as lifetime:
        with pytest.raises(provenance.OriginError, match="SOURCE_RETIRED"):
            origin_bindings.acquire_origin_inputs(vault, body, lifetime=lifetime)
    assert not path.exists()
    assert not any(held_fs.is_publish_temporary(item.name) for item in path.parent.iterdir())
    with manager.mutation_guard(vault):
        assert store.head() == before
        store.close()
    with closing(media_jobs.MediaJobStore(vault)._connect()) as conn:
        assert conn.execute("SELECT count(*) FROM media_access").fetchone()[0] == 0


def test_legacy_unknown_original_declaration_hydrates_without_claiming_whole_source_ownership(
        vault, enrolled_original):
    manager, reference, path, expected = enrolled_original
    with manager.mutation_guard(vault):
        manifest, guard = source_custody.read_manifest(vault, reference)
        legacy = manifest.model_copy(update={"originals": None})
        source_custody.publish(vault, source_custody.PlannedCustody((source_custody._write(
            vault, source_custody.manifest_path(vault, reference), legacy, guard, source_custody.MAX_MANIFEST_BYTES),)))
        native = source_native_custody.NativeCustody(vault)
        before = {item.binding_id for item in native.lookup().reservations}
    with media_store.open_original(vault, path) as opened:
        assert opened.stream.read() == expected
    with manager.mutation_guard(vault):
        retained = [item for item in native.lookup().reservations if item.binding_id not in before]
        assert len(retained) == 1 and retained[0].source_ref is None
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        try:
            image = store.image_header(retained[0].image_id).value
            assert image.whole_source_ref is None and image.opaque_source_refs == ()
            assert image.unknown_through_epoch > 0
            assert native.observe(retained[0].binding_id, destination=True).matching is True
        finally:
            store.close()
