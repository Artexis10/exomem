"""Canonical custody survives lost terminals without inventing producer completeness."""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_capture_attempts import attempts, authored, dispatch, reserve  # noqa: F401
from test_confined_writers import READABLE_SOURCE, _original, _remember
from test_confined_writers import citing as citing  # noqa: F401

from exomem import (
    capture_attempts,
    commands,
    curation,
    curation_witness,
    memory_refs,
    source_closure,
    source_custody,
    writer_lease,
)
from exomem import vault as vault_module
from exomem.governance.principal import owner_principal, request_scope


@pytest.fixture
def registered_custody(monkeypatch):
    assert source_custody.available()
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')


def test_capture_publishes_canonical_custody_before_a_lost_terminal(vault, attempts, registered_custody, monkeypatch):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)

    def lose_terminal(*args, **kwargs):
        raise RuntimeError("terminal unavailable")

    monkeypatch.setattr(manager.idempotency.capture_attempts, "complete", lose_terminal)
    with pytest.raises(writer_lease._PostCommitOutcomeUncertain):
        call("capture_source", **args, capture_reservation=ticket)
    settled = call("settle_source_capture", reservation=ticket)
    assert settled["past_outcome"] == "unknown"
    target = settled["owned_targets"][0]
    assert set(target) == {"source_ref", "source_hash"}
    with request_scope(owner_principal(surface="mcp")):
        manifest, _ = source_custody.read_manifest(vault, target["source_ref"])
    assert manifest.publication == "published"
    assert manifest.allocated_hash == target["source_hash"]
    assert any(memory_refs.ref_from_markdown(path.read_text()) == target["source_ref"]
               for path in (vault / "Knowledge Base/Sources").rglob("*.md"))
    assert manifest.coverage_complete is False


def test_failure_after_allocation_keeps_absent_target_and_gc_recovery_link(vault, attempts, registered_custody, monkeypatch):
    from exomem import add

    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)

    def fail_source_batch(*args, **kwargs):
        raise RuntimeError("parent batch unavailable")

    monkeypatch.setattr(add, "batch_atomic_write", fail_source_batch)
    with pytest.raises(RuntimeError, match="^parent batch unavailable$"):
        call("capture_source", **args, capture_reservation=ticket)
    settled = call("settle_source_capture", reservation=ticket)
    target = settled["owned_targets"][0]
    with request_scope(owner_principal(surface="mcp")):
        manifest, _ = source_custody.read_manifest(vault, target["source_ref"])
    assert not any(memory_refs.ref_from_markdown(path.read_text()) == target["source_ref"]
                   for path in (vault / "Knowledge Base/Sources").rglob("*.md"))
    assert settled["past_outcome"] == "unknown"
    assert settled["custody_state"] == "unknown"
    monkeypatch.setattr(manager.idempotency, "clock", lambda: 10**15)
    with manager.mutation_guard(vault) as boundary:
        assert manager.idempotency.capture_attempts.collect(boundary) == 0
    assert call("settle_source_capture", reservation=ticket)["owned_targets"][0]["source_ref"] == target["source_ref"]


def test_reused_source_is_observed_without_ownership(vault, attempts, registered_custody):
    _, call = attempts
    args = authored()
    first = reserve(call, args)
    captured = call("capture_source", **args, capture_reservation=first)
    second = reserve(call, args)
    assert call("capture_source", **args, capture_reservation=second)["capture_outcome"] == "reused"
    settled = call("settle_source_capture", reservation=second)
    assert settled["owned_targets"] == []
    assert (vault / captured["source"]["path"]).exists()


def test_capture_retains_canonical_copy_when_the_native_binding_disappears(vault, attempts, registered_custody):
    """An empty local table cannot turn a published Source payload into completed cleanup."""
    from exomem import generated_fragment_store, source_native_custody

    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    captured = call("capture_source", **args, capture_reservation=ticket)
    target = call("settle_source_capture", reservation=ticket)["owned_targets"][0]
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        native = source_native_custody.NativeCustody(vault)
        try:
            copies, cursor = [], None
            for _ in range(16):
                lookup = store.lookup(target["source_ref"], None, cursor=cursor)
                assert lookup.state == "unknown"
                copies.extend(lookup.copies)
                cursor = lookup.cursor
                if cursor is None:
                    break
            assert cursor is None
            whole = [copy for copy in copies
                     if store.image_header(copy.image_id).value.whole_source_ref == target["source_ref"]]
            assert len(whole) == 1
            copy = whole[0]
            assert copy.outcome == "present" and copy.origin_ledger == native.ledger_id
            assert native.observe(copy.owner.version, destination=True).matching is True
            image = store.image_header(copy.image_id).value
            assert copy.owner.locator == generated_fragment_store.file_locator(image.catalogue_id, captured["source"]["path"])
            assert copy.owner.version == native.get(copy.owner.version).binding_id
            assert image.whole_source_ref == target["source_ref"]
            assert image.image_sha256 == target["source_hash"]
            with manager.idempotency._connect() as conn:
                conn.execute("DELETE FROM source_native_bindings WHERE binding_id=?", (copy.owner.version,))
            assert native.lookup(source_ref=target["source_ref"]).reservations == ()
            assert native.observe(copy.owner.version, destination=True).state == "unknown"
            retained_copies, cursor = [], None
            for _ in range(16):
                retained = store.lookup(target["source_ref"], None, cursor=cursor)
                assert retained.state == "unknown"
                retained_copies.extend(retained.copies)
                cursor = retained.cursor
                if cursor is None:
                    break
            assert cursor is None and retained_copies == copies
            assert not copy.closed
        finally:
            store.close()
    assert (vault / captured["source"]["path"]).exists()


