"""Captured-Source erasure retires one exact incarnation and reports only acknowledged completion."""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
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
    held_fs,
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


def later_write(call):
    """Another capture, as a live vault's traffic: the graph's handoff record names a page until a later write."""
    marker = secrets.token_hex(4)
    args = {**authored(), "title": f"Later note {marker}", "content": "A later, unrelated record.",
            "source_identity": {"system": "archive", "record_id": f"later-{marker}", "revision": "one"}}
    call("capture_source", **args, capture_reservation=reserve(call, args))


def drive(call, reference, source_hash, operation_id="erase-one", seconds=30.0):
    """Retry as a caller does: a pending answer, or a busy writer, means a later call makes progress.

    Each call holds the writer boundary, so a caller that repeats at once starves the graph drain it waits for.
    """
    deadline = time.monotonic() + seconds
    wrote = False
    while True:
        try:
            result = erase(call, reference, source_hash, operation_id)
        except OpError as busy:
            if busy.code != "MUTATION_BUSY" or time.monotonic() >= deadline:
                raise
        else:
            if result["status"] != "pending" or time.monotonic() >= deadline:
                return result
            if not wrote:
                later_write(call)
                wrote = True
        time.sleep(0.5)


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
    assert result["settled"] is True
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


def _holders(roots, needles: list[str]) -> list[str]:
    """Every live SQLite table and plain file under these roots whose bytes or name hold one of the needles."""
    import sqlite3

    encoded = [needle.encode("utf-8") for needle in needles]
    found = set()
    for root in roots:
        for file in sorted(item for item in root.rglob("*") if item.is_file()):
            name = file.relative_to(root).as_posix()
            if any(needle in name for needle in needles):
                found.add(f"{name} (name)")
            if file.name.endswith(("-wal", "-shm", "-journal")):
                continue  # Their committed rows are read through the database that owns them.
            for attempt in range(3):
                try:
                    found |= {f"{name}{place}" for place in _file_holders(file, encoded)}
                    break
                except FileNotFoundError:
                    break  # A temporary file that vanished holds nothing.
                except (OSError, sqlite3.Error) as error:
                    if attempt == 2:
                        found.add(f"{name} (unreadable: {error})")  # An unread store is no proof of absence.
                    time.sleep(0.2)
    return sorted(found)


def _file_holders(file, encoded):
    import sqlite3

    data = file.read_bytes()
    if not data.startswith(b"SQLite format 3\x00"):
        return [""] if any(needle in data for needle in encoded) else []
    places = set()
    # Opened writable: a read-only reader cannot join a WAL database whose shared index it may not create.
    conn = sqlite3.connect(file, timeout=5)
    try:
        # A virtual table's rows live in its shadow tables, which this loop reads as ordinary tables.
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                 "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'")]
        for table in tables:
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                for value in row:
                    raw = value.encode("utf-8") if isinstance(value, str) else value
                    if isinstance(raw, bytes) and any(needle in raw for needle in encoded):
                        places.add(f":{table}")
    finally:
        conn.close()
    return places


def test_complete_leaves_no_row_or_file_that_names_or_holds_the_source(vault, attempts, erasable, tmp_path):
    """The birth claim: after complete, no live store under the vault or its state names the page or holds its text."""
    from exomem import state_paths

    _manager, call = attempts
    path, reference, source_hash = capture(vault, call)
    call("ask_memory", query="café dawn")
    roots = [vault, state_paths.state_store_root(), tmp_path / "attempt-state"]
    needles = [path, path.rsplit("/", 1)[-1].removesuffix(".md"), "opens at dawn"]
    assert _holders(roots, needles)  # The sweep sees the Source while it exists.

    result = drive(call, reference, source_hash)

    assert result["status"] == "complete", json.dumps(result)
    holders = _holders(roots, needles)
    assert holders == [], "\n".join(holders)


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
    # The binding keeps a digest of the identity: the caller's record names never reach the operation ledger.
    with _manager.idempotency._connect() as conn:
        stored = [str(value) for row in conn.execute("SELECT * FROM source_erasure_operations") for value in row]
    assert stored and not [value for value in stored if "survey" in value or "archive" in value], stored


