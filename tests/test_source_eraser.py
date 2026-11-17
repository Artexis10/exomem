"""Captured-Source erasure retires one exact incarnation and reports only acknowledged completion."""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import hashlib
import json
import time

import pytest
from test_capture_attempts import attempts, authored, reserve  # noqa: F401
from test_passage_evidence import publish_reference

from exomem import (
    capture_attempts,
    embeddings,
    file_watcher,
    generated_fragment_store,
    graph_drain,
    memory_refs,
    privacy_log,
    source_custody,
    writer_lease,
)
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.governance.principal import owner_principal, request_scope

IDENTITY = {"system": "archive", "record_id": "survey", "revision": "one"}


@pytest.fixture
def erasable(monkeypatch, vault):
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')
    # A Cloud cell keeps process logs content-private; tests outside that cell cover the unknown host log.
    monkeypatch.setattr(privacy_log, "content_private_logging_enabled", lambda env=None: True)
    # A service runs this daemon; without it a graph drop queued for repair never acknowledges.
    graph_drain.start(vault)


def capture(vault, call, **changes):
    args = {**authored(), **changes}
    ticket = reserve(call, args)
    path = call("capture_source", **args, capture_reservation=ticket)["source"]["path"]
    text = (vault / path).read_text(encoding="utf-8")
    return path, memory_refs.ref_from_markdown(text), vault_module.content_hash(text)


def erase(call, reference, source_hash, operation_id="erase-one", identity=IDENTITY):
    return call("erase_captured_source", operation_id=operation_id, source_identity=identity,
                expected_source_ref=reference, expected_source_hash=source_hash)


def drive(call, reference, source_hash, operation_id="erase-one", seconds=30.0):
    """Retry as a caller does: a pending answer means the next call makes progress."""
    deadline = time.monotonic() + seconds
    result = erase(call, reference, source_hash, operation_id)
    while result["status"] == "pending" and time.monotonic() < deadline:
        time.sleep(0.05)
        result = erase(call, reference, source_hash, operation_id)
    return result


def lookup_empty(vault, manager, reference):
    manifest, _ = source_custody.read_manifest(vault, reference)
    with manager.mutation_guard(vault):
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        try:
            found = store.lookup(reference, manifest.generated_birth, limit=4)
        finally:
            store.close()
    return found.state == "ready" and not found.copies and not found.image_ids


def test_erase_retires_the_page_copies_and_derived_rows_until_complete(vault, attempts, erasable):
    manager, call = attempts
    path, reference, source_hash = capture(vault, call)
    result = drive(call, reference, source_hash)
    assert result["status"] == "complete" and result["target_retired"] is True, json.dumps(result, indent=1)
    assert {item["state"] for item in result["components"].values()} == {"acknowledged"}
    assert not (vault / path).exists()
    assert not any((vault / "Knowledge Base" / ".trash").rglob("*")) if (vault / "Knowledge Base" / ".trash").exists() else True
    assert lookup_empty(vault, manager, reference)
    manifest, _ = source_custody.read_manifest(vault, reference)
    assert manifest.retirement.complete is True
    # A repeat under another operation answers from the content-free completion record.
    again = erase(call, reference, source_hash, operation_id="erase-two")
    assert again["status"] == "complete" and again["target_source_ref"] == reference
    _path, fresh, _hash = capture(vault, call)
    assert fresh != reference


def test_repeat_returns_the_same_target_and_a_conflicting_rebind_is_refused(vault, attempts, erasable):
    _manager, call = attempts
    _path, reference, source_hash = capture(vault, call)
    first = erase(call, reference, source_hash)
    second = erase(call, reference, source_hash)
    assert first["target_source_ref"] == second["target_source_ref"] == reference
    _other, other_ref, other_hash = capture(vault, call, title="Second survey",
                                            source_identity={**IDENTITY, "record_id": "second"})
    with pytest.raises(Exception, match="SOURCE_ERASURE_OPERATION_CONFLICT"):
        erase(call, other_ref, other_hash, identity={**IDENTITY, "record_id": "second"})


