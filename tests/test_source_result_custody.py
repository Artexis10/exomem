"""Private SQLite publication witnesses select ownership independently of equal bytes."""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811
from __future__ import annotations

import hashlib
import pickle

import pytest
from test_capture_attempts import attempts, authored, reserve  # noqa: F401

from exomem import source_custody
from exomem import source_result_attribution as attribution
from exomem import source_result_custody as results
from exomem.governance.principal import owner_principal, request_scope

A = "exomem://memory/00000000-0000-4000-8000-000000000001"
B = "exomem://memory/00000000-0000-4000-8000-000000000002"


@pytest.fixture
def result_owner(vault, attempts):
    manager, _ = attempts
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        for reference in (A, B):
            source_custody.publish(vault, source_custody.prepare_enrollment(
                vault, reference=reference, source_hash="a" * 64))
        journal = results.Journal(manager.idempotency)
        owner = results.ResultCustody(vault, journal=journal)
        try:
            yield owner
        finally:
            owner.close()


def _claim(owner, *, attempt="first"):
    pointer = results.Pointer(kind="mutation", key="explicit:opaque", digest="digest",
                              attempt_id=attempt, commit_token=f"token-{attempt}")
    with owner.journal.store._connect() as conn:
        conn.execute("INSERT INTO mutations(key,digest,state,updated_at,attempt_id,commit_token) "
                     "VALUES (?,?,'executing',0,?,?)", (pointer.key, pointer.digest, pointer.attempt_id, pointer.commit_token))
    return pointer


def _recipe(reference):
    return attribution.Recipe(contributions=(attribution.Contribution(
        path=("path",), source_refs=(reference,), action="omit"),))


def _publish(owner, pointer, reference, *, prior=None, payload=b"identical result"):
    def write(conn, raw):
        return conn.execute("UPDATE mutations SET result=?,state='completed' WHERE key=? "
                            "AND digest=? AND attempt_id=? AND commit_token=?",
                            (raw, pointer.key, pointer.digest, pointer.attempt_id, pointer.commit_token)).rowcount
    return owner.publish(pointer, payload, recipe=_recipe(reference), encode=lambda value: value,
                         write=write, prior_publication_id=prior)


def test_equal_bytes_restart_selects_exact_publication_and_retains_displaced_closure(result_owner, monkeypatch):
    owner = result_owner
    pointer = _claim(owner)
    first = _publish(owner, pointer, A)

    def lose_acknowledgment(binding):
        raise SystemExit("lost after atomic SQLite publication")

    monkeypatch.setattr(owner, "after_sql_publication", lose_acknowledgment)
    with pytest.raises(SystemExit, match="lost after atomic"):
        _publish(owner, pointer, B, prior=first.binding.publication_id)
    restarted = results.ResultCustody(owner.root, journal=results.Journal(owner.journal.store))
    try:
        with owner.journal.store._connect() as conn:
            conn.execute("BEGIN")
            payload = conn.execute("SELECT result FROM mutations WHERE key=?", (pointer.key,)).fetchone()[0]
            selected = restarted.journal.selected(conn, pointer.kind, pointer.key)
            displaced = restarted.journal.read(conn, first.binding.publication_id)
        assert hashlib.sha256(payload).hexdigest() == selected.payload_sha256 == first.binding.payload_sha256
        assert selected.recipe.sources == (B,)
        assert displaced.state == "closed" and displaced.recipe is None and displaced.closure == "retired"
        new_copy = restarted.catalogue.copy_record(selected.copy_id).value
        assert new_copy.outcome == "reserved"
        assert restarted.acknowledge(first.copy) == "retired"
        assert restarted.acknowledge(new_copy) == "present"
        with owner.journal.store._connect() as conn:
            assert restarted.journal.read(conn, first.binding.publication_id) is None
    finally:
        restarted.close()