def test_an_enrolled_capture_leaves_its_path_out_of_the_write_log(vault, attempts, erasable, monkeypatch, tmp_path):
    """Off a Cloud cell the write log persists where the eraser cannot reach; an erasable path must never land there."""
    from test_vault_store_embeddings import install_encoder

    monkeypatch.setattr(privacy_log, "content_private_logging_enabled", lambda env=None: False)
    # The suite's embedding switch also silences the query logs.
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "logs"))
    install_encoder(monkeypatch)
    _manager, call = attempts
    path, _reference, _hash = capture(vault, call)
    plain = call("capture_source", title="Tide table", content="Tides turn at noon.", source_kind="session")

    records = [json.loads(line) for line in (tmp_path / "logs" / "writes.jsonl").read_text().splitlines()]

    assert [record["written_path"] for record in records] == [None, plain["path"]], records
    assert path not in (tmp_path / "logs" / "writes.jsonl").read_text()


def test_an_admitted_erasure_finishes_after_the_policy_is_disabled(vault, attempts, erasable, monkeypatch):
    """Turning the policy off must not strand a Source whose cutoff is written; it only refuses new operations."""
    _manager, call = attempts
    _path, reference, source_hash = capture(vault, call)
    _other, other_ref, other_hash = capture(vault, call, title="Second survey",
                                            source_identity={**IDENTITY, "record_id": "second"})
    first = erase(call, reference, source_hash)
    assert first["status"] == "pending" and first["settled"] is False, first
    monkeypatch.delenv("EXOMEM_SOURCE_ERASURE_POLICY")

    result = drive(call, reference, source_hash)

    assert result["status"] == "complete", json.dumps(result)
    with pytest.raises(OpError, match="SOURCE_ERASURE_DISABLED"):
        erase(call, other_ref, other_hash, operation_id="erase-two", identity={**IDENTITY, "record_id": "second"})


def test_a_catalogue_built_before_the_erase_is_not_adopted_after_it(vault, attempts, erasable):
    """A standby builds its catalogue apart from the erase; adopting it after the cutoff would serve the Source again."""
    import sqlite3

    from exomem import lexstore

    _manager, call = attempts
    assert lexstore.get_store(vault).rebuild_atomic()  # A live cell keeps its lexical index built.
    path, reference, source_hash = capture(vault, call)
    detached = lexstore.build_detached_catalog(vault)
    assert detached is not None
    try:
        assert drive(call, reference, source_hash)["status"] == "complete"
        assert lexstore.adopt_detached_catalog(vault, detached) is False
    finally:
        lexstore.discard_detached_catalog(vault, detached)
    with sqlite3.connect(lexstore.lexical_path(vault)) as conn:
        assert conn.execute("SELECT count(*) FROM pages WHERE path = ?", (path,)).fetchone()[0] == 0


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


def test_host_without_content_private_logs_reports_unknown(vault, attempts, erasable, monkeypatch):
    """Every other owner finishes, so the answer settles; logs it cannot vouch for keep it from complete."""
    monkeypatch.setattr(privacy_log, "content_private_logging_enabled", lambda env=None: False)
    _manager, call = attempts
    _path, reference, source_hash = capture(vault, call)
    result = drive(call, reference, source_hash)
    assert result["status"] == "unknown" and result["settled"] is True and result["target_retired"] is False
    assert {name for name, item in result["components"].items() if item["state"] != "acknowledged"} == {"host_logs"}
    assert json.dumps(result).count(reference) == 1


def published(vault) -> bool:
    """The service's maintained warm-up, run inline: the service's own catalogue repair can win the race once."""
    try:
        publish_reference(vault)
    except AssertionError:
        return False
    return True


async def until(condition, what, seconds=20.0, pause=0.05):
    """Wait on a product signal; the bound only turns a hang into a named failure."""
    deadline = time.monotonic() + seconds
    while True:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        assert time.monotonic() < deadline, f"timed out waiting for {what() if callable(what) else what}"
        await asyncio.sleep(pause)