def test_disabled_policy_refuses_before_any_lookup(vault, attempts, monkeypatch):
    _manager, call = attempts
    monkeypatch.delenv("EXOMEM_SOURCE_ERASURE_POLICY", raising=False)
    reference = memory_refs.memory_ref(memory_refs.new_id())
    with pytest.raises(Exception, match="SOURCE_ERASURE_DISABLED"):
        erase(call, reference, "0" * 64)


def test_store_mode_refuses_and_its_policy_read_reports_disabled(vault, attempts, erasable, monkeypatch):
    from exomem import source_erasure
    from exomem.vault_store import mode

    _manager, call = attempts
    monkeypatch.setattr(mode, "store_mode_active", lambda root: True)
    with pytest.raises(Exception, match="SOURCE_ERASURE_MODE_UNSUPPORTED"):
        erase(call, memory_refs.memory_ref(memory_refs.new_id()), "0" * 64)
    with request_scope(owner_principal(surface="mcp")):
        policy = source_erasure.inspect_policy(vault, system="archive")
    assert policy["state"] == "observed" and policy["enabled"] is False


def test_missing_manifest_and_page_is_unknown_never_complete(vault, attempts, erasable):
    _manager, call = attempts
    result = erase(call, memory_refs.memory_ref(memory_refs.new_id()), "0" * 64)
    assert result["status"] == "unknown" and result["target_retired"] is False
    assert result["failure"] == {"code": "custody_missing"}


def test_unenrolled_source_is_refused(vault, attempts, erasable, monkeypatch):
    manager, call = attempts
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":[]}')
    args = authored()
    path = call("capture_source", **args)["source"]["path"]
    text = (vault / path).read_text(encoding="utf-8")
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')
    with pytest.raises(Exception, match="SOURCE_ERASURE_NOT_ENROLLED"):
        erase(call, memory_refs.ref_from_markdown(text), vault_module.content_hash(text))
    assert (vault / path).read_text(encoding="utf-8") == text


def test_stale_hash_is_refused_and_leaves_the_source(vault, attempts, erasable):
    _manager, call = attempts
    path, reference, _hash = capture(vault, call)
    with pytest.raises(Exception, match="SOURCE_ERASURE_STALE"):
        erase(call, reference, "1" * 64)
    assert (vault / path).exists()
    manifest, _ = source_custody.read_manifest(vault, reference)
    assert manifest.retirement is None


def test_inbound_link_refuses_and_dependency_latch_refuses(vault, attempts, erasable):
    manager, call = attempts
    path, reference, source_hash = capture(vault, call)
    note = vault / "Knowledge Base" / "Notes" / "Citing.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(f"# Citing\n\nSee [[{path.removesuffix('.md')}]].\n", encoding="utf-8")
    with pytest.raises(Exception, match="SOURCE_ERASURE_DEPENDENT"):
        erase(call, reference, source_hash)
    note.unlink()
    with manager.mutation_guard(vault):
        manifest, guard = source_custody.read_manifest(vault, reference)
        latch = manifest.dependency.model_copy(update={"first_publication": source_custody.OwnerLocator(
            owner="semantic_writes", locator="compiled", version="one")})
        updated = source_custody.Manifest.model_validate({**manifest.model_dump(), "dependency": latch.model_dump()})
        source_custody.publish(vault, source_custody.PlannedCustody((source_custody._write(
            vault, source_custody.manifest_path(vault, reference), updated, guard, source_custody.MAX_MANIFEST_BYTES),)))
    with pytest.raises(Exception, match="SOURCE_ERASURE_DEPENDENT"):
        erase(call, reference, source_hash)
    assert (vault / path).exists()