def test_one_sql_snapshot_cannot_pair_old_bytes_with_new_ownership(result_owner):
    owner = result_owner
    pointer = _claim(owner)
    first = _publish(owner, pointer, A)
    with owner.journal.store._connect() as reader:
        reader.execute("BEGIN")
        old_payload = reader.execute("SELECT result FROM mutations WHERE key=?", (pointer.key,)).fetchone()[0]
        second = _publish(owner, pointer, B, prior=first.binding.publication_id)
        old_selection = owner.journal.selected(reader, pointer.kind, pointer.key)
        assert old_payload == second.value
        assert old_selection.publication_id == first.binding.publication_id
        assert old_selection.recipe.sources == (A,)
    with owner.journal.store._connect() as reader:
        assert owner.journal.selected(reader, pointer.kind, pointer.key).recipe.sources == (B,)


def test_expiry_key_reuse_retains_closure_and_rejects_the_old_acknowledgment(result_owner):
    owner = result_owner
    pointer = _claim(owner)
    first = _publish(owner, pointer, A)
    with owner.journal.store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        owner.journal.close_rows(conn, kind="mutation", keys=(pointer.key,))
        conn.execute("DELETE FROM mutations WHERE key=?", (pointer.key,))
    replacement = _claim(owner, attempt="replacement")
    second = _publish(owner, replacement, B, payload=b"new result")
    with pytest.raises(results.PublicationChanged, match="publication right changed"):
        _publish(owner, pointer, A, prior=first.binding.publication_id, payload=b"stale acknowledgment")
    with owner.journal.store._connect() as conn:
        row = conn.execute("SELECT result,attempt_id FROM mutations WHERE key=?", (pointer.key,)).fetchone()
        old_closure = owner.journal.read(conn, first.binding.publication_id)
        selected = owner.journal.selected(conn, replacement.kind, replacement.key)
    assert row == (b"new result", "replacement")
    assert selected.publication_id == second.binding.publication_id
    assert old_closure.state == "closed" and old_closure.recipe is None
    assert owner.acknowledge(first.copy) == "retired"


def test_prepared_witness_revokes_a_copy_when_private_binding_update_was_lost(result_owner, monkeypatch):
    owner = result_owner
    pointer = _claim(owner)
    reserved = []
    reserve_copy = owner.catalogue.reserve_copy

    def crash_after_copy(*args, **kwargs):
        reserved.append(reserve_copy(*args, **kwargs))
        raise SystemExit("lost after canonical reservation")

    monkeypatch.setattr(owner.catalogue, "reserve_copy", crash_after_copy)
    with pytest.raises(SystemExit, match="lost after canonical reservation"):
        _publish(owner, pointer, A)
    assert owner.acknowledge(reserved[0]) == "no_effect"
    with owner.journal.store._connect() as conn:
        assert conn.execute("SELECT result FROM mutations WHERE key=?", (pointer.key,)).fetchone()[0] is None


def test_missing_witness_never_adopts_identical_result_bytes(result_owner):
    owner = result_owner
    pointer = _claim(owner)
    first = _publish(owner, pointer, A)
    with owner.journal.store._connect() as conn:
        conn.execute("DELETE FROM source_result_publications WHERE publication_id=?", (first.binding.publication_id,))
    assert owner.acknowledge(first.copy) == "unknown"
    assert owner.catalogue.copy_record(first.copy.copy_id).value.closed is False


def _fence(owner, reference):
    manifest, guard = source_custody.read_manifest(owner.root, reference)
    retired = manifest.model_copy(update={"retirement": source_custody.RetirementFence(
        operation_id="retire-result", source_hash=manifest.allocated_hash)})
    source_custody.publish(owner.root, source_custody.PlannedCustody((source_custody._write(
        owner.root, source_custody.manifest_path(owner.root, reference), retired, guard, source_custody.MAX_MANIFEST_BYTES),)))