@pytest.mark.timeout(180)  # Above the sum of its named waits, so a slow host reports which wait failed.
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

            async def answered(query):
                # The service republishes its passage index in the background; an unavailable index is no answer yet.
                found = await call("search_memory", {"query": query, "limit": 1, "options": {"result_level": "passage"}})
                return found if "hits" in found else None

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
            await until(lambda: published(vault), "the lexical catalogue to publish")
            # The service's watcher publishes the inventory; this test drives it synchronously.
            watcher = file_watcher.FileWatcher(vault)
            watcher._raw_witness.activate()
            assert watcher._flush_raw(census=True)
            monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
            monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
            found = await until(lambda: answered("lantern"), "the passage index to answer")
            assert [hit["ref"] for hit in found["hits"]] == [reference], found
            listed = await call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive"}})
            assert [item["source_ref"] for item in listed["items"]] == [reference], listed

            request = {"operation_id": "erase-one", "source_identity": IDENTITY,
                       "expected_source_ref": reference, "expected_source_hash": source_hash}
            last = {}

            async def settled():
                last.update(await call("manage_memory", {"operation": "sources.erase", "arguments": request}))
                return dict(last) if last.get("settled") else None

            assert not await settled()  # The first call fences the Source; its owners finish on later calls.
            # Later traffic replaces the graph's handoff record of the page, as a live vault's next write does.
            later = await call("save_source", {"title": "Tide table", "source_kind": "session",
                                               "options": {"passages": [{"key": "t/1", "text": "Tides turn at noon."}]}})
            assert "error" not in later, later
            # The documented loop: repeat until settled, then read the status. Each call holds the writer boundary
            # that the graph drain needs, so the caller pauses between calls as drive() does.
            result = await until(settled, lambda: f"the erase to settle; last answer {json.dumps(last)}",
                                 seconds=60.0, pause=0.5)
            assert result["status"] == "complete" and result["target_retired"] is True, json.dumps(result)
            assert {item["state"] for item in result["components"].values()} == {"acknowledged"}, result

            assert not (vault / path).exists()
            assert watcher._flush_raw(census=True)
            after = await call("inspect_memory", {"operation": "sources.inventory", "arguments": {"system": "archive"}})
            assert after["complete"] is True and after["items"] == [], after
            # The suite runs no watcher to keep the recall projection current, so the warm-up stands in again.
            await until(lambda: published(vault), "the lexical catalogue to publish after the erase")
            gone = await until(lambda: answered("lantern"), "the passage index to answer after the erase")
            # The later Source is the only page left, so passage search may rank it; never the erased one.
            erased = {hit["passage_hash"] for hit in found["hits"]}
            assert all(hit["ref"] != reference and hit["passage_hash"] not in erased for hit in gone["hits"]), gone
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
from exomem import (_held_fs_posix, commands, graph_drain, held_fs, index_sync, privacy_log, source_custody, source_eraser,
                    writer_lease)
from exomem.cli_ops import OpError
from exomem.governance.principal import owner_principal, request_scope

vault, state, reference, source_hash, phase, pages = sys.argv[1:]
privacy_log.content_private_logging_enabled = lambda env=None: True  # As the erasable fixture does.

def die(*args, **kwargs):
    os._exit(77)

def then_die(real):
    return lambda *args, **kwargs: (real(*args, **kwargs), die())

held = _held_fs_posix.PosixHeldFilesystem
real_retire, real_rename, real_unlink = source_custody.retire, held.rename, held.unlink

def in_pages(file, leaf):
    # Only the page's own staging name counts: the capability probe and other artifact publications use temp names too.
    found, wanted = os.fstat(file.parent_descriptor), os.stat(pages)
    return leaf.startswith(held_fs.PUBLISH_TEMP_PREFIX) and (found.st_dev, found.st_ino) == (wanted.st_dev, wanted.st_ino)

def staging_rename(self, source, parent, leaf, **kwargs):
    result = real_rename(self, source, parent, leaf, **kwargs)
    if in_pages(source, leaf):
        die()
    return result

def staging_unlink(self, file):
    named = in_pages(file, file.name)
    result = real_unlink(self, file)
    if named:
        die()
    return result
if phase == "before_fence":
    source_custody.retire = die
elif phase == "after_fence":
    source_eraser._Eraser._copies = die
elif phase == "after_move":
    held.rename = staging_rename
elif phase == "after_unlink":
    held.unlink = staging_unlink
elif phase == "before_derived":
    index_sync.delete_after_remove = die
