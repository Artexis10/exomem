"""Derived import collections (OpenSpec bring-in-large-exports §4).

A derived collection's rows live in a file beside the store and rebuild from its import
log. Each test names the defect only it catches. The export is invented: a device's
November days of local samples, preserved through the real archive expansion. The
service journey runs the real server lifecycle through the shared ``invoke_command``
dispatcher, which skips the MCP server's second egress pass.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import runpy
import sqlite3
import threading
import time
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_collection_store_import_exports import (
    FIELDS,
    MIXED_DOCUMENT,
    MIXED_FIELDS,
    MIXED_MAPPING,
    SAMPLES,
    at,
    samples,
)
from test_collection_store_importer import manifest_text
from test_collection_store_importer import run as run_jobs
from test_collection_store_s1_gate import _until
from test_collection_store_service import (  # noqa: F401 - the service fixture
    ON,
    Service,
    call,
    imported,
    service,
    titles,
)
from test_collection_store_writer import CID, manifest_path

from exomem import archive_members, commands, foreground_priority, writer_lease
from exomem.cli_ops import OpError
from exomem.collection_store import (
    capability,
    connection,
    derived_rows,
    importer,
    owner,
    replica,
    runtime,
    tokens,
)
from exomem.collection_store.preview import preview_store
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope, owner_principal, request_scope

DERIVED = "88888888-8888-4888-8888-888888888888"
PATH = manifest_path().replace("Work", "Samples")
ROLLUP = "rollups:\n  daily:\n    bucket: day\n    timestamp: local_date\n    values:\n      value: [sum, max]\n"
TEXT = manifest_text(DERIVED, "Samples", FIELDS).replace(
    "lifecycle: active\n", "lifecycle: active\nview_mode: summary\nderived: true\n").removesuffix("---\n") + ROLLUP + "---\n"
# Four hourly samples on each of three November days: values in tenths, local Tallinn (+02:00) time.
DAYS = {
    f"samples/2026-11-{day:02d}.json": samples(
        (f"2026-11-{day:02d}", at(f"2026-11-{day:02d}", *((f"{hour:02d}:00", day * 10 + hour) for hour in range(8, 12))))
    )
    for day in (1, 2, 3)
}
ROWS = {"version": 1, "page": {"limit": 50}}
DAILY = {"version": 1, "group_by": [{"field": "local_date", "bucket": "day"}],
         "aggregates": {"total": {"op": "sum", "field": "value"}, "peak": {"op": "max", "field": "value"}}}


class _Crash(BaseException):
    """The process dies here: nothing settles the job and the writer handle is gone."""


def _archive(members):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, document in members.items():
            zipped.writestr(name, json.dumps(document))
    return data.getvalue()


def _preserve(root, data):
    receipt, _ = archive_members.preserve_members(
        root, guard=contextlib.nullcontext, scope="device", category="export", filename="days.zip",
        stream=io.BytesIO(data), max_bytes=len(data), verified="upload",
    )
    return receipt


def _projections(conn, cid=DERIVED):
    """The names of a collection's query projection tables in one file."""
    return [name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? "
                                             "ESCAPE '!' AND name NOT LIKE '%!_undo' ESCAPE '!'",
                                             (f"cq!_{cid.replace('-', '')}!_%",))]


def _rows_file(found, cid=DERIVED):
    """Each derived row's key and stored columns, by row id, read straight from a derived file."""
    with closing(sqlite3.connect(found)) as conn:
        [table] = _projections(conn, cid)
        columns = [row[1] for row in conn.execute(f"PRAGMA table_xinfo({table})") if row[6] == 0]
        return conn.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY row_id").fetchall()


def _digests_of_rebuilt_rows(store_path, cid=DERIVED):
    """Each logged member's digest recomputed from the rebuilt rows: a SHA-256 chained over the item key
    and payload hash of each of the member's rows, taken in row order, member by member."""
    from exomem.collection_store import native_identity, typed_storage

    with closing(connection.open_reader(store_path)) as main, \
            closing(sqlite3.connect(derived_rows.path(store_path))) as derived:
        goal = derived_rows.target(main, cid)
        log = main.execute("SELECT accepted,rows_digest FROM import_members WHERE collection_id=? ORDER BY seq",
                           (cid,)).fetchall()
        columns = ",".join((*goal.layout.value_columns, "r"))
        rows = derived.execute(f"SELECT item_key,{columns} FROM {goal.table} ORDER BY row_id").fetchall()
        found, offset = [], 0
        for accepted, logged in log:
            running = hashlib.sha256(b"").hexdigest()
            for key, *encoded in rows[offset:offset + accepted]:
                if goal.layout.encoding == typed_storage.TYPED_V2:
                    key = native_identity.decode(key, goal.layout.identity)
                payload = tokens.payload_hash(1, key, typed_storage.decode_row(goal.layout, encoded, connection=derived), "")
                running = hashlib.sha256(f"{running}\0{key}\0{payload}".encode()).hexdigest()
            found.append((running, logged))
            offset += accepted
        return found


def agent(writer, handle=None, **request):
    with request_scope(owner_principal(surface="mcp")), preview_store(writer.root, handle or writer.handle):
        return commands.op_record_memory(writer.root, action="import", collection=DERIVED, import_request=request)


def finish(writer, job, handle=None):
    for _ in range(64):
        run_jobs(writer, handle=handle, max_batches=8)  # bounded, so an import that never settles fails fast
        status = agent(writer, handle, mode="status", continuation=job["continuation"])
        if status["state"] != "running":
            return status
    raise AssertionError("the import did not settle")


def _query_refusal(root):
    try:
        call(root, "record_memory", action="query", collection=DERIVED, query=ROWS)
    except OpError as error:
        return error.code, error.details.get("progress")
    return None, None