def test_uncertain_dependency_and_missing_coverage_answer_unknown(vault, attempts, erasable):
    manager, call = attempts
    path, reference, source_hash = capture(vault, call)
    with manager.mutation_guard(vault):
        manifest, guard = source_custody.read_manifest(vault, reference)
        updated = source_custody.Manifest.model_validate({**manifest.model_dump(), "coverage_complete": False})
        source_custody.publish(vault, source_custody.PlannedCustody((source_custody._write(
            vault, source_custody.manifest_path(vault, reference), updated, guard, source_custody.MAX_MANIFEST_BYTES),)))
    result = erase(call, reference, source_hash)
    assert result["status"] == "unknown" and result["failure"] == {"code": "coverage_incomplete"}
    assert (vault / path).exists()


def test_dependency_publication_after_the_cutoff_is_refused(vault, attempts, erasable):
    """A compile that loses the race to the cutoff cannot add a dependency on the retired incarnation."""
    manager, call = attempts
    _path, reference, source_hash = capture(vault, call)
    erase(call, reference, source_hash)
    with manager.mutation_guard(vault):
        with pytest.raises(OpError) as refused:
            source_custody.reserve_dependencies(vault, [reference], producer=source_custody.OwnerLocator(
                owner="semantic_writes", locator="compiled", version="one"), operation_id="compile-one",
                checkpoint="compile-one")
    assert refused.value.code == "SOURCE_RETIRED"


def test_an_allocation_whose_page_never_published_is_retired_through_its_attempt(vault, attempts, erasable, monkeypatch):
    """Without this, a capture that crashed before publication would leave its allocation unerasable forever."""
    from exomem import add

    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    with monkeypatch.context() as crash:
        crash.setattr(add, "batch_atomic_write", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("host lost")))
        with pytest.raises(RuntimeError, match="host lost"):
            call("capture_source", **args, capture_reservation=ticket)
    (target,) = call("settle_source_capture", reservation=ticket)["owned_targets"]
    manifest, _ = source_custody.read_manifest(vault, target["source_ref"])
    assert manifest.publication == "allocated"

    result = drive(call, target["source_ref"], target["source_hash"])

    assert result["status"] == "complete", result
    assert lookup_empty(vault, manager, target["source_ref"])


def test_a_copy_reserved_before_its_native_binding_closes_as_no_effect(vault, attempts, erasable):
    """A crash between the catalogue reservation and the native binding must not leave the erase unknown forever."""
    import uuid

    from exomem import source_native_custody

    manager, call = attempts
    _path, reference, source_hash = capture(vault, call)
    with manager.mutation_guard(vault):
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        try:
            with store._connect() as conn:
                image_id, data = conn.execute(
                    "SELECT c.image_id,c.data FROM copies c JOIN members m ON m.image_id=c.image_id "
                    "WHERE m.source_ref=? AND json_extract(c.data,'$.owner.owner')=?",
                    (reference, source_native_custody.OWNER)).fetchone()
            template = generated_fragment_store.Copy.model_validate_json(data)
            # The binding this copy names was never written, so no byte can exist for it.
            store.reserve_copy(image_id, owner=template.owner.model_copy(update={"version": uuid.uuid4().hex}),
                               origin_ledger=template.origin_ledger, expected_header=store.image_header(image_id).value,
                               expected_root=vault)
        finally:
            store.close()

    result = drive(call, reference, source_hash)

    assert result["status"] == "complete", json.dumps(result)
    assert lookup_empty(vault, manager, reference)


def test_host_without_content_private_logs_reports_unknown(vault, attempts, monkeypatch):
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')
    monkeypatch.setattr(privacy_log, "content_private_logging_enabled", lambda env=None: False)
    _manager, call = attempts
    _path, reference, source_hash = capture(vault, call)
    result = drive(call, reference, source_hash)
    assert result["status"] == "unknown" and result["target_retired"] is False
    assert result["components"]["host_logs"] == {"state": "unknown"}
    assert json.dumps(result).count(reference) == 1