elif phase == "after_derived":
    index_sync.delete_after_remove = then_die(index_sync.delete_after_remove)
elif phase == "before_completion_record":
    source_custody.retire = lambda root, ref, fence: die() if fence.complete else real_retire(root, ref, fence)
elif phase == "after_completion_record":
    source_custody.retire = lambda root, ref, fence: (real_retire(root, ref, fence), fence.complete and die())[0]
manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=Path(state)))
command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "erase_captured_source")
arguments = {"operation_id": "erase-one", "source_identity": {"system": "archive", "record_id": "survey", "revision": "one"},
             "expected_source_ref": reference, "expected_source_hash": source_hash}
graph_drain.start(Path(vault))  # This process stands in for the service, which runs the graph drain beside the erase.
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        with request_scope(owner_principal(surface="mcp")):
            last = manager.invoke(command, (Path(vault),), arguments)
    except OpError as busy:
        if busy.code != "MUTATION_BUSY":
            raise
    time.sleep(0.5)  # Each call holds the writer boundary that the graph drain needs.
print(last, file=sys.stderr)  # The phase never ran; the parent shows why.
os._exit(0)
"""


def _kill(vault, tmp_path, path: str, reference: str, source_hash: str, phase: str) -> None:
    import subprocess
    import sys

    writer_lease.reset_managers_for_tests()
    child = subprocess.run([sys.executable, "-c", _CHILD, str(vault), str(tmp_path / "attempt-state"), reference,
                            source_hash, phase, str((vault / path).parent)], capture_output=True, text=True, timeout=120)
    assert child.returncode == 77, child.stderr[-2000:]
    graph_drain.note_graph_debt()  # A restarted service drains at startup the debt that the killed process queued.


def _staged(vault, path: str) -> list[str]:
    return [item.name for item in (vault / path).parent.iterdir() if item.name.startswith(held_fs.PUBLISH_TEMP_PREFIX)]


@pytest.mark.parametrize("phase", ["before_fence", "after_fence", "after_move", "after_unlink", "before_derived",
                                   "after_derived", "before_completion_record", "after_completion_record"])
def test_a_process_killed_at_each_phase_is_finished_by_a_later_retry(vault, attempts, erasable, tmp_path, phase):
    """Each phase leaves a different durable state; a retry must reach complete from every one of them."""
    _manager, call = attempts
    path, reference, source_hash = capture(vault, call)
    later_write(call)  # The killed process has no traffic of its own to retire the graph's record of the page.
    _kill(vault, tmp_path, path, reference, source_hash, phase)
    if phase in ("after_move", "after_unlink"):
        # The kill landed on the page itself: moved to its staging name, or already unlinked from it.
        assert not (vault / path).exists() and len(_staged(vault, path)) == (phase == "after_move")
    result = drive(call, reference, source_hash)
    assert result["status"] == "complete", json.dumps(result)
    assert not (vault / path).exists()
    assert lookup_empty(vault, writer_lease.active_manager(), reference)
    # Saved progress names the page; a retry that answers from the completion record must drop it too.
    assert _holders([tmp_path / "attempt-state"], [path]) == []


def test_a_crash_after_the_page_move_finishes_when_another_source_takes_the_freed_name(vault, attempts, erasable,
                                                                                       tmp_path):
    """Checking the page name first would find the other Source there and leave the retired bytes staged forever."""
    _manager, call = attempts
    path, reference, source_hash = capture(vault, call)
    _kill(vault, tmp_path, path, reference, source_hash, "after_move")
    reused, reused_ref, _hash = capture(vault, call, source_identity={**IDENTITY, "record_id": "second"})
    assert reused == path and len(_staged(vault, path)) == 1

    result = drive(call, reference, source_hash)

    assert result["status"] == "complete", json.dumps(result)
    assert _staged(vault, path) == []
    assert memory_refs.ref_from_markdown((vault / path).read_text(encoding="utf-8")) == reused_ref


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
    monkeypatch.setattr(source_eraser._Eraser, "_owner_done", lambda self, component, outcome, path, reused: True)
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


@pytest.mark.timeout(180)  # Seeding 200 copies is 200 durable writes: 20 to 56 s alone on a loaded host.
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