def test_a_derived_import_keeps_rows_out_of_the_store_and_rebuilds_them_while_rollups_answer(
    service: Service,  # noqa: F811 - the imported fixture, by name
    monkeypatch,
    tmp_path,
):
    """Defect: a derived collection's rows land in the store, its replica or a portable backup, or its summary
    page counts the store's rows; or once the rows file is lost, a row query answers zero or partial rows
    instead of refusing with the rebuild's progress, a rollup stops answering, or the rebuilt rows differ from
    what the import log recorded."""
    root = service.root
    monkeypatch.setattr(capability, "RELEASED", ON)
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 3)
    store_path = connection.store_path(root)
    call(root, "record_memory", action="create", manifest_path=PATH, manifest_text=TEXT, why="derived samples")
    data = _archive(DAYS)
    manifest = _preserve(root, data)["path"]
    job = imported(root, collection=DERIVED, mode="start", source_ref=manifest, format="json-document",
                   members="samples/*", mapping=SAMPLES)
    _until(lambda: imported(root, collection=DERIVED, mode="status", continuation=job["continuation"])["state"]
           == "complete", 30)
    rows = call(root, "record_memory", action="query", collection=DERIVED, query=ROWS)["rows"]
    daily = call(root, "record_memory", action="query", collection=DERIVED, query=DAILY)
    assert len(rows) == 12 and daily["plan"]["strategy"] == "rollup"
    assert [group["total"] for group in daily["groups"]] == pytest.approx([7.8, 11.8, 15.8])
    with closing(connection.open_reader(store_path)) as reader:
        assert reader.execute("SELECT COUNT(*) FROM items WHERE collection_id=?", (DERIVED,)).fetchone() == (0,)
        assert reader.execute("SELECT MAX(row_count_after) FROM import_members WHERE collection_id=?",
                              (DERIVED,)).fetchone() == (12,)

    runtime.flush_for_export(root)
    page = root / PATH.replace("_collection.md", "Items/_summary.md")
    _until(lambda: page.exists() and "12 imported rows after 3 imported members" in page.read_text(), 30)
    portable = owner.backup(root, destination=tmp_path / "portable.sqlite")
    kept = owner.backup(root, destination=tmp_path / "kept.sqlite", include_derived=True)
    for snapshot in (replica.replica_path(root), tmp_path / "portable.sqlite"):
        with closing(sqlite3.connect(snapshot)) as copy:
            assert copy.execute("SELECT COUNT(*) FROM items WHERE collection_id=?", (DERIVED,)).fetchone() == (0,)
            assert all(copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,) for table in _projections(copy))
    before = _rows_file(derived_rows.path(store_path))
    assert "derived" not in portable and _rows_file(kept["derived"]["path"]) == before

    service.stop()
    for suffix in ("", "-wal", "-shm"):
        derived_rows.path(store_path).with_name(derived_rows.FILENAME + suffix).unlink(missing_ok=True)
    [blob] = (root / manifest).parent.glob("__exomem_raw_v1__members/*/" + hashlib.sha256(
        json.dumps(DAYS["samples/2026-11-02.json"]).encode()).hexdigest() + ".gz")
    blob.unlink()
    restarted = Service(root, writer_lease.start_server_lifecycle())
    try:
        _until(lambda: restarted.manager.status().get("collection_store", {}).get("status") == "admitted")
        # The rebuild replays the first member, then stops at the missing one: rows refuse, rollups answer.
        _until(lambda: (_query_refusal(root)[1] or {}).get("stop") is not None, 30)
        code, progress = _query_refusal(root)
        assert code == derived_rows.QUERY_REBUILDING
        assert progress["stop"]["code"] == derived_rows.SOURCE_MISSING and progress["stop"]["member"]["index"] == 1
        assert progress["members_applied"] == 1 and progress["members_logged"] == 3
        assert call(root, "record_memory", action="query", collection=DERIVED, query=DAILY)["groups"] == daily["groups"]
        assert _preserve(root, data)["archive"]["restored"] == 1  # re-attaching the export restores the blob
        _until(lambda: _query_refusal(root) == (None, None), 30)
        assert call(root, "record_memory", action="query", collection=DERIVED, query=ROWS)["rows"] == rows
        assert _rows_file(derived_rows.path(store_path)) == before
        assert all(rebuilt == logged for rebuilt, logged in _digests_of_rebuilt_rows(store_path))
        with pytest.raises(OpError) as refused:
            call(root, "record_memory", action="append", collection=DERIVED, item={"series": "hand", "at":
                 "2026-11-04T08:00:00Z"}, why="a hand edit")
        assert refused.value.code == derived_rows.COLLECTION_DERIVED
    finally:
        restarted.stop()


def _restart_into_a_slow_rebuild(running, monkeypatch):
    """Import one member of six batches, drop its rows file and restart the service, which rebuilds them.

    Each replayed batch takes 0.3 s, as a batch does beside CPU-bound requests, and the service's mutation
    timeout is one second: longer than one batch, shorter than the member's batches together. Returns the restarted
    service, the rows the import left and the start of each replayed batch.
    """
    root = running.root
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 3)
    store_path = connection.store_path(root)
    call(root, "record_memory", action="create", manifest_path=PATH, manifest_text=TEXT, why="derived samples")
    day = {"samples/2026-11-01.json": samples(("2026-11-01", at("2026-11-01", *(
        (f"{hour:02d}:00", hour) for hour in range(18)))))}
    job = imported(root, collection=DERIVED, mode="start", source_ref=_preserve(root, _archive(day))["path"],
                   format="json-document", members="samples/*", mapping=SAMPLES)
    _until(lambda: imported(root, collection=DERIVED, mode="status", continuation=job["continuation"])["state"]
           == "complete", 30)
    before = _rows_file(derived_rows.path(store_path))
    running.stop()
    for suffix in ("", "-wal", "-shm"):
        derived_rows.path(store_path).with_name(derived_rows.FILENAME + suffix).unlink(missing_ok=True)
    batches, apply = [], importer._derived_apply

    def slow_batch(*args, **kwargs):
        batches.append(time.monotonic())
        time.sleep(0.3)
        return apply(*args, **kwargs)

    monkeypatch.setattr(importer, "_derived_apply", slow_batch)
    monkeypatch.setenv("EXOMEM_MUTATION_TIMEOUT", "1")
    return Service(root, writer_lease.start_server_lifecycle()), before, batches


def _rebuilt(root):
    store_path = connection.store_path(root)
    with closing(connection.open_reader(store_path)) as reader:
        return derived_rows.rebuild_status(reader, store_path, DERIVED)["state"] == "ready"