@pytest.mark.parametrize("encoding", ["pickle", "failure-json"])
def test_owned_retirement_redacts_a_without_waiting_for_replay_and_keeps_b(result_owner, encoding):
    from exomem import writer_lease

    owner = result_owner
    pointer = _claim(owner)
    if encoding == "failure-json":
        value = {"code": writer_lease._COMMITTED_FAILURE_CODE,
                 "message": writer_lease._COMMITTED_FAILURE_MESSAGE,
                 "remediation": writer_lease._COMMITTED_FAILURE_REMEDIATION,
                 "outcome": {"kind": "cleanup_incomplete", "committed": True, "incomplete": True,
                             "affected_count": 2, "targets": ["Sources/retired.md", "Sources/surviving.md"],
                             "omitted_target_count": 0}}
        recipe = attribution.Recipe(contributions=(
            attribution.Contribution(path=("outcome", "targets", 0), source_refs=(A,)),
            attribution.Contribution(path=("outcome", "targets", 1), source_refs=(B,)),
        ))
        encode, decode = writer_lease._serialize_committed_failure_payload, writer_lease._deserialize_committed_failure_payload
    else:
        value = {"source": {"path": "Sources/surviving.md", "ref": B}, "capture_outcome": "created",
                 "same_url": {"sources": [{"path": "Sources/retired.md", "ref": A}], "count": 4}}
        recipe = attribution.Recipe(contributions=(
            attribution.Contribution(path=("source",), source_refs=(B,)),
            attribution.Contribution(path=("same_url", "sources", 0), source_refs=(A,)),
            attribution.Contribution(path=("same_url", "count"), source_refs=(A, B), action="count"),
        ))
        encode, decode = pickle.dumps, pickle.loads

    def write(conn, raw):
        return conn.execute("UPDATE mutations SET result=?,state='completed' WHERE key=? AND attempt_id=?",
                            (raw, pointer.key, pointer.attempt_id)).rowcount

    first = owner.publish(pointer, value, recipe=recipe, encode=encode, write=write,
                          prior_publication_id=None, encoding=encoding)
    _fence(owner, A)
    assert owner.retire(first.copy, A) == "retired"
    with owner.journal.store._connect() as conn:
        result = decode(conn.execute("SELECT result FROM mutations WHERE key=?", (pointer.key,)).fetchone()[0])
        remaining = owner.journal.selected(conn, pointer.kind, pointer.key)
    assert "Sources/retired.md" not in str(result)
    assert "Sources/surviving.md" in str(result)
    assert remaining.recipe.sources == (B,)
    if encoding == "failure-json":
        assert result["outcome"]["affected_count"] == 2
        assert result["outcome"]["omitted_target_count"] == 1
    else:
        assert result["capture_outcome"] == "created"
        assert result["same_url"]["count"] is None
        assert result["source_projection"]["state"] == "partial"


@pytest.mark.parametrize("reserved", [False, True])
def test_real_capture_result_keeps_source_custody_and_replays_a_retired_projection(
    vault, attempts, monkeypatch, reserved,
):
    from exomem import commands, generated_fragment_store, memory_refs

    manager, call = attempts
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')
    arguments = authored()
    ticket = reserve(call, arguments) if reserved else None
    if ticket is not None:
        arguments["capture_reservation"] = ticket
    captured = call("capture_source", **arguments)
    reference = memory_refs.ref_from_markdown((vault / captured["source"]["path"]).read_text())
    with manager.idempotency._connect() as conn:
        selected = results.Binding.model_validate_json(conn.execute(
            "SELECT data FROM source_result_publications WHERE state='selected'").fetchone()[0])
    assert selected.pointer.kind == ("capture" if reserved else "mutation")
    assert reference in selected.recipe.sources
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        manifest, guard = source_custody.read_manifest(vault, reference)
        retired = manifest.model_copy(update={"retirement": source_custody.RetirementFence(
            operation_id="retire-capture", source_hash=manifest.allocated_hash)})
        source_custody.publish(vault, source_custody.PlannedCustody((source_custody._write(
            vault, source_custody.manifest_path(vault, reference), retired, guard, source_custody.MAX_MANIFEST_BYTES),)))

    def must_replay(*args, **kwargs):
        pytest.fail("retired result executed capture again")

    monkeypatch.setattr(commands, "_add_source", must_replay)
    replayed = call("capture_source", **arguments)
    assert captured["source"]["path"] not in str(replayed)
    assert reference not in str(replayed)
    assert replayed["capture_outcome"] == "created"
    assert replayed["source_projection"]["state"] == "unavailable"
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        catalogue = generated_fragment_store.GeneratedFragmentStore(vault)
        try:
            assert catalogue.copy_record(selected.copy_id).value.outcome == "retired"
        finally:
            catalogue.close()
    with manager.idempotency._connect() as conn:
        assert conn.execute("SELECT count(*) FROM " + ("capture_attempts" if reserved else "mutations")).fetchone()[0] == 1