def test_sealed_binary_original_uses_whole_source_custody(vault, source_schema, attempts, registered_custody):
    """Opaque original bytes receive canonical copy obligations without a textual fragment map."""
    import hashlib
    import io

    from exomem import add, generated_fragment_store, source_native_custody

    manager, _ = attempts
    original = b"\x00\xffopaque original bytes"
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        saved = add.add(
            vault, source_schema, title="Attached record", content="", source_type="session",
            artifact=add.SourceArtifact(io.BytesIO(original), "record.bin", "application/octet-stream"),
            source_identity={"system": "archive", "record_id": "attached-record", "revision": "one"},
        )
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        native = source_native_custody.NativeCustody(vault)
        try:
            copies, cursor = [], None
            for _ in range(16):
                page = store.lookup(saved.ref, None, cursor=cursor)
                copies.extend(page.copies)
                cursor = page.cursor
                if cursor is None:
                    break
            assert cursor is None
            originals, whole = [], []
            for copy in copies:
                image = store.image_header(copy.image_id).value
                assert copy.outcome == "present"
                assert native.observe(copy.owner.version, destination=True).matching is True
                if image.whole_source_ref is not None:
                    assert image.whole_source_ref == saved.ref and image.page_count == 0
                    whole.append(copy)
                    if image.image_sha256 == hashlib.sha256(original).hexdigest():
                        originals.append(copy)
            assert len(whole) == 2
            assert len(originals) == 1
            assert (vault / saved.artifact_path).read_bytes() == original
        finally:
            store.close()


def test_capture_rollback_closes_its_exact_source_payload_copies(vault, attempts, registered_custody, monkeypatch):
    """Caught failure after installation removes the fresh Source and closes its retained copy obligations."""
    from exomem import generated_fragment_store, source_native_custody

    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)

    def fail_after_source(path):
        if path.suffix == ".md" and memory_refs.ref_from_markdown(path.read_text()):
            raise RuntimeError("Source installation interrupted")

    monkeypatch.setattr(vault_module, "_after_batch_destination_published", fail_after_source)
    with pytest.raises(RuntimeError, match="Source installation interrupted"):
        call("capture_source", **args, capture_reservation=ticket)
    target = call("settle_source_capture", reservation=ticket)["owned_targets"][0]
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        try:
            retained = store.lookup(target["source_ref"], None)
            assert retained.image_ids and retained.copies == ()
            assert source_native_custody.NativeCustody(vault).lookup(source_ref=target["source_ref"]).reservations == ()
        finally:
            store.close()
    assert not any(memory_refs.ref_from_markdown(path.read_text()) == target["source_ref"]
                   for path in (vault / "Knowledge Base/Sources").rglob("*.md"))