@pytest.mark.parametrize("awaiting_bulk", [False, True], ids=["alone", "beside-a-request-awaiting-bulk-work"])
def test_a_writer_that_waits_for_the_boundary_during_a_rebuild_gets_it_after_one_batch(
    service: Service,  # noqa: F811 - the imported fixture, by name
    monkeypatch,
    awaiting_bulk,
):
    """Defect: a rebuild step kept the vault mutation boundary through every batch of its member while a
    writer waited, and the next step followed at once. On a service rebuilding a large derived collection,
    background commits waited out the mutation timeout and failed MUTATION_BUSY for the whole rebuild.
    A request that awaits bulk work, such as a governed write joining a corpus build, lets every bulk pass
    run, so a step that asked only about foreground work missed the writer queued on the boundary."""
    root = service.root
    cleanup_timeout = service.manager.config.mutation_timeout_seconds
    restarted, before, batches = _restart_into_a_slow_rebuild(service, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def joining():
        with foreground_priority.foreground(), foreground_priority.awaiting_bulk():
            entered.set()
            release.wait(10)

    thread = threading.Thread(target=joining)
    try:
        _until(lambda: batches, 30)
        if awaiting_bulk:
            thread.start()
            assert entered.wait(5)
        with restarted.manager.mutation_guard(root, operation="media_commit", holder_kind="background"):
            pass  # MUTATION_BUSY raises here
        release.set()
        _until(lambda: _rebuilt(root), 30)
        assert _rows_file(derived_rows.path(connection.store_path(root))) == before
        assert all(rebuilt == logged for rebuilt, logged in _digests_of_rebuilt_rows(connection.store_path(root)))
    finally:
        release.set()
        # The one-second budget tests writer fairness while cleanup retains the original fixture budget.
        restarted.stop(deadline=time.monotonic() + cleanup_timeout)


def test_a_request_in_flight_holds_the_rebuild_and_the_store_still_answers_at_once(
    service: Service,  # noqa: F811 - the imported fixture, by name
    monkeypatch,
):
    """Defect: the rebuild stepped on beside every request, which then shared the interpreter with it for its
    whole run, so requests ran many times slower while a large rebuild ran. The store thread now waits while
    a request runs, and that wait must not hold a store request, which would time out behind it."""
    root = service.root
    restarted, before, batches = _restart_into_a_slow_rebuild(service, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def request():  # foreground work that never reaches the store, such as an activation
        with foreground_priority.foreground():
            entered.set()
            release.wait(10)

    thread = threading.Thread(target=request)
    try:
        _until(lambda: batches, 30)
        thread.start()
        assert entered.wait(5)
        begun = len(batches)
        time.sleep(0.9)  # three batches' time
        assert len(batches) - begun <= 1  # at most the one it was starting when the request arrived
        # Within the store's one-second deadline, or COLLECTION_STORE_BUSY raises.
        assert titles(root, CID) == ["Canonical"]
        release.set()
        thread.join(10)
        _until(lambda: _rebuilt(root), 30)
        assert _rows_file(derived_rows.path(connection.store_path(root))) == before
    finally:
        release.set()
        restarted.stop()


@pytest.fixture
def derived(tmp_path, monkeypatch):
    """An in-process derived collection with an invented three-day export preserved for it."""
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 3)
    opened = []

    def build(name, days=DAYS, *, text=TEXT, mapping=SAMPLES):
        base = tmp_path / name
        root = base / "vault"
        (root / "Knowledge Base").mkdir(parents=True)
        (root / "Knowledge Base/log.md").write_text("# Existing log\n")
        handle = connection.open_writer(base / "collections.sqlite", lease_check=lambda: True)
        writer = CollectionWriter(root, handle)
        opened.append(writer)
        writer.create_collection(PATH, text, why="derived samples", scaffold=False)
        manifest = _preserve(root, _archive(days))["path"]
        job = agent(writer, mode="start", source_ref=manifest, format="json-document", members="samples/*",
                    mapping=mapping)
        return writer, job

    with library_scope():
        yield build
    for writer in opened:
        writer.handle.close()


@pytest.mark.query_deadline
def test_timeout_observation_separates_derived_metadata_without_changing_the_refusal(derived, monkeypatch, tmp_path):
    """A metadata admission timeout must retain its CPU and offsets without claiming SQL wait or changing results."""
    from exomem.query_engine import route, runtime

    writer, job = derived("metadata-timeout")
    assert finish(writer, job)["state"] == "complete"
    before = route.run(writer, DERIVED, ROWS, facade_profile="records")
    assert len(before["rows"]) == 12
    observe = runpy.run_path(str(
        Path(__file__).parents[1] / "scripts" / "_recipient_query_observer.py"
    ))["observe_query_timeouts"]
    log = tmp_path / "metadata-timeout.jsonl"
    real_assess, real_target = derived_rows.assess, derived_rows.target
    real_time = runtime.time
    real_flag, real_clock, real_check = derived_rows.is_derived, real_time.monotonic, runtime.ReadSession.check
    offset, errors = [0.0], []
    clock = SimpleNamespace(monotonic=lambda: real_clock() + offset[0], perf_counter=real_time.perf_counter,
                            thread_time=real_time.thread_time, process_time=real_time.process_time,
                            time_ns=real_time.time_ns)

    def assess(*args, **kwargs):
        result = real_assess(*args, **kwargs)
        if runtime._CURRENT_SESSION.get() is not None:
            offset[0] += 0.201  # Expire the unchanged interactive deadline after metadata admission, without sleeping.
        return result

    def check(session):
        try:
            return real_check(session)
        except runtime.QueryError as error:
            errors.append(error)
            raise

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "time", clock)
        patch.setitem(observe.__wrapped__.__globals__, "time", clock)
        patch.setattr(derived_rows, "assess", assess)
        patch.setattr(runtime.ReadSession, "check", check)
        with observe(log):
            with pytest.raises(runtime.QueryError, match="QUERY_TIMEOUT") as failure:
                route.run(writer, DERIVED, ROWS, facade_profile="records")
        assert failure.value is errors[0]
        assert derived_rows.assess is assess
        assert derived_rows.is_derived is real_flag
        assert derived_rows.target is real_target
        assert real_time.monotonic is real_clock
    [record] = [json.loads(line) for line in log.read_text().splitlines()]
    phases = record["phases"]
    parent = phases["derived.row_source"]
    assert record["timeout_ms"] == 200
    assert record["query_deadline_elapsed_ms"] >= 201
    assert record["largest_selected_values"] is None
    assert parent["thread_cpu_ms"] >= 0
    for name in ("is_derived", "target", "assess"):
        child = phases[f"derived.row_source.{name}"]
        assert child["calls"] == 1
        assert child["thread_cpu_ms"] >= 0
        assert 0 <= parent["first_started_after_query_ms"] <= child["first_started_after_query_ms"]
        assert child["last_finished_after_query_ms"] <= parent["last_finished_after_query_ms"]
    assert DERIVED not in log.read_text()
    assert "samples/" not in log.read_text()
    assert route.run(writer, DERIVED, ROWS, facade_profile="records")["rows"] == before["rows"]


def _mixed_manifest(*, location_key):
    """The mixed-types derived manifest; a location natural-key field is what makes a new derived collection hashed."""
    fields = MIXED_FIELDS if not location_key else MIXED_FIELDS.replace(
        "day: {type: date, required: true, sortable: true}",
        "day: {type: date, required: true, sortable: true, classification: location}")
    return manifest_text(DERIVED, "Mixed", fields).replace(
        "lifecycle: active\n", "lifecycle: active\nview_mode: summary\nderived: true\n").removesuffix("---\n") + (
        "rollups:\n  daily:\n    bucket: day\n    timestamp: local_date\n"
        "    values:\n      numeric: [sum, max]\n---\n")