def test_service_erases_a_captured_source_with_passages_until_every_owner_acknowledges(vault, monkeypatch):
    """The highest-level proof: a service reserves, captures, finds a passage, erases, and nothing answers after."""
    from fastmcp import Client
    from test_vault_store_embeddings import install_encoder

    from exomem import readiness, server

    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    monkeypatch.delenv("KB_MCP_DISABLE_EMBEDDINGS", raising=False)
    install_encoder(monkeypatch)
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')
    args = {"title": "Harbour survey", "source_kind": "session", "source_identity": IDENTITY,
            "passages": [{"key": "r/42", "text": "The lantern keeper logs oil."}]}

    async def service_run():
        # One server lifetime, as a deployed cell runs, so its background graph owner stays live.
        async with Client(server.build_server(require_auth=False)) as client:
            async def call(name, arguments):
                result = await client.call_tool(name, arguments, raise_on_error=False)
                payload = result.structured_content or {"error": " ".join(getattr(b, "text", "") for b in result.content)}
                return payload.get("result", payload)

            listed = (await call("read_instructions", {"topic": "manage_memory"}))["operations"].get("sources", [])
            assert "erase" not in listed
            wire = capture_attempts.CaptureWireV1.model_validate(args)
            ticket = (await call("manage_memory", {"operation": "sources.reserve_capture", "arguments": {
                "source_identity": IDENTITY, "request_sha256": wire.sha256()}}))["reservation"]
            options = {"source_identity": IDENTITY, "capture_reservation": ticket, "passages": args["passages"]}
            captured = await call("save_source", {"title": args["title"], "source_kind": "session",
                                                  "options": options, "detail": "full"})
            assert captured.get("capture_outcome") == "created", captured
            path = captured["diagnostics"]["operational"]["source_path"]
            text = (vault / path).read_text(encoding="utf-8")
            reference, source_hash = memory_refs.ref_from_markdown(text), vault_module.content_hash(text)
            embeddings.index_incremental(vault)
            publish_reference(vault)
            # The service's watcher publishes the inventory; this test drives it synchronously.
            watcher = file_watcher.FileWatcher(vault)
            watcher._raw_witness.activate()
            assert watcher._flush_raw(census=True)
            monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
            monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
            found = await call("search_memory", {"query": "lantern", "limit": 1, "options": {"result_level": "passage"}})
            assert [hit["ref"] for hit in found["hits"]] == [reference], found
            listed = await call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive"}})
            assert [item["source_ref"] for item in listed["items"]] == [reference], listed

            request = {"operation_id": "erase-one", "source_identity": IDENTITY,
                       "expected_source_ref": reference, "expected_source_hash": source_hash}
            deadline = time.monotonic() + 60
            result = await call("manage_memory", {"operation": "sources.erase", "arguments": request})
            while result.get("status") == "pending" and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                result = await call("manage_memory", {"operation": "sources.erase", "arguments": request})
            assert result["status"] == "complete" and result["target_retired"] is True, json.dumps(result)
            assert {item["state"] for item in result["components"].values()} == {"acknowledged"}, result

            assert not (vault / path).exists()
            assert watcher._flush_raw(census=True)
            after = await call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive"}})
            assert after["complete"] is True and after["items"] == [], after
            gone = await call("search_memory", {"query": "lantern", "limit": 1, "options": {"result_level": "passage"}})
            assert gone.get("hits") == [], gone
            assert lookup_empty(vault, writer_lease.active_manager(), reference)
            again = await call("save_source", {"title": args["title"], "source_kind": "session", "detail": "full",
                                               "options": {"source_identity": IDENTITY, "passages": args["passages"]}})
            assert again.get("capture_outcome") == "created", again
            fresh = (vault / again["diagnostics"]["operational"]["source_path"]).read_text(encoding="utf-8")
            assert memory_refs.ref_from_markdown(fresh) != reference

    try:
        asyncio.run(service_run())
    finally:
        readiness.reset()