def test_protected_source_preparation_preserves_input_without_publishing(vault, source_schema, attempts):
    """Private text must stay a preparation, with its original input available for exact replay."""
    from exomem import add, vocabulary_gate

    class Prepared(Exception):
        def __init__(self, value):
            self.value = value

    def stop(value):
        raise Prepared(value)

    manager, _ = attempts
    before = {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    original = "Confidential text with café and exact line endings.\r\n"
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with memory_refs._allocated_ids() as identities, vocabulary_gate.preparation_context(vault):
            with pytest.raises(Prepared) as prepared:
                add.add(vault, source_schema, title="Private account", content=original,
                        source_type="session", raw_protection=True, _prepare=stop)
        snapshot = prepared.value.value
        assert snapshot["normalized_native_args"]["content"] == original
        assert {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()} == before
        with memory_refs._allocated_ids(identities):
            saved = add.add(vault, source_schema, **snapshot["normalized_native_args"], _allocation=snapshot["allocation"])
    assert saved.ref == snapshot["ref"]
    assert (vault / saved.artifact_path).read_bytes() == original.encode("utf-8")


def test_unreserved_capture_under_default_policy_stays_unenrolled(vault, attempts):
    _, call = attempts
    args = authored()
    assert call("capture_source", **args)["capture_outcome"] == "created"
    assert not source_custody.manifest_root(vault).exists()


def test_issued_capture_keeps_custody_when_erasure_policy_becomes_unavailable(vault, attempts, registered_custody, monkeypatch):
    _, call = attempts
    args = authored()
    ticket = reserve(call, args)
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", "{")
    captured = call("capture_source", **args, capture_reservation=ticket)
    settled = call("settle_source_capture", reservation=ticket)
    assert settled["custody_state"] == "complete"
    source = (vault / captured["source"]["path"]).read_text()
    assert settled["owned_targets"][0]["source_ref"] == memory_refs.ref_from_markdown(source)


def test_settlement_closes_and_reports_owned_allocation_without_canonical_readers(vault, attempts, registered_custody, monkeypatch):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)

    def lose_terminal(*args, **kwargs):
        raise RuntimeError("terminal unavailable")

    monkeypatch.setattr(manager.idempotency.capture_attempts, "complete", lose_terminal)
    with pytest.raises(writer_lease._PostCommitOutcomeUncertain):
        call("capture_source", **args, capture_reservation=ticket)
    with manager.idempotency._connect() as conn:
        allocation = json.loads(conn.execute("SELECT allocation FROM capture_attempt_custody WHERE ticket=?", (ticket["ticket"],)).fetchone()[0])
    capture_guard = vault_module.PathGuard.capture

    def unavailable_guard(cls, root, target, *args, **kwargs):
        if Path(target) in (Path(allocation["canonical_path"]), root / allocation["canonical_path"]):
            pytest.fail("settlement consulted the canonical Source")
        return capture_guard(root, target, *args, **kwargs)

    monkeypatch.setattr(vault_module.PathGuard, "capture", classmethod(unavailable_guard))
    monkeypatch.setattr(source_custody, "read_manifest", lambda *a, **k: pytest.fail("settlement consulted canonical custody"))
    settled = call("settle_source_capture", reservation=ticket)
    assert settled["admission_closed_here"] is True and settled["past_outcome"] == "unknown"
    assert settled["owned_targets"] == [{"source_ref": allocation["source_ref"], "source_hash": allocation["source_hash"]}]
    with manager.idempotency._connect() as conn:
        assert conn.execute("SELECT state FROM capture_attempts WHERE ticket=?", (ticket["ticket"],)).fetchone()[0] == "closed"


def test_missing_primary_cannot_clear_another_pending_publication(vault, attempts, registered_custody):
    manager, call = attempts
    args = authored()
    captured = call("capture_source", **args, capture_reservation=reserve(call, args))
    reference = memory_refs.ref_from_markdown((vault / captured["source"]["path"]).read_text())
    owner = source_custody.OwnerLocator(owner="semantic_writes", locator="retained-interpretations", version="one")
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        first = source_custody.reserve_publication(vault, reference=reference, producer=owner, primary=_primary(vault, "first"))
        second = source_custody.reserve_publication(vault, reference=reference, producer=owner, primary=_primary(vault, "second"))
        assert source_custody.observe_publication(vault, first) is False
        manifest, _ = source_custody.read_manifest(vault, reference)
    assert manifest.dependency.state == "uncertain"
    assert manifest.reservations[owner.owner].publication_id == second.publication_id


def test_exact_success_is_sticky_without_clearing_a_different_reservation(vault, attempts, registered_custody):
    manager, call = attempts
    args = authored()
    captured = call("capture_source", **args, capture_reservation=reserve(call, args))
    reference = memory_refs.ref_from_markdown((vault / captured["source"]["path"]).read_text())
    owner = source_custody.OwnerLocator(owner="semantic_writes", locator="retained-interpretations", version="one")
    primary = _primary(vault, "published")
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        first = source_custody.reserve_publication(vault, reference=reference, producer=owner, primary=primary)
        second = source_custody.reserve_publication(vault, reference=reference, producer=owner, primary=_primary(vault, "later"))
        primary.path.parent.mkdir(parents=True, exist_ok=True)
        primary.path.write_text(primary.content)
        assert source_custody.observe_publication(vault, first) is True
        assert source_custody.observe_publication(vault, second) is False
        manifest, _ = source_custody.read_manifest(vault, reference)
    assert manifest.dependency.state == "present"
    assert manifest.reservations[owner.owner].publication_id == second.publication_id
    assert manifest.reservations[owner.owner].state == "pending"


def test_legacy_primary_can_publish_while_its_exact_publication_proof_stays_uncertain(vault, attempts, registered_custody):
    manager, call = attempts
    args = authored()
    captured = call("capture_source", **args, capture_reservation=reserve(call, args))
    reference = memory_refs.ref_from_markdown((vault / captured["source"]["path"]).read_text())
    primary = _primary(vault, "legacy-primary")
    primary.content = "\n".join(line for line in primary.content.split("\n") if not line.startswith("exomem_id:"))
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with source_custody.producing(vault, (reference,), owner="semantic_writes", locator=primary.path.relative_to(vault).as_posix(),
                                      version=vault_module.content_hash(primary.content), primary=primary):
            vault_module.batch_atomic_write([primary], vault_root=vault)
        manifest, _ = source_custody.read_manifest(vault, reference)
    assert primary.path.exists()
    assert manifest.dependency.state == "uncertain"
    assert manifest.reservations["semantic_writes"].primary_ref is None
    assert manifest.reservations["semantic_writes"].state == "pending"


def _primary(root, name):
    identity = memory_refs.new_id()
    return vault_module.PlannedWrite(root / f"Knowledge Base/Notes/{name}.md",
                              f"---\ntype: insight\nexomem_id: {identity}\nsources: []\n---\n\n# Derived\n", create_only=True)


def test_metadata_owner_cannot_publish_source_body(vault, attempts, registered_custody):
    from exomem.cli_ops import OpError

    manager, _ = attempts
    target = vault / "Knowledge Base/Sources/forbidden.md"
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with pytest.raises(OpError, match="SOURCE_CUSTODY_TARGET_INVALID"):
            source_custody.publish(vault, source_custody.PlannedCustody((vault_module.PlannedWrite(target, "body"),)))
    assert not target.exists()


@pytest.mark.parametrize("invalid", ["wrong-reference", "extra-payload", "extra-directory"])
def test_metadata_scope_rejects_unbound_payload_before_service_authority(vault, attempts, registered_custody, monkeypatch, invalid):
    from exomem.cli_ops import OpError
    from exomem.governance import principal

    manager, _ = attempts
    reference = memory_refs.memory_ref(memory_refs.new_id())
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        write = source_custody.prepare_enrollment(vault, reference=reference, source_hash="1" * 64).writes[0]
        if invalid == "wrong-reference":
            other_ref = memory_refs.memory_ref(memory_refs.new_id())
            write.content = write.content.replace(reference, other_ref)
        elif invalid == "extra-payload":
            write.content = json.dumps({**json.loads(write.content), "unowned": "bytes"})
        else:
            write.ensure_directories = (vault / "unowned",)
        monkeypatch.setattr(principal, "owner_local_producer", lambda *a, **k: pytest.fail("invalid plan entered service authority"))
        with pytest.raises(OpError, match="SOURCE_CUSTODY_TARGET_INVALID"):
            source_custody.publish(vault, source_custody.PlannedCustody((write,)))
    assert not source_custody.manifest_path(vault, reference).exists()
    assert not (vault / "unowned").exists()


def test_auxiliary_custody_batch_leaves_parent_witness_for_its_native_batch(vault, attempts, registered_custody):
    manager, _ = attempts
    reference = memory_refs.memory_ref(memory_refs.new_id())
    witness_path = curation.CurationStore(vault).evidence_dir("cur-20261101-111111111111") / "publication.json"
    primary = _primary(vault, "witnessed-parent")
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with curation_witness.atomic_witness(vault, witness_path, '{"version":1}') as pending:
            source_custody.publish(vault, source_custody.prepare_enrollment(
                vault, reference=reference, source_hash="1" * 64,
            ))
            assert curation_witness.pending_for(vault) is pending
            assert not pending.consumed and not witness_path.exists()
            vault_module.batch_atomic_write([primary], vault_root=vault)
            assert pending.consumed and curation_witness.pending_for(vault) is None
    assert primary.path.exists() and witness_path.exists()


def test_restricted_semantic_commit_tracks_a_skipped_back_reference(vault, citing, registered_custody):
    source_before = _original(vault, READABLE_SOURCE)
    reference = memory_refs.ref_from_markdown(source_before.decode())
    manager = writer_lease.active_manager()
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        source_custody.publish(vault, source_custody.prepare_enrollment(
            vault, reference=reference, source_hash=vault_module.content_hash(source_before.decode()),
        ))
        assert source_custody.observe_capture(vault, reference, path=READABLE_SOURCE)
    result = _remember(vault, citing, "Derived from a readable original", sources=[READABLE_SOURCE.removesuffix(".md")])
    assert "__error__" not in result, result
    assert any("back-ref skipped" in warning for warning in result["warnings"])
    assert (vault / READABLE_SOURCE).read_bytes() == source_before
    with request_scope(owner_principal(surface="mcp")):
        manifest, _ = source_custody.read_manifest(vault, reference)
    assert manifest.dependency.state == "present"
    assert manifest.coverage_complete is False


def test_semantic_origin_without_sources_field_reserves_its_exact_input_root(vault, registered_custody):
    from exomem import note, provenance

    source = _original(vault, READABLE_SOURCE).decode()
    reference = memory_refs.ref_from_markdown(source)
    manager = writer_lease.active_manager()
    origin = provenance.encode_origin({
        "inputs": {"original": {"reference": reference, "version": provenance.evidence_version(source)}},
        "assessments": [],
        "bindings": [{"inputs": ["original"], "scope": {"kind": "unit", "unit_ref": "#claim"}}],
    }, authoring=True)
    with request_scope(owner_principal(surface="mcp")):
        with manager.mutation_guard(vault):
            source_custody.publish(vault, source_custody.prepare_enrollment(
                vault, reference=reference, source_hash=vault_module.content_hash(source),
            ))
            source_custody.observe_capture(vault, reference, path=READABLE_SOURCE)
        arguments = {"title": "Origin-bound conclusion", "note_type": "insight", "status": "active",
                     "content": origin + "\n\n## Observations\n\n- [finding] Exact conclusion ^claim\n"}
        prepared = note.note(vault, **arguments, validate_only=True)
        committed = note.note(vault, **arguments, draft_id=prepared.draft_id, draft_token=prepared.draft_token,
                              draft_hash=prepared.draft_hash, relation_disposition="reviewed_none",
                              relation_review_hash=prepared.draft_hash, relation_review_reason="No typed relation is established.")
        manifest, _ = source_custody.read_manifest(vault, reference)
    assert (vault / committed.path).exists()
    assert manifest.dependency.state == "present"
    assert manifest.reservations["semantic_writes"].source_ref == reference


def test_retirement_fence_rejects_a_prepared_source_closure(vault, attempts, registered_custody):
    from exomem.cli_ops import OpError

    manager, call = attempts
    args = authored()
    captured = call("capture_source", **args, capture_reservation=reserve(call, args))
    path = captured["source"]["path"]
    reference = memory_refs.ref_from_markdown((vault / path).read_text())
    primary = _primary(vault, "delayed")
    primary.content = primary.content.replace("sources: []", f"sources: ['{reference}']")
    with request_scope(owner_principal(surface="mcp")):
        prepared = source_closure.prepare_source_closure(vault, primary.content, destination=primary.path.relative_to(vault).as_posix())
        with manager.mutation_guard(vault):
            manifest, guard = source_custody.read_manifest(vault, reference)
            retired = manifest.model_copy(update={"retirement": source_custody.RetirementFence(operation_id="withdrawal", source_hash=manifest.allocated_hash)})
            source_custody.publish(vault, source_custody.PlannedCustody((source_custody._write(
                vault, source_custody.manifest_path(vault, reference), retired, guard, source_custody.MAX_MANIFEST_BYTES,
            ),)))
            with pytest.raises(OpError, match="SOURCE_RETIRED"):
                source_closure.enforce_source_closure(vault, primary.content, destination=primary.path.relative_to(vault).as_posix(), prepared=prepared)


def test_retirement_fence_rejects_prepared_origin_inputs_while_source_bytes_remain(vault, attempts, registered_custody):
    """An origin-only record must not commit after retirement while canonical deletion is pending."""
    from exomem import origin_bindings, provenance

    manager, _ = attempts
    source = _original(vault, READABLE_SOURCE).decode()
    reference = memory_refs.ref_from_markdown(source)
    origin = provenance.encode_origin({
        "inputs": {"original": {"reference": reference, "version": provenance.evidence_version(source)}},
        "assessments": [],
        "bindings": [{"inputs": ["original"], "scope": {"kind": "unit", "unit_ref": "#claim"}}],
    }, authoring=True)
    with request_scope(owner_principal(surface="mcp")):
        prepared = origin_bindings.prepare_origin_inputs(
            vault, origin + "\n\n## Observations\n\n- [finding] Exact conclusion ^claim\n",
        )
        assert prepared is not None
        prepared.revalidate(vault)
        with manager.mutation_guard(vault):
            source_custody.publish(vault, source_custody.prepare_enrollment(
                vault, reference=reference, source_hash=vault_module.content_hash(source),
            ))
            manifest, guard = source_custody.read_manifest(vault, reference)
            retired = manifest.model_copy(update={"retirement": source_custody.RetirementFence(
                operation_id="withdrawal", source_hash=manifest.allocated_hash,
            )})
            source_custody.publish(vault, source_custody.PlannedCustody((source_custody._write(
                vault, source_custody.manifest_path(vault, reference), retired, guard,
                source_custody.MAX_MANIFEST_BYTES,
            ),)))
            assert (vault / READABLE_SOURCE).read_text() == source
            with pytest.raises(provenance.OriginError, match="SOURCE_RETIRED"):
                prepared.revalidate(vault)


def test_pre_reference_residue_survives_settlement_and_gc_until_owner_ack(vault, attempts, registered_custody, monkeypatch, tmp_path):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    original = tmp_path / "owned-original.bin"
    owner = source_custody.OwnerLocator(owner="original-stage", locator="owned-stage", version="one")

    def fail_before_allocation(*args, **kwargs):
        capture_attempts.reserve_residue(vault, owner)
        original.write_bytes(b"preserved original bytes")
        raise RuntimeError("allocation unavailable")

    monkeypatch.setattr(commands, "_add_source", fail_before_allocation)
    with pytest.raises(RuntimeError, match="allocation unavailable"):
        call("capture_source", **args, capture_reservation=ticket)
    settled = call("settle_source_capture", reservation=ticket)
    assert settled["owned_targets"] == [] and settled["attempt_residue"]["state"] == "pending"
    assert original.exists()
    monkeypatch.setattr(manager.idempotency, "clock", lambda: 10**15)
    withdrawn = call("settle_source_capture", reservation=ticket, retire_owned=True)
    assert withdrawn["attempt_residue"]["state"] == "pending" and original.exists()
    assert withdrawn["attempt_residue"]["retirement_requested"] is True
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        assert manager.idempotency.capture_attempts.collect(manager._mutation_coordinator_for(vault)) == 0
        # This test owner acknowledges only after its own bytes were removed.
        original.unlink()
        capture_attempts.acknowledge_residue(vault, ticket, owner)
        assert manager.idempotency.capture_attempts.collect(manager._mutation_coordinator_for(vault)) == 1


def test_restart_retains_allocated_target_without_reexecuting_lost_capture(vault, source_schema, attempts, registered_custody, monkeypatch):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    def lose_terminal(*args, **kwargs):
        raise RuntimeError("terminal unavailable")

    monkeypatch.setattr(manager.idempotency.capture_attempts, "complete", lose_terminal)
    with pytest.raises(writer_lease._PostCommitOutcomeUncertain):
        call("capture_source", **args, capture_reservation=ticket)
    restarted = writer_lease.LeaseManager(manager.config)
    monkeypatch.setattr(commands, "_add_source", lambda *a, **k: pytest.fail("lost capture executed again"))
    with request_scope(owner_principal(surface="mcp")):
        settled = dispatch(restarted, vault, "settle_source_capture", {"reservation": ticket})
        assert settled["past_outcome"] == "unknown"
        with restarted.idempotency._connect() as conn:
            allocation = json.loads(conn.execute("SELECT allocation FROM capture_attempt_custody WHERE ticket=?", (ticket["ticket"],)).fetchone()[0])
        assert settled["owned_targets"] == [{"source_ref": allocation["source_ref"], "source_hash": allocation["source_hash"]}]
        assert (vault / allocation["canonical_path"]).exists()
        with pytest.raises(ValueError, match="CAPTURE_ATTEMPT_CLOSED"):
            dispatch(restarted, vault, "capture_source", {**args, "capture_reservation": ticket}, source_schema)


def test_nested_capture_gc_preserves_outer_residue_and_its_active_admission(vault, attempts, registered_custody, monkeypatch):
    manager, call = attempts
    outer_args = authored()
    inner_args = {**outer_args, "source_identity": {**outer_args["source_identity"], "record_id": "inner"}}
    outer, inner = reserve(call, outer_args), reserve(call, inner_args)
    owner = source_custody.OwnerLocator(owner="original-stage", locator="outer-original", version="one")
    native_add = commands._add_source

    def nested(*args, **kwargs):
        if kwargs["source_identity"]["record_id"] == "survey":
            capture_attempts.reserve_residue(vault, owner)
            call("capture_source", **inner_args, capture_reservation=inner)
        else:
            assert call("settle_source_capture", reservation=outer)["state"] == "pending"
            assert manager.idempotency.capture_attempts.collect(manager._mutation_coordinator_for(vault)) == 0
        return native_add(*args, **kwargs)

    monkeypatch.setattr(commands, "_add_source", nested)
    call("capture_source", **outer_args, capture_reservation=outer)
    settled = call("settle_source_capture", reservation=outer)
    assert settled["past_outcome"] == "created"
    assert settled["attempt_residue"]["owners"] == [owner.model_dump()]
    assert settled["custody_state"] == "pending"


def test_collection_work_does_not_grow_with_100k_pinned_predecessors(vault, attempts, registered_custody, monkeypatch, request):
    from contextlib import contextmanager

    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    call("capture_source", **args, capture_reservation=ticket)
    with manager.idempotency._connect() as conn:
        template = conn.execute("SELECT * FROM capture_attempts WHERE ticket=?", (ticket["ticket"],)).fetchone()
    monkeypatch.setattr(manager.idempotency, "clock", lambda: 10**15)
    connect = manager.idempotency._connect
    vm_steps = []

    @contextmanager
    def measured_connection():
        with connect() as conn:
            steps = []
            conn.set_progress_handler(lambda: steps.append(None) or 0, 1)
            try:
                yield conn
            finally:
                conn.set_progress_handler(None, 0)
                vm_steps.append(len(steps))

    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault) as boundary:
        with monkeypatch.context() as measured:
            measured.setattr(manager.idempotency, "_connect", measured_connection)
            assert manager.idempotency.capture_attempts.collect(boundary) == 1
        baseline_steps = sum(vm_steps)
        pinned_count = 100_000
        with connect() as conn:
            conn.executemany(
                "INSERT INTO capture_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ((f"pinned-{index}", *template[1:4], "closed", *template[5:7], 1.0, "unknown", None, 1)
                 for index in range(pinned_count)),
            )
            conn.executemany("INSERT INTO capture_attempt_residue VALUES (?,'original-stage','retained-original','one','pending')",
                             ((f"pinned-{index}",) for index in range(pinned_count)))
            conn.execute("INSERT INTO capture_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("eligible-later", *template[1:]))
        vm_steps.clear()
        with monkeypatch.context() as measured:
            measured.setattr(manager.idempotency, "_connect", measured_connection)
            assert manager.idempotency.capture_attempts.collect(boundary) == 1
        pinned_steps = sum(vm_steps)
        assert pinned_steps <= baseline_steps + 100
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM capture_attempts").fetchone()[0] == pinned_count
        assert conn.execute("SELECT count(*) FROM capture_attempt_residue").fetchone()[0] == pinned_count
    request.node.user_properties.extend((("pinned_predecessors", pinned_count),
                                         ("baseline_sqlite_vm_steps", baseline_steps),
                                         ("pinned_sqlite_vm_steps", pinned_steps)))


def test_earlier_candidate_without_custody_eligibility_is_not_guessed(attempts):
    from exomem.cli_ops import OpError

    manager, _ = attempts
    with manager.idempotency._connect() as conn:
        conn.execute("DROP TABLE IF EXISTS capture_attempts")
        conn.execute("CREATE TABLE capture_attempts (ticket TEXT PRIMARY KEY, retention_seconds REAL NOT NULL)")
    with pytest.raises(OpError, match="CAPTURE_ATTEMPT_LEDGER_UNAVAILABLE") as caught:
        capture_attempts.CaptureAttemptLedger(manager.idempotency)
    assert "custody eligibility" in caught.value.message


def test_enrolled_capture_stays_off_shared_pages_and_unenrolled_capture_pays_no_page_custody(vault, attempts, registered_custody):
    """An enrolled Source leaves no copy on the shared pages, so a later unenrolled write there needs no custody."""
    from exomem import generated_fragment_store

    manager, call = attempts
    pages = [vault / "Knowledge Base" / relative for relative in ("Sources/index.md", "index.md", "log.md")]
    before = {path: path.read_bytes() for path in pages}
    enrolled = {**authored(), "title": "Retained café 🧭"}
    ticket = reserve(call, enrolled)
    assert call("capture_source", **enrolled, capture_reservation=ticket)["capture_outcome"] == "created"
    assert call("settle_source_capture", reservation=ticket)["owned_targets"]
    assert {path: path.read_bytes() for path in pages} == before
    later = {**authored(), "title": "Unenrolled later survey", "content": "A different observation.",
             "source_identity": {"system": "unlisted", "record_id": "later", "revision": "one"}}
    captured = call("capture_source", **later)
    assert captured["capture_outcome"] == "created"
    for path in pages:
        assert Path(captured["source"]["path"]).stem in path.read_text(), path
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        try:
            assert store.path.exists() and not store.holds_slots()
        finally:
            store.close()


def _forge_earlier_listing(vault, manager, reference: str, source_path: str) -> None:
    """Write what builds before this exclusion left: the enrolled Source listed on shared pages, its lines owned."""
    import hashlib

    from exomem import (
        generated_fragment_store,
        generated_fragments,
        generated_output_plan,
        indexes,
        source_native_custody,
    )

    kb = vault / "Knowledge Base"
    pages = (kb / "Sources/index.md", kb / "index.md", kb / "log.md")
    listed = Path(source_path).with_suffix("").as_posix()
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        try:
            producer = generated_output_plan.GeneratedProducer(store, source_native_custody.NativeCustody(vault))
            update = indexes.compute_updates(vault, source_type="session", folder_title="Sessions",
                folder_description="captured sessions", rel_source_path=listed, date_iso="2026-11-15",
                activity_summary=f"Captured {Path(listed).name}", log_entry_body=f"Captured {Path(listed).name}.",
                before_fragment_maps=producer.before_maps(pages))
            writes = []
            for path, content, guard in zip(pages,
                    (update.sources_index_content, update.top_index_content, update.log_content),
                    (update.sources_index_guard, update.top_index_guard, update.log_guard), strict=True):
                image = generated_fragments._Image.before(content, update.fragment_plans[path.relative_to(vault).as_posix()],
                                                          hashlib.sha256(content.encode()).hexdigest())
                start = content.rindex("\n", 0, content.index(Path(listed).name)) + 1
                end = content.index("\n", start) + 1
                image.splice(start, end, image.slice(start, end).own(generated_fragments.SourceRef(reference)))
                write = vault_module.PlannedWrite(path=path, content=content, guard=guard)
                writes.append(producer.attach(write, image.plan(update.fragment_plans[path.relative_to(vault).as_posix()].before_sha256)))
            vault_module.batch_atomic_write(writes, vault_root=vault, post_commit_fanout=False)
        finally:
            store.close()
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with generated_output_plan.producer(vault) as producer:
            assert all(reference in {item.source_ref for item in producer.before(path).mapping.after.fragments}
                       for path in pages)


def test_fresh_default_off_capture_does_not_initialize_a_generated_catalogue(vault, attempts, monkeypatch):
    from exomem import generated_fragment_store

    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":[]}')
    manager, call = attempts
    captured = call("capture_source", **authored())
    assert captured["capture_outcome"] == "created"
    assert not generated_fragment_store.GeneratedFragmentStore(vault).path.exists()
    reference = memory_refs.ref_from_markdown((vault / captured["source"]["path"]).read_text())
    with request_scope(owner_principal(surface="mcp")):
        manifest, _ = source_custody.read_manifest(vault, reference)
    assert manifest is None


def test_tracked_log_rotation_and_retry_keep_the_native_archive_map(vault, attempts, registered_custody, monkeypatch):
    from exomem import generated_output_plan, source_native_custody

    manager, call = attempts
    args = {**authored(), "title": "Archived café 🧭"}
    ticket = reserve(call, args)
    captured = call("capture_source", **args, capture_reservation=ticket)
    reference = call("settle_source_capture", reservation=ticket)["owned_targets"][0]["source_ref"]
    _forge_earlier_listing(vault, manager, reference, captured["source"]["path"])
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        padding = [vault_module.LogEntry("2026-11-15", "note", f"Notes/independent-{number}", "An independent finding.")
                   for number in range(vault_module.LOG_ROTATE_KEEP_ENTRIES)]
        with generated_output_plan.producer(vault) as producer:
            setup = vault_module.plan_log_entries(vault, padding, operation_token="padding", generated_producer=producer)
            vault_module.batch_atomic_write(setup.writes, vault_root=vault, post_commit_fanout=False)
        monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1")
        with generated_output_plan.producer(vault) as producer:
            def no_derived_cache():
                pytest.fail("log planning consulted the derived catalogue")

            monkeypatch.setattr(producer.store, "_connect", no_derived_cache)
            plan = vault_module.plan_log_writes(vault, date_iso="2026-11-15", op="note", rel_path_no_ext="Notes/latest",
                body="The latest independent finding.", operation_token="rotation", generated_producer=producer)
            assert plan.rotation_note is not None
            archive, live = plan.writes
            assert {item.source_ref for item in archive.generated_image.after.after.fragments} == {reference}
            assert live.generated_image.after.after.fragments == ()
            archive_map = archive.generated_image.after.after
            vault_module.batch_atomic_write(plan.writes, vault_root=vault, post_commit_fanout=False)
        before_retry = {write.path: write.path.read_bytes() for write in plan.writes}
        with generated_output_plan.producer(vault) as producer:
            retry = vault_module.plan_log_writes(vault, date_iso="2026-11-15", op="note", rel_path_no_ext="Notes/latest",
                body="The latest independent finding.", operation_token="rotation", generated_producer=producer)
            assert retry.rotation_note is None
            assert retry.writes[0].path == archive.path
            assert retry.writes[0].generated_image.after.after == archive_map
            vault_module.batch_atomic_write(retry.writes, vault_root=vault, post_commit_fanout=False)
        assert {write.path: write.path.read_bytes() for write in retry.writes} == before_retry
        with generated_output_plan.producer(vault) as producer:
            current = producer.before(archive.path)
            assert current.mapping.after == archive_map and current.current is not None
            native = source_native_custody.NativeCustody(vault)
            assert native.observe(current.current.native.binding_id, destination=True).matching is True


def test_note_preserve_and_edit_keep_capture_ownership_after_activity_trim_and_log_rotation(
    vault, attempts, registered_custody, monkeypatch,
):
    from exomem import generated_output_plan, indexes

    manager, call = attempts
    first = {**authored(), "title": "Retained café 🧭"}
    ticket = reserve(call, first)
    captured = call("capture_source", **first, capture_reservation=ticket)
    reference = call("settle_source_capture", reservation=ticket)["owned_targets"][0]["source_ref"]
    _forge_earlier_listing(vault, manager, reference, captured["source"]["path"])
    later = {**authored(), "title": "Later unenrolled survey", "content": "Another observation.",
             "source_identity": {"system": "unlisted", "record_id": "later", "revision": "one"}}
    call("capture_source", **later)
    anchor = vault / "Knowledge Base/Notes/Insights/anchor.md"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text("---\ntype: insight\nstatus: active\n---\n# Anchor\n\n- [finding] A separate finding.\n")
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with generated_output_plan.producer(vault) as producer:
            top, _ = indexes.plan_recent_activity(vault,
                (("2026-11-15", f"Independent activity {number}") for number in range(indexes.RECENT_ACTIVITY_CAP - 2)),
                generated_producer=producer)
            logs = vault_module.plan_log_entries(vault,
                (vault_module.LogEntry("2026-11-15", "note", f"Notes/padding-{number}", "An independent finding.")
                 for number in range(vault_module.LOG_ROTATE_KEEP_ENTRIES)),
                operation_token="workflow-padding", generated_producer=producer)
            vault_module.batch_atomic_write([top, *logs.writes], vault_root=vault, post_commit_fanout=False)
    monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1")
    with request_scope(owner_principal(surface="mcp")):
        note = dispatch(manager, vault, "remember", {"title": "Independent note", "note_type": "insight",
            "content": "## Observations\n\n- [finding] A separate independent fact. ^independent\n\n## Relations\n- supports [[Knowledge Base/Notes/Insights/anchor]]\n"})
        dispatch(manager, vault, "preserve_evidence", {"scope": "References", "category": "snapshots", "filename": "independent.txt",
            "content": "A separately preserved observation."})
        dispatch(manager, vault, "edit_memory", {"path": note["path"], "why": "Clarify the independent finding.",
            "operation": {"kind": "replace_string", "old_string": "separate independent fact", "new_string": "clear independent fact"}})
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with generated_output_plan.producer(vault) as producer:
            top = producer.before(vault / "Knowledge Base/index.md")
            assert top.current is not None
            assert reference not in {item.source_ref for item in top.mapping.after.fragments}
            sources = producer.before(vault / "Knowledge Base/Sources/index.md")
            assert sources.current is not None
            assert reference in {item.source_ref for item in sources.mapping.after.fragments}
            archives = list((vault / "Knowledge Base/_archive/logs").glob("log-*.md"))
            assert archives
            retained = []
            for path in archives:
                current = producer.before(path)
                assert current.current is not None and current.binding.slot.pending is None
                retained.extend(item for item in current.mapping.after.fragments if item.source_ref == reference)
            assert retained
    assert "clear independent fact" in (vault / note["path"]).read_text()


def test_public_append_preserves_captured_source_log_map(vault, attempts, registered_custody):
    from exomem import generated_output_plan

    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    captured = call("capture_source", **args, capture_reservation=ticket)
    reference = call("settle_source_capture", reservation=ticket)["owned_targets"][0]["source_ref"]
    _forge_earlier_listing(vault, manager, reference, captured["source"]["path"])
    log = vault / "Knowledge Base/log.md"
    target = vault / "Knowledge Base/Evidence/sidecar.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("Independent evidence sidecar.\n")
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with generated_output_plan.producer(vault) as producer:
            before = producer.before(log)
            assert before.current is not None
            assert reference in {fragment.source_ref for fragment in before.mapping.after.fragments}
        dispatch(manager, vault, "manage_memory_file", {"operation": "append",
            "path": target.relative_to(vault).as_posix(), "content": "A separate sidecar update."})
        assert "A separate sidecar update." in target.read_text()
        with generated_output_plan.producer(vault) as producer:
            after = producer.before(log)
            assert after.current is not None
            assert reference in {fragment.source_ref for fragment in after.mapping.after.fragments}


@pytest.mark.parametrize("lost_descriptor", (False, True))
def test_public_create_recovers_pending_custody_without_mutating_preview(vault, attempts, registered_custody, monkeypatch, lost_descriptor):
    import sqlite3

    from exomem import generated_output_plan, native_copy_custody, source_native_custody

    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    captured = call("capture_source", **args, capture_reservation=ticket)
    reference = call("settle_source_capture", reservation=ticket)["owned_targets"][0]["source_ref"]
    _forge_earlier_listing(vault, manager, reference, captured["source"]["path"])
    log = vault / "Knowledge Base/log.md"
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with generated_output_plan.producer(vault) as producer:
            before = producer.before(log)
            assert before.current is not None
            store = producer.store
            native = source_native_custody.NativeCustody(vault)
            copies = native_copy_custody.NativeCopyCustody(vault, store=store, native=native)
            header = store.image_header(before.current.canonical.image_id).value
            set_head = store._set_head

            def lose_after_pending(expected, value):
                result = set_head(expected, value)
                if value.pending is not None:
                    raise RuntimeError("interruption after canonical pending head")
                return result

            with monkeypatch.context() as crashing:
                crashing.setattr(store, "_set_head", lose_after_pending)
                with pytest.raises(RuntimeError, match="interruption"):
                    copies.prepare(header, logical_path=log)
            assert store.head().pending is not None
            interrupted_epoch = store.head().epoch
            if lost_descriptor:
                (store.path / "changes" / f"{store.head().epoch}.json").unlink()

                def unavailable_cache(_store):
                    raise sqlite3.DatabaseError("derived cache unavailable")

                monkeypatch.setattr(type(store), "_connect", unavailable_cache)
            canonical = {path: path.read_bytes() for path in store.path.rglob("*.json")}
            proposal = {"operation": "create", "path": "Knowledge Base/after-interruption.txt", "content": "Independent evidence."}
            preview = {"operation": "create", "path": "Knowledge Base/Notes/Insights/proposed.md",
                "content": "# Proposed\n\n## Observations\n\n- [constraint] Keep recovery bounded.\n",
                "frontmatter": {"type": "insight", "title": "Proposed", "status": "active"}, "validate_only": True}
            dispatch(manager, vault, "manage_memory_file", preview)
            assert not (vault / preview["path"]).exists()
            assert {path: path.read_bytes() for path in store.path.rglob("*.json")} == canonical
            dispatch(manager, vault, "manage_memory_file", proposal)
            assert (vault / proposal["path"]).exists()
            assert store.head().pending is None
        with generated_output_plan.producer(vault) as producer:
            after = producer.before(log)
            assert after.current is not None
            assert reference in {fragment.source_ref for fragment in after.mapping.after.fragments}
            assert {(fragment.opaque_fragment_id, fragment.source_ref, fragment.payload_sha256)
                    for fragment in after.mapping.after.fragments} == {
                (fragment.opaque_fragment_id, fragment.source_ref, fragment.payload_sha256)
                for fragment in before.mapping.after.fragments}
            assert after.mapping.unknown_through_epoch >= interrupted_epoch


def test_planning_preparation_keeps_generated_custody_unchanged(vault, attempts, registered_custody):
    from test_planning_mutation import _manifest

    from exomem import generated_output_plan, planning

    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    captured = call("capture_source", **args, capture_reservation=ticket)
    reference = call("settle_source_capture", reservation=ticket)["owned_targets"][0]["source_ref"]
    _forge_earlier_listing(vault, manager, reference, captured["source"]["path"])
    collection = "Knowledge Base/Planning/Work/_collection.md"
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        planning.create_collection(vault, collection, _manifest(), why="Retain intended work.")
        current = planning.inspect(vault, collection)
        with generated_output_plan.producer(vault) as producer:
            before = producer.before(vault / "Knowledge Base/log.md")
            assert reference in {fragment.source_ref for fragment in before.mapping.after.fragments}
            canonical = {path: path.read_bytes() for path in producer.store.path.rglob("*.json")}
            prepared = planning.prepare(vault, "add", {"collection": collection,
                "plan_id": "991acdd4-16b9-4396-8220-2cb37b7e8516", "item": {"title": "Review the account"},
                "why": "Retain intended work.", "expected_container_hash": current["snapshot"]})
            assert not (vault / prepared["path"]).exists()
            assert {path: path.read_bytes() for path in producer.store.path.rglob("*.json")} == canonical
            after = generated_output_plan.read_before(producer.store, producer.native, path=before.path,
                text=before.path.read_bytes().decode(), guard=before.guard)
            assert after == before


def test_lost_acknowledgement_phase_keeps_the_capture_and_the_next_write_settles_its_slots(
    vault, source_schema, attempts, registered_custody, monkeypatch,
):
    """Catalogue work after native bytes is one deferred phase; losing it must not strand published output."""
    from exomem import generated_fragment_store, generated_output_plan

    manager, call = attempts
    store_type = generated_fragment_store.GeneratedFragmentStore
    commit = store_type._commit
    lost = []

    def lose_the_phase_that_settles_slots(self, operation, values):
        if not lost and operation == "unit" and any(
                item.kind == "slot" and value.pending is None and value.current_copy_id is not None
                for item, value in values):
            lost.extend(values)
            raise RuntimeError("process lost before its acknowledgements became durable")
        return commit(self, operation, values)

    args = authored()
    ticket = reserve(call, args)
    enrolled = call("capture_source", **args, capture_reservation=ticket)
    reference = call("settle_source_capture", reservation=ticket)["owned_targets"][0]["source_ref"]
    _forge_earlier_listing(vault, manager, reference, enrolled["source"]["path"])
    unenrolled = {**authored(), "title": "Unenrolled survey", "content": "Another observation.",
                  "source_identity": {"system": "unlisted", "record_id": "unenrolled", "revision": "one"}}
    with monkeypatch.context() as crashing:
        crashing.setattr(store_type, "_commit", lose_the_phase_that_settles_slots)
        captured = call("capture_source", **unenrolled)
    assert lost and captured["capture_outcome"] == "created"
    assert (vault / captured["source"]["path"]).exists()
    log = vault / "Knowledge Base/log.md"
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        store = store_type(vault)
        try:
            acknowledged = [item.object_id for item, value in lost if item.kind == "copy" and value.outcome == "present"]
            assert acknowledged
            assert {store.copy_record(copy_id).value.outcome for copy_id in acknowledged} == {"reserved"}
        finally:
            store.close()
        with generated_output_plan.producer(vault) as producer:
            assert producer.before(log).binding.slot.pending is not None

    later = {**authored(), "title": "Later survey", "content": "A later observation.",
             "source_identity": {"system": "unlisted", "record_id": "later", "revision": "one"}}
    with request_scope(owner_principal(surface="mcp")):
        assert dispatch(manager, vault, "capture_source", later, source_schema)["capture_outcome"] == "created"
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        with generated_output_plan.producer(vault) as producer:
            settled = producer.before(log)
            assert settled.current is not None and settled.binding.slot.pending is None
            assert reference in {fragment.source_ref for fragment in settled.mapping.after.fragments}
            census = producer.store.census(limit=64)
            for _ in range(64):
                if census.state != "pending":
                    break
                census = producer.store.census(limit=64)
            assert census.state == "ready"