@pytest.mark.parametrize("scheme", ["ordered-v1", "hashed-v1"])
def test_guarded_heterogeneous_rows_rebuild_with_exact_types_and_logged_member_digests(derived, scheme):
    """A rebuild drops the guard identity, rejects strings, or reconstructs a different scalar subtype, or
    its rows come back under other item keys than the import log recorded. A location natural-key field
    makes the collection hashed; every other new derived collection is ordered."""
    from dataclasses import replace

    from exomem import structured_collections as collections
    from exomem.collection_store.typed_storage import canonical_json

    text = _mixed_manifest(location_key=scheme == "hashed-v1")
    writer, job = derived("mixed", {"samples/mixed.json": MIXED_DOCUMENT}, text=text, mapping=MIXED_MAPPING)
    done = finish(writer, job)
    assert {name: done["rows"][name] for name in ("inserted", "updated", "unchanged", "imported", "duplicates", "rejected")} == {
        "inserted": 8, "updated": 0, "unchanged": 0, "imported": 8, "duplicates": 0, "rejected": 0}

    def rows(handle):
        with request_scope(owner_principal(surface="mcp")), preview_store(writer.root, handle):
            return commands.op_record_memory(writer.root, action="query", collection=DERIVED,
                query={"version": 1, "select": ["day", "position", "numeric", "text"],
                       "order_by": [{"field": "day"}, {"field": "position"}]})["rows"]

    expected = [{"day": day["day"], "position": position,
                 "text" if type(value) is str else "numeric": value}
                for day in MIXED_DOCUMENT["days"] for position, value in enumerate(day["values"])]
    assert canonical_json(rows(writer.handle)) == canonical_json(expected)
    parsed = replace(collections.parse_manifest_bytes(writer.root, writer.root / PATH, text.encode()),
                     item_key_scheme=scheme)
    assert writer.connection.execute("SELECT item_key_scheme FROM collections").fetchone() == (scheme,)
    expected_digest = hashlib.sha256(b"").hexdigest()
    for row in expected:
        values = {**row, "local_date": row["day"]}
        key = collections.derived_item_key(parsed, values)
        payload = tokens.payload_hash(1, key, values, "")
        expected_digest = hashlib.sha256(f"{expected_digest}\0{key}\0{payload}".encode()).hexdigest()
    assert writer.connection.execute("SELECT accepted,rejected,rows_digest,row_count_after FROM import_members").fetchall() == [
        (8, 0, expected_digest, 8)]
    daily = {"version": 1, "group_by": [{"field": "local_date", "bucket": "day"}],
             "aggregates": {"total": {"op": "sum", "field": "numeric"}, "peak": {"op": "max", "field": "numeric"}}}

    def buckets(handle):
        with request_scope(owner_principal(surface="mcp")), preview_store(writer.root, handle):
            result = commands.op_record_memory(writer.root, action="query", collection=DERIVED, query=daily)
            assert result["plan"]["strategy"] == "rollup"
            return result["groups"]

    expected_buckets = [{"local_date": "2026-11-01", "total": 17.25, "peak": 7},
                        {"local_date": "2026-11-02", "total": 3.0, "peak": 3}]
    assert buckets(writer.handle) == expected_buckets
    path = writer.handle.path
    before = _rows_file(derived_rows.path(path))
    writer.handle.close()
    for suffix in ("", "-wal", "-shm"):
        derived_rows.path(path).with_name(derived_rows.FILENAME + suffix).unlink(missing_ok=True)
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        restarted = CollectionWriter(writer.root, handle)
        with preview_store(writer.root, handle):
            for _ in range(16):
                outcome = derived_rows.step(writer.root, restarted)
                if outcome in {"done", "idle"}:
                    break
            else:
                raise AssertionError("the guarded rebuild did not settle")
        assert canonical_json(rows(handle)) == canonical_json(expected)
        assert buckets(handle) == expected_buckets
        assert _rows_file(derived_rows.path(path)) == before
        assert all(rebuilt == logged for rebuilt, logged in _digests_of_rebuilt_rows(path))


def test_a_descending_window_is_charged_its_index_interval_and_pages_in_source_order(derived):
    """Defect: no index runs in a descending order's direction, so the page's private copy read every row
    of the collection and was charged for all of them, however narrow the caller's window: a large
    collection was refused or ran past its deadline. A window on an index's leading field must be charged
    only its interval; a predicate that cannot seek an index must still be charged the whole collection,
    because the copy then reads every row. The windows filter a date and a number, so the copied operands
    must order and compare exactly as the stored values do."""
    writer, job = derived("descending", {"samples/mixed.json": MIXED_DOCUMENT},
                          text=_mixed_manifest(location_key=False), mapping=MIXED_MAPPING)
    finish(writer, job)
    source = [{"day": day["day"], "position": position, **({"numeric": value} if type(value) is not str else {})}
              for day in MIXED_DOCUMENT["days"] for position, value in enumerate(day["values"])]
    order = [{"field": "day", "direction": "desc"}, {"field": "position", "direction": "desc"}]

    def query(where, **extra):
        request = {"version": 1, "select": ["day", "position", "numeric"], "order_by": order, **extra}
        with request_scope(owner_principal(surface="mcp")), preview_store(writer.root, writer.handle):
            result = commands.op_record_memory(writer.root, action="query", collection=DERIVED,
                                               query={**request, **({"where": where} if where else {})})
        assert "error" not in result, result
        return result

    def charge(where):
        return query(where, mode="preview")["visit_charge"]

    def pages(where):
        rows, after = [], None
        while True:
            result = query(where, page={"limit": 2, **({"after": after} if after else {})})
            rows += result["rows"]
            if not result["has_more"]:
                return rows
            after = result["next_cursor"]

    def descending(rows):
        return sorted(rows, key=lambda row: (row["day"], row["position"]), reverse=True)

    day, numeric = {"field": "day", "op": "eq", "value": "2026-11-02"}, {"field": "numeric", "op": "gte", "value": 3}
    whole = charge(None)
    assert charge(day) < whole and charge(numeric) < whole
    assert charge({"field": "numeric", "op": "ne", "value": 7}) == whole
    assert pages(day) == descending([row for row in source if row["day"] == "2026-11-02"])
    assert pages(numeric) == descending([row for row in source if row.get("numeric", -1) >= 3])


def test_a_revision_13_store_upgrades_its_derived_collection_as_hashed_without_touching_its_rows(derived):
    """Defect: the upgrade to revision 14 re-keys or rebuilds a derived collection made before ordered keys,
    or marks it ordered, so its stored UUID keys no longer match the keys its imports compute.

    The revision-13 store is made by removing exactly what revision 14 adds from a hashed collection's store."""
    from dataclasses import replace

    from exomem import structured_collections as collections
    from exomem.collection_store import schema

    writer, job = derived("upgrade", {"samples/mixed.json": MIXED_DOCUMENT}, text=_mixed_manifest(location_key=True),
                          mapping=MIXED_MAPPING)
    assert finish(writer, job)["state"] == "complete"
    path = writer.handle.path
    file = derived_rows.path(path)
    before = _rows_file(file)
    writer.handle.close()
    with closing(sqlite3.connect(path, isolation_level=None)) as raw:
        for trigger in ("collections_item_key_scheme_fixed", "collections_ordered_keys_derived"):
            raw.execute(f"DROP TRIGGER {trigger}")
        raw.execute("ALTER TABLE collections DROP COLUMN item_key_scheme")
        raw.execute("UPDATE store_meta SET value='13' WHERE key=?", (schema.META_SCHEMA_VERSION,))
        raw.execute("UPDATE alembic_version SET version_num='13'")

    with connection.open_writer(path, lease_check=lambda: True) as handle:
        assert schema.schema_version(handle.connection) == schema.SCHEMA_VERSION == 14
        assert handle.connection.execute("SELECT collection_id,item_key_scheme FROM collections").fetchall() == [
            (DERIVED, "hashed-v1")]
        restarted = CollectionWriter(writer.root, handle)
        manifest = restarted._collection_manifest(restarted._collection_row(DERIVED))[0]
        assert manifest.item_key_scheme == "hashed-v1"
        with preview_store(writer.root, handle):
            assert derived_rows.step(writer.root, restarted) == "idle"  # nothing to rebuild
        assert derived_rows.rebuild_status(handle.connection, path, DERIVED)["state"] == "ready"
        assert _rows_file(file) == before
        # The keys the rows hold are the keys an import computes today.
        with closing(sqlite3.connect(file)) as derived_file:
            [table] = _projections(derived_file)
            held = {key for (key,) in derived_file.execute(f"SELECT item_key FROM {table}")}
        parsed = replace(collections.parse_manifest_bytes(writer.root, writer.root / PATH,
                         _mixed_manifest(location_key=True).encode()), item_key_scheme="hashed-v1")
        values = {"day": "2026-11-01", "position": 0, "local_date": "2026-11-01", "numeric": 7}
        assert uuid.UUID(collections.derived_item_key(parsed, values)).bytes in held