def test_real_committed_capture_failure_keeps_its_format_and_retires_identifying_targets(
    vault, attempts, monkeypatch,
):
    from exomem import add, memory_refs
    from exomem import vault as vault_module

    manager, call = attempts
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')
    original_batch = add.batch_atomic_write
    written = []

    def lose_cleanup(writes, **options):
        original_batch(writes, **options)
        written.extend(writes)
        targets = tuple(write.path.relative_to(vault).as_posix() for write in writes)[:16]
        raise vault_module.BatchWriteError("BATCH_CLEANUP_INCOMPLETE",
            vault_module.BatchTargetSummary(len(writes), targets, len(writes) - len(targets)), True)

    monkeypatch.setattr(add, "batch_atomic_write", lose_cleanup)
    with pytest.raises(vault_module.BatchWriteError) as original:
        call("capture_source", **authored())
    sources = [(write.path.relative_to(vault).as_posix(), memory_refs.ref_from_markdown(write.content))
               for write in written if isinstance(write.content, str)]
    path, reference = next((path, reference) for path, reference in sources if reference is not None)
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        owner = results.ResultCustody(vault, journal=manager.idempotency.source_results)
        try:
            with manager.idempotency._connect() as conn:
                selected = owner.journal._decode(conn.execute("SELECT publication_id,kind,row_key,state,data "
                    "FROM source_result_publications WHERE state='selected'").fetchone())
            assert selected.encoding == "failure-json" and selected.recipe.sources == (reference,)
            copy = owner.catalogue.copy_record(selected.copy_id).value
            _fence(owner, reference)
            assert owner.retire(copy, reference) == "retired"
        finally:
            owner.close()

    def must_replay(*args, **kwargs):
        pytest.fail("committed failure executed the capture again")

    monkeypatch.setattr(add, "batch_atomic_write", must_replay)
    with pytest.raises(ValueError) as replayed:
        call("capture_source", **authored())
    payload = replayed.value.as_public_dict()
    assert payload["code"] == original.value.code
    assert payload["outcome"]["affected_count"] == original.value.affected_count
    assert path not in payload["outcome"]["targets"]
    assert payload["outcome"]["omitted_target_count"] >= 1


@pytest.mark.usefixtures("duplicate_index")
def test_truncated_same_url_capture_preserves_known_members_and_finite_uncertainty(
    vault, attempts, monkeypatch,
):
    manager, call = attempts
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')
    for index in range(6):
        arguments = authored()
        arguments.update(title=f"Survey section {index}", content=f"Independent observation number {index}.",
                         url="https://example.test/study")
        arguments["source_identity"] = {**arguments["source_identity"], "record_id": f"section-{index}"}
        ticket = reserve(call, arguments)
        call("capture_source", **arguments, capture_reservation=ticket)
    arguments = authored()
    arguments.update(title="Survey section 6", content="Independent observation number 6.",
                     url="https://example.test/study")
    arguments.pop("source_identity")  # Ordinary text capture owns the same-URL advisory; identity capture deliberately skips it.
    captured = call("capture_source", **arguments, response_detail="full")
    assert captured["same_url"]["count"] == 6
    assert len(captured["same_url"]["sources"]) == 5
    primary = captured["diagnostics"]["source"]["ref"]
    shown = {row["ref"] for row in captured["same_url"]["sources"]}
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(vault):
        owner = results.ResultCustody(vault, journal=manager.idempotency.source_results)
        try:
            with manager.idempotency._connect() as conn:
                key = conn.execute("SELECT key FROM mutations").fetchone()[0]
                selected = owner.journal.selected(conn, "mutation", key)
            assert selected.recipe.unknown is True
            assert set(selected.recipe.sources) == {primary, *shown}
            image = owner.catalogue.image_header(selected.image_id).value
            assert image.unknown_through_epoch > 0
            assert set(image.opaque_source_refs) == {primary, *shown}
        finally:
            owner.close()