_CHILD = """
import os, sys, time
from pathlib import Path
from exomem import _held_fs_posix, commands, index_sync, privacy_log, source_custody, source_eraser, writer_lease
from exomem.governance.principal import owner_principal, request_scope

vault, state, reference, source_hash, phase = sys.argv[1:]
privacy_log.content_private_logging_enabled = lambda env=None: True  # As the erasable fixture does.

def die(*args, **kwargs):
    os._exit(77)

def then_die(real):
    return lambda *args, **kwargs: (real(*args, **kwargs), die())

held = _held_fs_posix.PosixHeldFilesystem
real_retire = source_custody.retire
if phase == "before_fence":
    source_custody.retire = die
elif phase == "after_fence":
    source_eraser._Eraser._copies = die
elif phase == "after_move":
    held.rename = then_die(held.rename)
elif phase == "after_unlink":
    held.unlink = then_die(held.unlink)
elif phase == "before_derived":
    index_sync.delete_after_remove = die
elif phase == "after_derived":
    index_sync.delete_after_remove = then_die(index_sync.delete_after_remove)
elif phase == "before_completion_record":
    source_custody.retire = lambda root, ref, fence: die() if fence.complete else real_retire(root, ref, fence)
manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=Path(state)))
command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "erase_captured_source")
arguments = {"operation_id": "erase-one", "source_identity": {"system": "archive", "record_id": "survey", "revision": "one"},
             "expected_source_ref": reference, "expected_source_hash": source_hash}
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    with request_scope(owner_principal(surface="mcp")):
        last = manager.invoke(command, (Path(vault),), arguments)
    time.sleep(0.05)
print(last, file=sys.stderr)  # The phase never ran; the parent shows why.
os._exit(0)
"""


@pytest.mark.parametrize("phase", ["before_fence", "after_fence", "after_move", "after_unlink", "before_derived",
                                   "after_derived", "before_completion_record"])
def test_a_process_killed_at_each_phase_is_finished_by_a_later_retry(vault, attempts, erasable, tmp_path, phase):
    """Each phase leaves a different durable state; a retry must reach complete from every one of them."""
    import subprocess
    import sys

    manager, call = attempts
    path, reference, source_hash = capture(vault, call)
    writer_lease.reset_managers_for_tests()
    child = subprocess.run([sys.executable, "-c", _CHILD, str(vault), str(tmp_path / "attempt-state"), reference,
                            source_hash, phase], capture_output=True, text=True, timeout=120)
    assert child.returncode == 77, child.stderr[-2000:]
    result = drive(call, reference, source_hash)
    assert result["status"] == "complete", json.dumps(result)
    assert not (vault / path).exists()
    assert lookup_empty(vault, writer_lease.active_manager(), reference)


def test_a_delayed_retry_from_a_fresh_process_finishes_without_touching_later_captures(vault, attempts, erasable,
                                                                                      monkeypatch):
    """A caller retries hours later from another process, after other Sources were captured."""
    from exomem import source_eraser

    _manager, call = attempts
    path, reference, source_hash = capture(vault, call)
    monkeypatch.setattr(source_eraser._Eraser, "_derived", lambda self, progress, manifest: {"state": "pending"})
    assert erase(call, reference, source_hash)["status"] == "pending"
    monkeypatch.undo()
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')
    monkeypatch.setattr(privacy_log, "content_private_logging_enabled", lambda env=None: True)
    writer_lease.reset_managers_for_tests()
    other, other_ref, _other_hash = capture(vault, call, title="Second survey", source_identity={**IDENTITY, "record_id": "second"})
    result = drive(call, reference, source_hash)
    assert result["status"] == "complete", json.dumps(result)
    assert not (vault / path).exists()
    assert memory_refs.ref_from_markdown((vault / other).read_text(encoding="utf-8")) == other_ref