@pytest.mark.parametrize(
    ("change", "scheme"),
    [
        (lambda text: text, "ordered-v1"),
        (lambda text: text.replace("sortable: true}", "sortable: true, classification: location}", 1), "hashed-v1"),
        (lambda text: text.replace("day: {type: date,", "day: {type: enum, enum: [1, 2],", 1), "hashed-v1"),
        (lambda text: text.replace("derived: true\n", ""), "hashed-v1"),
    ],
    ids=["new derived collection", "location key field", "key field without an ordered encoding", "not derived"],
)
def test_a_new_collection_takes_ordered_keys_only_when_derived_and_its_key_can_carry_them(tmp_path, change, scheme):
    """Defect: a collection is created ordered although its key spells a location or has no ordered encoding,
    or a collection whose rows live in the store is created ordered."""
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    with connection.open_writer(tmp_path / "collections.sqlite", lease_check=lambda: True) as handle, library_scope():
        CollectionWriter(root, handle).create_collection(
            PATH, change(_mixed_manifest(location_key=False)), why="scheme", scaffold=False)
        assert handle.connection.execute("SELECT item_key_scheme FROM collections").fetchone() == (scheme,)


def test_an_ordered_collection_refuses_a_location_natural_key_by_revise_and_by_import_mapping(derived):
    """Defect: a key that spells a location reaches an item key, which every reader of the collection sees, by a
    classification added after creation: in a revised manifest or in an import mapping's coverage."""
    document = {"samples/mixed.json": MIXED_DOCUMENT}
    writer, job = derived("location", document, text=_mixed_manifest(location_key=False), mapping=MIXED_MAPPING)
    assert finish(writer, job)["state"] == "complete"
    assert writer.connection.execute("SELECT item_key_scheme FROM collections").fetchone() == ("ordered-v1",)

    text = _mixed_manifest(location_key=True)
    guards = writer.inspect_collection(DERIVED)["lifecycle_guards"]
    with pytest.raises(Exception) as refused:
        writer.revise_collection(DERIVED, manifest_text=text, why="locate the day", **guards)
    assert refused.value.code == derived_rows.COLLECTION_DERIVED
    assert refused.value.details["location_key_fields"] == ["day"]

    request = {"source_ref": _preserve(writer.root, _archive(document))["path"], "format": "json-document",
               "members": "samples/*", "mapping": MIXED_MAPPING}
    # Keys of a date (4 bytes) and an integer position (2 bytes for 0, 3 from 1 on): the preview reports them.
    assert agent(writer, mode="preview", **request)["identity"]["item_keys"] == {
        "scheme": "ordered-v1", "stored_key_bytes": {"min": 1, "max": 320},
        "sampled_key_bytes": {"rows": 8, "min": 6, "max": 7}}

    located = {**MIXED_MAPPING, "coverage": {"$.days[].day": {"classification": "location"}}}
    request = {"source_ref": _preserve(writer.root, _archive(document))["path"], "format": "json-document",
               "members": "samples/*", "mapping": located}
    [finding] = agent(writer, mode="preview", **request)["mapping"]["findings"]
    assert (finding["code"], finding["at"]) == ("IMPORT_MAPPING_INVALID", "mapping.coverage")
    with pytest.raises(OpError) as refused:
        agent(writer, mode="start", **request)
    assert refused.value.code == "IMPORT_MAPPING_INVALID" and refused.value.details["at"] == "mapping.coverage"


@pytest.mark.parametrize("scheme", ["ordered-v1", "hashed-v1"])
def test_a_recipient_who_cannot_see_a_natural_key_field_cannot_read_it_back_through_the_item_key(
        derived, monkeypatch, scheme):
    """Defect: an ordered item key spells the natural key, so a principal whose field plan withholds a
    natural-key field reads that value by selecting, filtering or sorting on `item_key`."""
    from test_collection_field_admission import GUEST

    from exomem import item_keys
    from exomem.query_engine import route

    if scheme == "hashed-v1":
        monkeypatch.setattr(CollectionWriter, "_new_item_key_scheme", lambda self, manifest: item_keys.HASHED_V1)
    writer, job = derived("recipient", DAYS, text=TEXT.replace("natural_key: [series, at]", "natural_key: [device, at]"))
    assert finish(writer, job)["state"] == "complete"
    assert writer.connection.execute("SELECT item_key_scheme FROM collections").fetchone() == (scheme,)

    def ask(who, **raw):
        with request_scope(who):
            return route.run(writer, DERIVED, {"version": 1, "page": {"limit": 3}, **raw}, facade_profile="records")

    with pytest.raises(Exception) as hidden:
        ask(GUEST, select=["device"])
    assert hidden.value.code == "QUERY_FIELD_UNAVAILABLE"
    owner_keys = [row["item_key"] for row in ask(owner_principal(), select=["item_key"])["rows"]]
    assert len(owner_keys) == 3
    if scheme == "hashed-v1":
        # The uuid spells nothing, so the recipient reads it as before.
        assert [row["item_key"] for row in ask(GUEST, select=["item_key"])["rows"]] == owner_keys
        return
    reached = {"select": {"select": ["item_key"]},
               "filter": {"where": {"field": "item_key", "op": "eq", "value": owner_keys[0]}},
               "sort": {"order_by": [{"field": "item_key"}]},
               "prefix": {"where": {"field": "item_key", "op": "startswith", "value": owner_keys[0][:4]}}}
    for name, raw in reached.items():
        with pytest.raises(Exception) as refused:
            ask(GUEST, **raw)
        assert (name, refused.value.code) == (name, "QUERY_FIELD_UNAVAILABLE")
    # Rows of an ordinary page carry no key, and the owner still reads, filters and sorts by it.
    assert all("item_key" not in row for row in ask(GUEST)["rows"])
    assert ask(owner_principal(), **reached["filter"], select=["item_key"])["rows"] == [{"item_key": owner_keys[0]}]
    assert [row["item_key"] for row in ask(owner_principal(), **reached["sort"], select=["item_key"])["rows"]] == owner_keys



