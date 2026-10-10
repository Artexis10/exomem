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
import sqlite3
import zipfile
from contextlib import closing

import pytest
from test_collection_store_import_exports import FIELDS, SAMPLES, at, samples
from test_collection_store_importer import manifest_text
from test_collection_store_importer import run as run_jobs
from test_collection_store_s1_gate import _until
from test_collection_store_service import (  # noqa: F401 - the service fixture
    ON,
    Service,
    call,
    imported,
    service,
)
from test_collection_store_writer import manifest_path

from exomem import archive_members, commands, writer_lease
from exomem.cli_ops import OpError
from exomem.collection_store import (
    capability,
    connection,
    derived_rows,
    importer,
    owner,
    replica,
    schema,
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
                                             "ESCAPE '!'", (f"cq!_{cid.replace('-', '')}!_%",))]


def _rows_file(found, cid=DERIVED):
    """Each derived row's key and stored columns, by row id, read straight from a derived file."""
    with closing(sqlite3.connect(found)) as conn:
        [table] = _projections(conn, cid)
        columns = [row[1] for row in conn.execute(f"PRAGMA table_xinfo({table})") if row[6] == 0]
        return conn.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY row_id").fetchall()


def _digests_of_rebuilt_rows(store_path, cid=DERIVED):
    """Each logged member's digest recomputed from the rebuilt rows: a SHA-256 chained over the item key
    and payload hash of each of the member's rows, taken in row order, member by member."""
    with closing(connection.open_reader(store_path)) as main, \
            closing(sqlite3.connect(derived_rows.path(store_path))) as derived:
        goal = derived_rows.target(main, cid)
        log = main.execute("SELECT accepted,rows_digest FROM import_members WHERE collection_id=? ORDER BY seq",
                           (cid,)).fetchall()
        columns = ",".join((*goal.layout.value_columns, "r"))
        rows = derived.execute(f"SELECT item_key,{columns} FROM {goal.table} ORDER BY row_id").fetchall()
    from exomem.collection_store import typed_storage

    found, offset = [], 0
    for accepted, logged in log:
        running = hashlib.sha256(b"").hexdigest()
        for key, *encoded in rows[offset:offset + accepted]:
            payload = tokens.payload_hash(1, key, typed_storage.decode_row(goal.layout, encoded), "")
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

    def published():
        with closing(connection.open_reader(store_path)) as reader:
            meta = dict(reader.execute("SELECT key,value FROM store_meta"))
        head = json.loads(meta.get(schema.META_PUBLISHED_REPLICA_HEAD) or "null")
        return head is not None and str(head["commit_seq"]) == meta[schema.META_COMMIT_SEQ]

    _until(published, 30)
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


@pytest.fixture
def derived(tmp_path, monkeypatch):
    """An in-process derived collection with an invented three-day export preserved for it."""
    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 3)
    opened = []

    def build(name, days=DAYS):
        base = tmp_path / name
        root = base / "vault"
        (root / "Knowledge Base").mkdir(parents=True)
        (root / "Knowledge Base/log.md").write_text("# Existing log\n")
        handle = connection.open_writer(base / "collections.sqlite", lease_check=lambda: True)
        writer = CollectionWriter(root, handle)
        opened.append(writer)
        writer.create_collection(PATH, TEXT, why="derived samples", scaffold=False)
        manifest = _preserve(root, _archive(days))["path"]
        job = agent(writer, mode="start", source_ref=manifest, format="json-document", members="samples/*",
                    mapping=SAMPLES)
        return writer, job

    with library_scope():
        yield build
    for writer in opened:
        writer.handle.close()


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