def test_a_recapture_at_the_same_path_keeps_the_new_incarnation_indexed(vault, attempts, erasable, monkeypatch):
    """Path-keyed owners must drop the retired rows without dropping the fresh page that reuses the name."""
    from exomem import lexstore, source_eraser

    _manager, call = attempts
    assert lexstore.get_store(vault).rebuild_atomic()  # A live cell keeps its lexical index built.
    path, reference, source_hash = capture(vault, call)
    monkeypatch.setattr(source_eraser._Eraser, "_derived", lambda self, progress, manifest: {"state": "pending"})
    assert erase(call, reference, source_hash)["status"] == "pending"
    monkeypatch.undo()
    monkeypatch.setenv("EXOMEM_SOURCE_ERASURE_POLICY", '{"enabled_systems":["archive"]}')
    monkeypatch.setattr(privacy_log, "content_private_logging_enabled", lambda env=None: True)
    fresh_path, fresh_ref, fresh_hash = capture(vault, call)
    assert fresh_path == path and fresh_ref != reference
    result = drive(call, reference, source_hash)
    assert result["status"] == "complete", json.dumps(result)
    digest = hashlib.sha256((vault / path).read_bytes()).hexdigest()
    assert memory_refs.holds_content_identities(vault, {path: digest}) == {path: True}
    assert lexstore.holds_content_identities(vault, {path: digest}) == {path: True}
    assert memory_refs.ref_from_markdown((vault / path).read_text(encoding="utf-8")) == fresh_ref
    assert vault_module.content_hash((vault / path).read_text(encoding="utf-8")) == fresh_hash


def _seed_unclosable_copies(vault, manager, reference: str, copies: int, unrelated: int) -> str:
    """Open copies of the Source's image that no owner can close, ordered before its own copy, plus unrelated rows."""
    import itertools
    import uuid

    from exomem import source_native_custody

    with manager.mutation_guard(vault):
        store = generated_fragment_store.GeneratedFragmentStore(vault)
        native = source_native_custody.NativeCustody(vault)
        try:
            with store._connect() as conn:
                image_id, data = conn.execute(
                    "SELECT c.image_id,c.data FROM copies c JOIN members m ON m.image_id=c.image_id "
                    "WHERE m.source_ref=? AND json_extract(c.data,'$.owner.owner')=?",
                    (reference, source_native_custody.OWNER)).fetchone()
            template = generated_fragment_store.Copy.model_validate_json(data)
            with native._connect() as conn:
                ledger, scope, binding = conn.execute(
                    "SELECT ledger_id,physical_scope,data FROM source_native_bindings WHERE source_ref=?",
                    (reference,)).fetchone()
                # Each seeded copy names a native binding that does not match it, so every call reads its
                # custody row and must leave it open.
                probes = [uuid.uuid4().hex for _ in range(copies)]
                conn.executemany("INSERT INTO source_native_bindings VALUES (?,?,?,NULL,NULL,NULL,?)",
                                 [(probe, ledger, scope, binding) for probe in probes])
                conn.executemany("INSERT INTO source_native_bindings VALUES (?,?,?,?,NULL,NULL,?)", [
                    (uuid.uuid4().hex, ledger, scope, memory_refs.memory_ref(str(uuid.uuid4())), binding)
                    for _ in range(unrelated)])
            header = store.image_header(image_id).value
            ordered = itertools.count(1)
            with pytest.MonkeyPatch.context() as patch:
                # Low identifiers sort these copies ahead of the Source's own randomly named copy.
                patch.setattr(generated_fragment_store, "_id", lambda: f"{next(ordered):032x}")
                for probe in probes:
                    store.reserve_copy(image_id, owner=template.owner.model_copy(update={"version": probe}),
                                       origin_ledger=template.origin_ledger, expected_header=header, expected_root=vault)
            with store._connect() as conn:
                # Unrelated Sources sit in the same indexes the step reads; they have no catalogue records.
                conn.executemany("INSERT INTO members(source_ref,image_id) VALUES (?,?)",
                                 [(memory_refs.memory_ref(str(uuid.uuid4())), f"unrelated-{n:06d}") for n in range(unrelated)])
                conn.executemany("INSERT INTO images(image_id,header,horizon,exact,seen) SELECT ?,header,horizon,exact,seen "
                                 "FROM images WHERE image_id=?", [(f"unrelated-{n:06d}", image_id) for n in range(unrelated)])
                conn.commit()
            with manager.idempotency._connect() as conn:
                conn.executemany("INSERT INTO capture_attempt_custody VALUES (?,?,NULL,'published')", [
                    (f"unrelated-{n:06d}", json.dumps({"source_ref": memory_refs.memory_ref(str(uuid.uuid4()))}))
                    for n in range(unrelated)])
        finally:
            store.close()
    return image_id