def test_a_recipient_pages_past_long_ordered_keys_with_cursors_of_one_length(derived):
    """Defect: an ordered key over the cursor's old 128-character boundary refuses the next page, and a
    cursor's length tells a recipient how long the hidden natural key is."""
    from test_collection_field_admission import GUEST

    from exomem.query_engine import route

    devices = ["u", "unit-" + "y" * 20, "unit-" + "z" * 60, "unit-" + "w" * 100]
    members = {}
    for n, device in enumerate(devices):
        day = f"2026-11-0{n + 1}"
        members[f"samples/{day}.json"] = {**samples((day, at(day, ("08:00", 1), ("09:00", 2)))), "device": device}
    writer, job = derived("long-keys", members, text=TEXT.replace("natural_key: [series, at]", "natural_key: [device, at]"))
    assert finish(writer, job)["state"] == "complete"
    rows, cursor, lengths = [], None, set()
    while True:
        with request_scope(GUEST):
            page = route.run(writer, DERIVED, {"version": 1, "select": ["series"],
                                               "page": {"limit": 1, **({"after": cursor} if cursor else {})}},
                             facade_profile="records")
        rows += page["rows"]
        cursor = page["next_cursor"]
        if cursor is None:
            break
        lengths.add(len(cursor))
    assert len(rows) == 8
    assert len(lengths) == 1

def test_the_derived_connection_caps_its_wal_at_the_journal_size_limit(derived):
    """Defect: the derived file's connection loses the limit, so a large import leaves its whole WAL on disk,
    hundreds of megabytes beside the rows."""
    writer, _ = derived("journal")
    assert derived_rows.open_store(writer).conn.execute("PRAGMA journal_size_limit").fetchone() == (64 << 20,)


def test_a_rebuild_leaves_the_vault_commit_generation_where_it_was(derived):
    """Defect: every rebuild step advanced the vault's commit generation, though it writes only the derived
    rows file, which the store's import log rebuilds. A governed write compares the generation it read before
    the boundary with the one inside it, so for the whole rebuild every write missed its validity stamp and
    repeated its corpus walk inside the boundary."""
    writer, job = derived("generation")
    assert finish(writer, job)["state"] == "complete"
    root, path = writer.root, writer.handle.path
    writer.handle.close()
    for suffix in ("", "-wal", "-shm"):
        derived_rows.path(path).with_name(derived_rows.FILENAME + suffix).unlink(missing_ok=True)
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        restarted = CollectionWriter(root, handle)
        generation = writer_lease.read_commit_generation(root)
        with preview_store(root, handle):
            outcomes = [derived_rows.step(root, restarted) for _ in range(8)]
        assert "done" in outcomes
        assert writer_lease.read_commit_generation(root) == generation


@pytest.mark.parametrize("replay", [False, True], ids=["import", "rebuild"])
def test_slow_rows_keep_each_derived_boundary_hold_near_the_step_budget(derived, monkeypatch, replay):
    """Defect: a derived batch held the vault mutation boundary for as many rows as it took, whatever each
    cost. Beside a large derived file a row costs milliseconds, so an import or a rebuild held every
    foreground write and read for seconds per batch."""
    day = "2026-11-01"
    writer, job = derived("slow", {f"samples/{day}.json": samples((day, at(day, *(
        (f"{minute // 60:02d}:{minute % 60:02d}", minute) for minute in range(240)))))})
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 500)
    apply, transaction, holds = derived_rows.apply, derived_rows.Store.transaction, []

    def slow(conn, goal, items, **kwargs):
        time.sleep(0.006 * len(items))
        return apply(conn, goal, items, **kwargs)

    @contextlib.contextmanager
    def timed(self):
        started = time.monotonic()
        with transaction(self) as conn:
            yield conn
        holds.append(time.monotonic() - started)

    def slowed():
        monkeypatch.setattr(derived_rows, "apply", slow)
        monkeypatch.setattr(derived_rows.Store, "transaction", timed)

    path = writer.handle.path
    if not replay:
        slowed()
        assert finish(writer, job)["state"] == "complete"
        assert len(_rows_file(derived_rows.path(path))) == 240
    else:
        assert finish(writer, job)["state"] == "complete"
        before = _rows_file(derived_rows.path(path))
        writer.handle.close()
        for suffix in ("", "-wal", "-shm"):
            derived_rows.path(path).with_name(derived_rows.FILENAME + suffix).unlink(missing_ok=True)
        slowed()
        with connection.open_writer(path, lease_check=lambda: True) as handle:
            restarted = CollectionWriter(writer.root, handle)
            with preview_store(writer.root, handle):
                outcomes = [derived_rows.step(writer.root, restarted) for _ in range(64)]
            assert "done" in outcomes
        assert _rows_file(derived_rows.path(path)) == before
    assert all(rebuilt == logged for rebuilt, logged in _digests_of_rebuilt_rows(path))
    assert max(holds) < 0.75  # a 240-row batch at this pace held the boundary for about 1.5 s


def test_a_row_page_continued_after_another_batch_of_the_open_member_is_stale(derived):
    """Each batch of a derived import commits only to the derived file, and the store's freshness
    basis moves when a member ends. A continuation bound to the store's basis alone resumed after a
    batch that added rows, so paging silently skipped rows the collection held."""
    nine = {"samples/2026-11-01.json": samples(("2026-11-01", at("2026-11-01", *((f"{hour:02d}:00", hour)
                                                                                for hour in range(9)))))}
    writer, _ = derived("live", nine)

    def page(**after):
        with request_scope(owner_principal(surface="mcp")), preview_store(writer.root, writer.handle):
            return commands.op_record_memory(writer.root, action="query", collection=DERIVED,
                                             query={"version": 1, "page": {"limit": 2, **after}})

    run_jobs(writer, max_batches=1)
    first = page()
    run_jobs(writer, max_batches=1)
    logged = writer.connection.execute("SELECT count(*) FROM import_members WHERE collection_id=?",
                                       (DERIVED,)).fetchone()[0]

    assert first["has_more"] and logged == 0  # the member is still open
    with pytest.raises(OpError) as stale:
        page(after=first["next_cursor"])
    assert stale.value.code == "QUERY_CURSOR_STALE"


def test_a_writer_that_lost_the_lease_stages_no_derived_rows(derived):
    """Defect: derived writes checked only the writer's thread. A worker past a lost lease or a managed
    handoff still staged rows, and the new holder logged member counts built on them, for good."""
    writer, _ = derived("stale")
    writer.handle._lease_check = lambda: False
    with contextlib.suppress(Exception):
        run_jobs(writer, max_batches=1)

    assert not derived_rows.path(writer.handle.path).exists() or _rows_file(derived_rows.path(writer.handle.path)) == []


def test_writer_contention_keeps_a_derived_import_running_and_preserves_committed_rows(derived, monkeypatch):
    """A transient vault-writer refusal must not fail an acknowledged job or discard its checkpoint."""
    writer, job = derived("busy")
    run_jobs(writer, max_batches=1)
    before = agent(writer, mode="status", continuation=job["continuation"])
    rows = _rows_file(derived_rows.path(writer.handle.path))

    def busy_stage(self, *args, **kwargs):
        # The native mutation boundary refuses before the derived batch writes.
        raise OpError("MUTATION_BUSY", "another writer holds the vault", "retry later",
                      details={"status": "retryable", "committed": False})

    with monkeypatch.context() as patch:
        patch.setattr(derived_rows.Store, "stage", busy_stage)
        run_jobs(writer, max_batches=1)
    blocked = agent(writer, mode="status", continuation=job["continuation"])
    assert (blocked["state"], blocked["reason"]) == ("partial", "store_unavailable")
    assert blocked["error"]["code"] == "MUTATION_BUSY"
    assert blocked["next_position"] == before["next_position"]
    assert blocked["rows"] == before["rows"]
    assert _rows_file(derived_rows.path(writer.handle.path)) == rows

    done = finish(writer, job)
    assert done["state"] == "complete"
    assert (done["rows"]["imported"], done["rows"]["duplicates"], done["rows"]["rejected"]) == (12, 0, 0)
    assert len(_rows_file(derived_rows.path(writer.handle.path))) == 12
    assert all(rebuilt == logged for rebuilt, logged in _digests_of_rebuilt_rows(writer.handle.path))


def _imported_state(writer):
    """What a finished import leaves: the derived rows, the import log and the store's rollup buckets."""
    log = writer.connection.execute(
        "SELECT seq,member_index,member_sha256,accepted,rejected,rows_digest,row_count_after FROM import_members "
        "WHERE collection_id=? ORDER BY seq", (DERIVED,)).fetchall()
    buckets = writer.connection.execute(
        "SELECT b.bucket,b.groups,b.state_json FROM rollup_buckets b JOIN rollup_definitions d "
        "ON d.rollup_id=b.rollup_id WHERE d.collection_id=? ORDER BY b.bucket", (DERIVED,)).fetchall()
    return _rows_file(derived_rows.path(writer.handle.path)), log, buckets


# Where a process can die between the two files' commits (``derived_rows`` write order).
BOUNDARIES = {
    "after a batch's rows": ("stage", lambda record: not record["done"]),
    "after a member's last rows, before its log entry": ("stage", lambda record: record["done"]),
    "after the log entry, before the applied sequence": ("finalize", None),
}


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_a_crash_at_each_commit_boundary_recovers_to_the_rows_of_a_run_without_it(derived, monkeypatch, boundary):
    """Two connections cannot commit atomically: a process that dies between them must resume with no
    member's rows applied twice or lost, so the rows, import log and rollups equal a run without the crash."""
    writer, job = derived("clean")
    assert finish(writer, job)["state"] == "complete"
    clean = _imported_state(writer)

    writer, job = derived("crashed")
    method, when = BOUNDARIES[boundary]
    original = getattr(derived_rows.Store, method)
    crashed = []

    def crash(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if not crashed and (when is None or when(result)) and (method != "stage" or self.progress(DERIVED)["member"]):
            crashed.append(True)
            raise _Crash
        return result

    def crash_before(self, *args, **kwargs):
        if not crashed and len(writer.connection.execute("SELECT 1 FROM import_members").fetchall()) == 2:
            crashed.append(True)
            raise _Crash
        return original(self, *args, **kwargs)

    monkeypatch.setattr(derived_rows.Store, method, crash if method == "stage" else crash_before)
    with pytest.raises(_Crash):
        for _ in range(64):
            run_jobs(writer)
    monkeypatch.setattr(derived_rows.Store, method, original)
    root, path = writer.root, writer.handle.path
    writer.handle.close()
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        restarted = CollectionWriter(root, handle)
        assert finish(restarted, job, handle)["state"] == "complete"
        assert _imported_state(restarted) == clean


def test_a_reexport_that_lowers_a_peak_while_adding_samples_keeps_the_daily_max_exact(derived, monkeypatch):
    """Defect: a batch folds its rollup moves per bucket, and a re-export that lowers a day's peak while it adds
    a sample to that day, or lowers a day's only sample, leaves the daily max at a value no row holds: the
    recompute misses the batch's new member, or the emptied day keeps its old extremes."""
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 500)  # each member is one batch
    day1, day2 = "samples/2026-11-01.json", "samples/2026-11-02.json"
    first = {day1: samples(("2026-11-01", at("2026-11-01", ("08:00", 100), ("09:00", 500), ("10:00", 200)))),
             day2: samples(("2026-11-02", at("2026-11-02", ("08:00", 600))))}
    # A new 07:00 sample precedes the lowered 09:00 peak in the same batch; day 2's only sample drops.
    again = {day1: samples(("2026-11-01", at("2026-11-01", ("07:00", 400), ("08:00", 100), ("09:00", 50),
                                             ("10:00", 200)))),
             day2: samples(("2026-11-02", at("2026-11-02", ("08:00", 150))))}
    writer, job = derived("reexport", first)
    assert finish(writer, job)["state"] == "complete"
    job = agent(writer, mode="start", source_ref=_preserve(writer.root, _archive(again))["path"],
                format="json-document", members="samples/*", mapping=SAMPLES)
    assert finish(writer, job)["state"] == "complete"
    with request_scope(owner_principal(surface="mcp")), preview_store(writer.root, writer.handle):
        daily = commands.op_record_memory(writer.root, action="query", collection=DERIVED, query=DAILY)
    assert daily["plan"]["strategy"] == "rollup"
    assert [group["peak"] for group in daily["groups"]] == pytest.approx([40.0, 15.0])


def test_a_rebuild_refuses_an_export_manifest_that_lost_its_raw_protection_binding(derived):
    """Defect: a rebuild, which runs with no principal, trusts a manifest that a live import would refuse. Its
    bytes still hash to the logged manifest_sha256, but it is no longer kept under raw protection, so whoever
    may edit it could point the replay at other blobs."""
    writer, job = derived("unbound")
    assert finish(writer, job)["state"] == "complete"
    [binding] = writer.connection.execute("SELECT binding_json FROM import_jobs").fetchone()
    manifest = archive_members.read_manifest(writer.root, json.loads(binding)["source"]["ref"])
    root, path = writer.root, writer.handle.path
    writer.handle.close()
    for suffix in ("", "-wal", "-shm"):
        derived_rows.path(path).with_name(derived_rows.FILENAME + suffix).unlink(missing_ok=True)
    (root / manifest.companion).unlink()
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        restarted = CollectionWriter(root, handle)
        with preview_store(root, handle):
            assert derived_rows.step(root, restarted) == "stopped"
        stop = derived_rows.rebuild_status(handle.connection, path, DERIVED)["stop"]
    assert (stop["code"], stop["missing"], stop["reason"]) == (
        derived_rows.SOURCE_MISSING, "export manifest", "it lost its raw-protection binding")


def test_a_populated_derived_collection_keeps_its_fields_and_may_only_gain_optional_ones(derived):
    """Defect: the store checks a field revise against the rows it holds, and a derived collection holds
    none there, so any field change was accepted. A rebuild then replays the logged imports under the new
    fields, disagrees with the log and stops for good."""
    writer, job = derived("fields")
    assert finish(writer, job)["state"] == "complete"
    value = "    value: {type: number}\n"
    for revised in (TEXT.replace(value, "    value: {type: integer}\n"),
                    TEXT.replace(value, value + "    unit: {type: string, required: true}\n"),
                    TEXT.replace("    device: {type: string}\n", "")):
        guards = writer.inspect_collection(DERIVED)["lifecycle_guards"]
        with pytest.raises(Exception) as refused:
            writer.revise_collection(DERIVED, manifest_text=revised, why="fields", **guards)
        assert refused.value.code == derived_rows.COLLECTION_DERIVED

    guards = writer.inspect_collection(DERIVED)["lifecycle_guards"]
    writer.revise_collection(DERIVED, manifest_text=TEXT.replace(value, value + "    unit: {type: string}\n"),
                             why="an optional field", **guards)