def test_copies_no_owner_can_close_do_not_starve_the_pages_own_copy(vault, attempts, erasable):
    """A fixed first page of unclosable copies would leave the canonical page bytes on disk forever."""
    manager, call = attempts
    path, reference, source_hash = capture(vault, call)
    _seed_unclosable_copies(vault, manager, reference, copies=40, unrelated=0)
    for _ in range(6):
        result = erase(call, reference, source_hash)
    assert result["components"]["canonical"]["state"] == "acknowledged", result
    assert not (vault / path).exists()
    assert result["components"]["copies"]["state"] == "unknown" and result["status"] != "complete"


def _progress_cost(vault, attempts, monkeypatch, copies: int, unrelated: int) -> tuple[int, int]:
    """Statements and SQLite VM steps of one progress call, after the cutoff, with seeded open copies."""
    import sqlite3
    import threading

    manager, call = attempts
    _path, reference, source_hash = capture(vault, call)
    image_id = _seed_unclosable_copies(vault, manager, reference, copies, unrelated)
    assert erase(call, reference, source_hash)["status"] != "complete"  # Mismatched copies never acknowledge.
    from exomem import source_eraser

    # The graph's currency probe reads two fixed sidecar rows; a background build's timing must not change the count.
    monkeypatch.setattr(source_eraser._Eraser, "_owner_done", lambda self, component, outcome, graph: True)
    counts, caller = {"statements": 0, "steps": 0}, threading.get_ident()
    connect = sqlite3.connect

    def count(key):
        # Background index and graph workers share the process; only the erase call's own work is its cost.
        if threading.get_ident() == caller:
            counts[key] += 1

    def counted(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(lambda _sql: count("statements"))
        conn.set_progress_handler(lambda: count("steps") or 0, 100)
        return conn

    monkeypatch.setattr(sqlite3, "connect", counted)
    result = erase(call, reference, source_hash)
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert result["status"] != "complete" and result["components"]["copies"]["state"] == "unknown", result
    with generated_fragment_store.GeneratedFragmentStore(vault)._connect() as conn:
        assert conn.execute("SELECT count(*) FROM members WHERE image_id LIKE 'unrelated-%'").fetchone()[0] == unrelated
        assert conn.execute("SELECT count(*) FROM copies WHERE image_id=? AND closed=0",
                            (image_id,)).fetchone()[0] >= copies
    return counts["statements"], counts["steps"]


@pytest.mark.parametrize(("copies", "unrelated"), [(2, 0), (200, 0), (200, 20_000)])
def test_a_progress_call_costs_the_same_however_many_copies_or_unrelated_sources(vault, attempts, erasable, monkeypatch,
                                                                                 record_property, copies, unrelated):
    """One query per copy, or a scan of every Source, would make the erase step grow with the vault."""
    statements, steps = _progress_cost(vault, attempts, monkeypatch, copies, unrelated)
    record_property("progress_cost", {"copies": copies, "unrelated": unrelated, "statements": statements,
                                      "vm_steps_x100": steps})
    print(f"PROGRESS_COST copies={copies} unrelated={unrelated} statements={statements} vm_steps_x100={steps}")
    # Fixed bounds just above the 2-copy run (321, 36): closing 128 copies a call costs 853, a 20,000-row scan far more.
    assert statements <= 400 and steps <= 200, (statements, steps)