def test_a_revise_cannot_convert_a_derived_collection_and_the_store_refuses_the_flag_change(derived):
    """Defect: a revise drops ``derived`` and the collection's rows, which live outside the store, are
    orphaned and its row writes reach a collection whose canonical content is its import log."""
    writer, _ = derived("revise")
    guards = writer.inspect_collection(DERIVED)["lifecycle_guards"]
    with pytest.raises(Exception) as refused:
        writer.revise_collection(DERIVED, manifest_text=TEXT.replace("derived: true\n", ""), why="convert",
                                 **guards)
    assert refused.value.code == "VIEW_MODE_CHANGE_UNSUPPORTED" and refused.value.details["derived"] is True
    with pytest.raises(sqlite3.IntegrityError, match="derived only at creation"):
        writer.connection.execute("UPDATE collections SET derived=0 WHERE collection_id=?", (DERIVED,))


# A stopped import reverts only the member it had open (design D3).

_FIRST = {"samples/2026-11-01.json": samples(("2026-11-01", at("2026-11-01", ("08:00", 100), ("09:00", 500),
                                                                ("10:00", 200)))),
          "samples/2026-11-02.json": samples(("2026-11-02", at("2026-11-02", ("08:00", 600))))}
# The re-export's open member adds 07:00 and lowers the 09:00 peak, so its first batch inserts and updates.
_AGAIN = {"samples/2026-11-01.json": samples(("2026-11-01", at("2026-11-01", ("07:00", 400), ("08:00", 90),
                                                                ("09:00", 50), ("10:00", 200)))),
          "samples/2026-11-02.json": _FIRST["samples/2026-11-02.json"]}


def _mirror(writer):
    """The derived file's rows, rollup mirrors and flagged counts, as a reader of that file sees them."""
    with closing(sqlite3.connect(derived_rows.path(writer.handle.path))) as conn:
        return (_rows_file(derived_rows.path(writer.handle.path)),
                conn.execute("SELECT * FROM rollup_buckets ORDER BY rollup_id,bucket,groups").fetchall(),
                conn.execute("SELECT * FROM rollup_members ORDER BY rollup_id,bucket,groups,row_id").fetchall(),
                conn.execute("SELECT * FROM derived_flagged ORDER BY rollup_id").fetchall())


def _cancelled_mid_member(derived, monkeypatch, name):
    """A collection holding a first export, then a re-export cancelled after the open member's first batch."""
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 3)
    writer, job = derived(name, _FIRST)
    assert finish(writer, job)["state"] == "complete"
    before = _mirror(writer)
    job = agent(writer, mode="start", source_ref=_preserve(writer.root, _archive(_AGAIN))["path"],
                format="json-document", members="samples/*", mapping=SAMPLES)
    run_jobs(writer, max_batches=1)
    assert derived_rows.Store.progress(derived_rows.open_store(writer), DERIVED)["restored"] == 2
    assert _mirror(writer) != before  # the open member moved rows, rollup buckets and members
    agent(writer, mode="cancel", continuation=job["continuation"])
    return writer, before


def _revert_to_rest(writer):
    with preview_store(writer.root, writer.handle):
        for _ in range(16):
            if derived_rows.step(writer.root, writer) == "idle":
                return
    raise AssertionError("the derived collection did not come to rest")


def _no_rebuild(monkeypatch):
    """Record each full rebuild's start: a revert never installs an empty rows table."""
    installs = []
    original = derived_rows.Store._install
    monkeypatch.setattr(derived_rows.Store, "_install",
                        lambda self, goal, *, state, **kwargs: (installs.append(state), original(self, goal, state=state, **kwargs))[1])
    return installs


def test_a_cancel_mid_member_restores_the_rows_buckets_and_members_without_a_rebuild(derived, monkeypatch):
    """Defect: a stop dropped the whole collection and replayed its import log, so cancelling the one
    open member of a re-export cost as much as importing every earlier member again."""
    writer, before = _cancelled_mid_member(derived, monkeypatch, "revert")
    installs = _no_rebuild(monkeypatch)

    _revert_to_rest(writer)

    assert _mirror(writer) == before
    assert installs == []
    assert derived_rows.rebuild_status(writer.connection, writer.handle.path, DERIVED)["state"] == "ready"
    assert all(rebuilt == logged for rebuilt, logged in _digests_of_rebuilt_rows(writer.handle.path))


def test_a_revert_cut_short_resumes_after_a_restart_to_the_same_rows(derived, monkeypatch):
    """Defect: a restart in the middle of a revert left the collection half reverted, with its earlier rows
    back and the cancelled member's new rows still there, or sent it to a full rebuild."""
    writer, before = _cancelled_mid_member(derived, monkeypatch, "resume")
    monkeypatch.setattr(derived_rows, "REBUILD_STEP_ROWS", 1)
    with preview_store(writer.root, writer.handle):
        assert derived_rows.step(writer.root, writer) == "advanced"
    status = derived_rows.rebuild_status(writer.connection, writer.handle.path, DERIVED)
    assert (status["state"], status["rows_remaining"] > 0) == ("reverting", True)
    root, path = writer.root, writer.handle.path
    writer.handle.close()
    installs = _no_rebuild(monkeypatch)

    with connection.open_writer(path, lease_check=lambda: True) as handle:
        restarted = CollectionWriter(root, handle)
        with preview_store(root, handle):
            outcomes = [derived_rows.step(root, restarted) for _ in range(64)]
        assert outcomes[-1] == "idle" and "done" in outcomes
        assert installs == []
        assert _mirror(restarted) == before


def test_a_member_opened_by_an_older_release_rebuilds_in_full(derived, monkeypatch):
    """Defect: a record without ``row_floor`` was reverted from an undo table its member never filled,
    which keeps the member's updates and drops nothing."""
    writer, before = _cancelled_mid_member(derived, monkeypatch, "older")
    with closing(sqlite3.connect(derived_rows.path(writer.handle.path), isolation_level=None)) as raw:
        record = json.loads(raw.execute("SELECT record_json FROM derived_progress").fetchone()[0])
        del record["row_floor"]
        raw.execute("UPDATE derived_progress SET record_json=?", (json.dumps(record),))
    installs = _no_rebuild(monkeypatch)

    _revert_to_rest(writer)

    assert installs == ["rebuilding"]
    assert _mirror(writer) == before


def test_an_undo_table_that_disagrees_with_the_member_count_rebuilds_in_full(derived, monkeypatch):
    """Defect: a revert trusted an undo table that lost a pre-image, and left that row at the cancelled
    member's value."""
    writer, before = _cancelled_mid_member(derived, monkeypatch, "short")
    with closing(sqlite3.connect(derived_rows.path(writer.handle.path), isolation_level=None)) as raw:
        raw.execute(f"DELETE FROM {derived_rows._undo_table(DERIVED)} WHERE row_id=(SELECT max(row_id) FROM "
                    f"{derived_rows._undo_table(DERIVED)})")
    installs = _no_rebuild(monkeypatch)

    _revert_to_rest(writer)

    assert installs == ["rebuilding"]
    assert _mirror(writer) == before
