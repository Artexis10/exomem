"""Preserved-source streaming import jobs (OpenSpec add-collection-query-engine Q7.3).

Each test names the defect only it catches. The importer is imported lazily so
that, before it exists, every case fails on its own missing behaviour. Rows come
from the shared invented wearable export; ``local_day`` is its independent
reference for which rows carry a source-local day and which are flagged.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest
from s1_export_fixture import daily_csv, daily_summaries, iter_exercises, local_day
from test_authorization_session_authority import NOW
from test_collection_store_governance import session
from test_collection_store_s1_gate import ab as ab
from test_collection_store_s1_gate import abc as abc
from test_collection_store_s1_gate import run_host
from test_collection_store_writer import CID as GATE_CID
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope

from exomem import commands, records
from exomem.cli_ops import OpError
from exomem.collection_store import connection, schema, typed_storage
from exomem.collection_store.preview import preview_store
from exomem.governance import authorization_custody, authorization_session_lifecycle
from exomem.governance import store as authority_store
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.record_memory import record_memory

CID = "6f1c0d1e-1f5a-4c43-9a43-0b6d3a6e8f21"
DAILY = "8a4b2c1d-3e5f-4a6b-8c7d-9e0f1a2b3c4d"
SOURCE = "Knowledge Base/Evidence/wearable/export/exercises.ndjson"
OTHER = RequestPrincipal(audience_id="other-principal", surface="mcp")
WORKOUT_FIELDS = (
    "  natural_key: [exercise_id]\n  fields:\n"
    "    exercise_id: {type: string, required: true}\n    kind: {type: string}\n"
    "    duration_s: {type: integer}\n    calories: {type: integer}\n    distance_m: {type: number}\n"
    "    started_at: {type: datetime}\n    utc_offset: {type: string}\n    local_date: {type: date}\n"
)
DAILY_FIELDS = (
    "  natural_key: [date]\n  fields:\n"
    "    date: {type: date, required: true}\n    steps: {type: integer}\n    resting_hr: {type: integer}\n"
)
MAPPING = {
    "fields": {
        "exercise_id": "id",
        "kind": "kind",
        "duration_s": "duration_s",
        "calories": "metrics.calories",
        "distance_m": "metrics.distance_m",
    },
    "time": {
        "from": [{"instant": "start_utc", "offset": "utc_offset"}, {"instant": "start"}],
        "instant": "started_at",
        "offset": "utc_offset",
        "local_date": "local_date",
    },
    "on_invalid": "skip",
}
DAILY_MAPPING = {
    "fields": {
        "steps": {"from": "steps", "type": "integer"},
        "resting_hr": {"from": "resting_hr", "type": "integer"},
    },
    "time": {"from": [{"date": "date"}], "local_date": "date"},
}


def importer():
    from exomem.collection_store import importer as module

    return module


def manifest_text(cid=CID, title="Workouts", fields=WORKOUT_FIELDS):
    return (
        f"---\ntype: collection\nexomem_id: {cid}\ntitle: {title}\nsemantic_profile: records\n"
        "collection_version: 1\nschema_version: 1\nlifecycle: active\nstorage:\n"
        "  strategy: markdown-items\n  source: Items\n  format_version: 1\n"
        f"item_schema:\n{fields}---\n"
    )


def ndjson(records):
    return b"".join(json.dumps(record, sort_keys=True).encode() + b"\n" for record in records)


def json_array(records):
    return json.dumps(list(records), sort_keys=True).encode()


def write_source(root, data, path=SOURCE):
    target = Path(root) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def setup(store, data=None, *, cid=CID, title="Workouts", fields=WORKOUT_FIELDS, path=SOURCE):
    store.create_collection(
        f"Knowledge Base/Records/{title}/_collection.md",
        manifest_text(cid, title, fields),
        why="fixture",
        scaffold=False,
    )
    for _ in range(16):
        if store.migrate_typed_encoding(cid) == "ready":
            break
    else:
        raise AssertionError("typed-v1 cutover did not finish")
    write_source(store.root, ndjson(iter_exercises()) if data is None else data, path)


def call(store, who=None, *, handle=None, cid=CID, **request):
    with request_scope(who or _external()), preview_store(store.root, handle or store.handle):
        return record_memory(store.root, action="import", collection=cid, import_request=request)


def start(store, who=None, **overrides):
    request = {
        "mode": "start",
        "source_ref": SOURCE,
        "format": "ndjson",
        "mapping": MAPPING,
        **overrides,
    }
    return call(store, who, **request)


def status(store, job, who=None, **kwargs):
    return call(store, who, mode="status", continuation=job["continuation"], **kwargs)


def run(store, *, handle=None, **kwargs):
    with preview_store(store.root, handle or store.handle):
        return importer().run_jobs(store.root, **kwargs)


def count(conn, sql="SELECT COUNT(*) FROM items WHERE collection_id=?", args=(CID,)):
    return conn.execute(sql, args).fetchone()[0]


def import_txns(conn):
    return count(conn, "SELECT COUNT(*) FROM txns WHERE request_id LIKE 'import:%'", ())


def small(monkeypatch, rows=40):
    monkeypatch.setattr(importer(), "MAX_BATCH_ROWS", rows)


def valid(records):
    return [record for record in records if local_day(record) is not None]


def refused(function, *args, **kwargs):
    with pytest.raises(OpError) as error:
        function(*args, **kwargs)
    return error.value


def release(store):
    for name in ("scopes/patterns.yaml", "rules/patterns-external.yaml"):
        (store.root / "Knowledge Base/_Governance" / name).unlink(missing_ok=True)


# Streaming, caps and formats


RSS_SCRIPT = r"""
import json, resource, sys
from pathlib import Path
from exomem import structured_collections as collections
from exomem.collection_store import importer
root, manifest, fmt, mapping, small, large = sys.argv[1:7]
parsed = collections.parse_manifest_bytes(Path(root), Path(root) / "Knowledge Base/Records/Workouts/_collection.md",
                                          manifest.encode())
plan = importer.compile_mapping(json.loads(mapping), parsed, fmt)
def drain(path):
    rows = 0
    with open(path, "rb") as handle:
        for batch in importer.iter_batches(handle, fmt, plan):
            rows += len(batch.rows)
    return rows
drain(small)
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
rows = drain(large)
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"rows": rows, "delta_kib": after - before}))
"""


def _write_export(path, count, fmt):
    """The fixture export with GPS-sized rows: each route carries 200 points."""
    with open(path, "wb") as handle:
        handle.write(b"[" if fmt == "json-array" else b"")
        for index, record in enumerate(iter_exercises(count)):
            record["route"] = [
                {
                    "lat": round(59.4 + step / 1e5, 6),
                    "lon": round(24.7 + step / 1e5, 6),
                    "t": step * 60,
                }
                for step in range(200)
            ]
            if fmt == "json-array" and index:
                handle.write(b",")
            handle.write(
                json.dumps(record, sort_keys=True).encode() + (b"\n" if fmt == "ndjson" else b"")
            )
        handle.write(b"]" if fmt == "json-array" else b"")


@pytest.mark.parametrize("fmt", ["ndjson", "json-array"])
def test_streaming_peak_memory_is_independent_of_total_rows(tmp_path, fmt):
    """A reader that loads the export, or a batch that keeps each row's decoded object, grows."""
    (tmp_path / "Knowledge Base").mkdir()
    small_path, large_path = tmp_path / "small", tmp_path / "large"
    _write_export(small_path, 20, fmt)
    _write_export(large_path, 10_000, fmt)
    assert large_path.stat().st_size > 64 << 20
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            RSS_SCRIPT,
            str(tmp_path),
            manifest_text(),
            fmt,
            json.dumps(MAPPING),
            str(small_path),
            str(large_path),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        env=os.environ,
        check=True,
    )
    measured = json.loads(completed.stdout.strip().splitlines()[-1])
    assert measured["rows"] == len(valid(iter_exercises(10_000)))
    assert measured["delta_kib"] < 16 * 1024, measured


ROW_CAP = {
    "ndjson": lambda big: ndjson([{"id": "a"}, {"id": "b", "notes": big}, {"id": "c"}]),
    "json-array": lambda big: json_array([{"id": "a"}, {"id": "b", "notes": big}, {"id": "c"}]),
    "csv": lambda big: f"id,notes\na,\nb,{big}\nc,\n".encode(),
}


@pytest.mark.parametrize("fmt", sorted(ROW_CAP))
def test_a_row_over_one_mebibyte_refuses_at_its_position(store, fmt):
    """A parser without a decoded-row cap buffers an unbounded record before mapping it."""
    setup(store, ROW_CAP[fmt]("x" * (1 << 20)))
    mapping = {"fields": {"exercise_id": "id"}}
    job = start(store, format=fmt, mapping=mapping)
    run(store)
    result = status(store, job)
    assert (result["state"], result["reason"]) == ("failed", "invalid_row")
    assert result["error"]["code"] == "IMPORT_ROW_TOO_LARGE" and result["error"]["row"] == 1
    assert result["rows"]["imported"] == 0 and count(store.connection) == 0


def test_a_governed_session_previews_and_starts_without_loading_the_source(store, monkeypatch):
    """A release check that fingerprints the source by reading it loads a large export whole."""
    setup(store, b"")
    _write_export(store.root / SOURCE, 3_000, "ndjson")
    size = (store.root / SOURCE).stat().st_size
    session(store, monkeypatch, paths="Unrelated/**")
    who = _session_at(store, NOW, 600)
    tracemalloc.start()
    try:
        preview = call(
            store, who, mode="preview", source_ref=SOURCE, format="ndjson", mapping=MAPPING
        )
        job = start(store, who)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert size > 24 << 20 and peak < 8 << 20, peak
    assert preview["source"]["bytes"] == size and job["state"] == "running"


def test_a_withheld_source_is_refused_unread_like_an_absent_one(store, monkeypatch):
    """Hashing before the release decision lets its timing tell a withheld file from an absent one."""
    setup(store, b"")
    _write_export(store.root / SOURCE, 500, "ndjson")
    session(store, monkeypatch, paths="Evidence/**")
    who = _session_at(store, NOW, 600)
    hashed = []
    monkeypatch.setattr(importer(), "_digest", lambda root, source: hashed.append(source.ref))
    request = {"mode": "preview", "format": "ndjson", "mapping": MAPPING}
    withheld = refused(call, store, who, source_ref=SOURCE, **request)
    absent = refused(call, store, who, source_ref=SOURCE.replace("exercises", "missing"), **request)
    assert (withheld.code, str(withheld), hashed) == ("IMPORT_SOURCE_NOT_FOUND", str(absent), [])


def test_a_stray_csv_quote_rejects_only_its_own_record(store):
    """Framing that toggles on every quote lets one stray quote swallow the records after it."""
    data = (
        b'date,steps,resting_hr\n2026-03-01,100,50\n2026-03-02,1"00,50\n'
        b"2026-03-03,300,52\n2026-03-04,400,53\n"
    )
    setup(store, data, cid=DAILY, title="Daily", fields=DAILY_FIELDS)
    job = call(
        store,
        cid=DAILY,
        mode="start",
        source_ref=SOURCE,
        format="csv",
        mapping={**DAILY_MAPPING, "on_invalid": "skip"},
    )
    run(store)
    result = call(store, cid=DAILY, mode="status", continuation=job["continuation"])
    assert (result["state"], result["rows"]["imported"], result["rows"]["rejected"]) == (
        "complete",
        3,
        1,
    )
    assert result["rejections"] == [
        {"row": 1, "byte": data.index(b"2026-03-02"), "code": "IMPORT_ROW_MALFORMED", "at": ""}
    ]
    stored = {
        values["date"]: values["steps"]
        for _, values in typed_storage.collection_values(store.connection, DAILY)
    }
    assert stored == {"2026-03-01": 100, "2026-03-03": 300, "2026-03-04": 400}


@pytest.mark.parametrize("fmt", ["ndjson", "json-array"])
def test_nesting_deeper_than_32_refuses_before_decoding(store, fmt):
    """Without a depth cap a hostile row reaches the recursive decoder and value walkers."""
    deep = {"id": "deep"}
    for _ in range(33):
        deep = {"n": deep}
    rows = [{"id": "ok", "n": 1}, deep]
    setup(store, ndjson(rows) if fmt == "ndjson" else json_array(rows))
    job = start(store, format=fmt, mapping={"fields": {"exercise_id": "id"}, "on_invalid": "skip"})
    run(store)
    result = status(store, job)
    assert result["state"] == "complete" and result["rows"]["imported"] == 1
    assert result["rejections"] == [
        {"row": 1, "byte": result["rejections"][0]["byte"], "code": "IMPORT_ROW_TOO_DEEP", "at": ""}
    ]


def test_batches_close_at_500_rows_or_4_mebibytes(tmp_path):
    """A batcher that only counts rows commits an unbounded transaction of wide rows."""
    from exomem import structured_collections as collections

    (tmp_path / "Knowledge Base").mkdir()
    manifest = collections.parse_manifest_bytes(
        tmp_path,
        tmp_path / "Knowledge Base/Records/Workouts/_collection.md",
        manifest_text().encode(),
    )
    plan = importer().compile_mapping({"fields": {"exercise_id": "id"}}, manifest, "ndjson")
    narrow = ndjson({"id": f"n{index}"} for index in range(1_201))
    wide = ndjson({"id": f"w{index}", "pad": "p" * 300_000} for index in range(30))
    for data, sizes in ((narrow, [500, 500, 201]), (wide, None)):
        path = tmp_path / "source"
        path.write_bytes(data)
        with open(path, "rb") as handle:
            batches = list(importer().iter_batches(handle, "ndjson", plan))
        assert all(len(batch.rows) <= 500 and batch.source_bytes <= 4 << 20 for batch in batches)
        if sizes:
            assert [len(batch.rows) for batch in batches] == sizes
        else:
            assert len(batches) > 2 and sum(len(batch.rows) for batch in batches) == 30
        assert batches[-1].checkpoint["byte"] == len(data)


# Lineage, authority and takeover


def test_job_binds_exact_source_receipt_and_target_lineage(store):
    """A job that re-resolves its source by path imports bytes it never bound."""
    setup(store)
    data = (store.root / SOURCE).read_bytes()
    job = start(store)
    binding = json.loads(
        store.connection.execute("SELECT binding_json FROM import_jobs").fetchone()[0]
    )
    assert binding["source"] == {
        "ref": SOURCE,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }
    assert binding["target"]["collection_id"] == CID
    assert binding["target"]["store_id"] == count(
        store.connection, "SELECT value FROM store_meta WHERE key='store_id'", ()
    )
    assert binding["principal"]["audience_id"] == "external"
    replacement = store.root / (SOURCE + ".new")
    replacement.write_bytes(ndjson(iter_exercises(96, seed=7)))
    os.replace(replacement, store.root / SOURCE)
    run(store)
    result = status(store, job)
    assert (result["state"], result["reason"], result["rows"]["imported"]) == (
        "partial",
        "authority_lost",
        0,
    )
    assert count(store.connection) == 0 and import_txns(store.connection) == 0


def test_source_release_revoked_between_batches_pauses_with_exact_counts(store, monkeypatch):
    """A job that proves source release only at start keeps importing after revocation."""
    setup(store)
    small(monkeypatch)
    job = start(store)
    assert run(store, max_batches=1)["batches"] == 1
    committed, txns = count(store.connection), import_txns(store.connection)
    write_scope(store.root, paths="Evidence/**")
    write_rule(store.root, ceiling=0)
    with request_scope(owner_principal(surface="mcp")):
        run(store)
    assert (count(store.connection), import_txns(store.connection)) == (committed, txns)
    assert refused(status, store, job).code == "IMPORT_JOB_NOT_FOUND"
    release(store)
    result = status(store, job)
    assert (result["state"], result["reason"]) == ("partial", "authority_lost")
    assert result["rows"]["imported"] == committed and result["batches"] == 1


def test_source_release_revoked_during_commit_rolls_back_the_batch(store, monkeypatch):
    """Checking release only before a batch commits rows read after revocation."""
    from exomem.collection_store.writer import CollectionWriter

    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store, max_batches=1)
    committed, txns = count(store.connection), import_txns(store.connection)
    write_item = CollectionWriter._write_item

    def revoke_mid_batch(self, *args, **kwargs):
        write_scope(store.root, paths="Evidence/**")
        write_rule(store.root, ceiling=0)
        return write_item(self, *args, **kwargs)

    monkeypatch.setattr(CollectionWriter, "_write_item", revoke_mid_batch)
    run(store, max_batches=1)
    monkeypatch.setattr(CollectionWriter, "_write_item", write_item)
    assert (count(store.connection), import_txns(store.connection)) == (committed, txns)
    release(store)
    result = status(store, job)
    assert (result["state"], result["reason"], result["rows"]["imported"]) == (
        "partial",
        "authority_lost",
        committed,
    )


def test_target_becoming_mixed_release_pauses_the_job(store, monkeypatch):
    """Checking only new-row release lets a job write into a collection its principal cannot see whole."""
    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store, max_batches=1)
    committed = count(store.connection)
    hidden = store.connection.execute(
        "SELECT view_path FROM items ORDER BY row_id LIMIT 1"
    ).fetchone()[0]
    write_scope(store.root, paths=hidden.removeprefix("Knowledge Base/"))
    write_rule(store.root, ceiling=0)
    run(store)
    assert count(store.connection) == committed
    result = status(store, job)
    assert (result["state"], result["reason"], result["rows"]["imported"]) == (
        "partial",
        "authority_lost",
        committed,
    )


def _session_at(store, at, ttl):
    """A further session for the governed principal; the fixture membership lasts 299 s."""
    custody = authorization_custody._load_authorization_custody_once()
    conn = authority_store.open_connection(store.root)
    try:
        opened = authorization_session_lifecycle.open_session(
            conn,
            custody=custody,
            principal_id="principal:person-1",
            issuer_family="mcp-oauth",
            now=at,
            ttl_seconds=ttl,
        )
    finally:
        conn.close()
    return RequestPrincipal(
        opened.context.principal_id,
        surface="mcp",
        issuer_family=opened.context.issuer_family,
        verified_authorization_session=opened.context,
    )


def test_grant_expiry_pauses_and_is_never_extended(store, monkeypatch):
    """A job that caches its initial grant keeps writing after the session expires."""
    setup(store)
    small(monkeypatch)
    session(store, monkeypatch, paths="Unrelated/**")
    who = _session_at(store, NOW, 60)
    job = start(store, who)
    run(store, max_batches=1)
    committed = count(store.connection)
    expired = who.verified_authorization_session.expires_at + 1
    monkeypatch.setattr("time.time", lambda: expired)
    run(store)
    run(store)
    assert count(store.connection) == committed
    renewed = _session_at(store, expired, 120)
    result = status(store, job, renewed)
    assert (result["state"], result["reason"], result["rows"]["imported"]) == (
        "partial",
        "authority_lost",
        committed,
    )
    assert who.verified_authorization_session.session_id not in json.dumps(result)
    call(store, renewed, mode="start", continuation=job["continuation"])
    run(store)
    assert status(store, job, renewed)["state"] == "complete"
    assert count(store.connection) == len(valid(iter_exercises()))
    receipts = " ".join(row[0] for row in store.connection.execute("SELECT receipt_json FROM txns"))
    assert who.verified_authorization_session.session_id not in receipts


def test_explicit_continuation_resumes_only_for_the_same_principal(store, monkeypatch):
    """Restored authority alone, or another principal's continuation, must not restart a paused job."""
    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store, max_batches=1)
    write_scope(store.root, paths="Evidence/**")
    write_rule(store.root, ceiling=0)
    run(store)
    release(store)
    assert run(store)["batches"] == 0
    assert status(store, job)["state"] == "partial"
    for other in (OTHER, owner_principal(surface="mcp")):
        assert (
            refused(call, store, other, mode="start", continuation=job["continuation"]).code
            == "IMPORT_JOB_NOT_FOUND"
        )
    assert call(store, mode="start", continuation=job["continuation"])["state"] == "running"
    run(store)
    result = status(store, job)
    assert result["state"] == "complete"
    assert result["rows"]["imported"] == count(store.connection) == len(valid(iter_exercises()))
    assert import_txns(store.connection) == result["batches"]


def test_another_principal_cannot_see_or_cancel_a_job(store, monkeypatch):
    """Job status keyed only by token discloses another audience's counts and lets it cancel."""
    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store, max_batches=1)
    absent = refused(call, store, OTHER, mode="status", continuation="import-job:" + "0" * 32)
    for mode in ("status", "cancel"):
        error = refused(call, store, OTHER, mode=mode, continuation=job["continuation"])
        assert (error.code, str(error), error.details) == (absent.code, str(absent), absent.details)
    result = status(store, job)
    assert (result["state"], result["batches"]) == ("running", 1)


@pytest.mark.parametrize("changed", [False, True], ids=["intact", "changed-in-place"])
def test_host_takeover_reproves_the_bound_source_hash(store, monkeypatch, changed):
    """A new writer host that trusts the old host's proof imports bytes the job never bound."""
    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store, max_batches=1)
    committed = count(store.connection)
    if changed:
        with open(store.root / SOURCE, "r+b") as handle:
            handle.seek(8)
            handle.write(b"Z")
    path = store.handle.path
    store.handle.close()
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        run(store, handle=handle)
        result = call(store, handle=handle, mode="status", continuation=job["continuation"])
        if changed:
            assert (result["state"], result["reason"]) == ("partial", "authority_lost")
            assert count(handle.connection) == result["rows"]["imported"] == committed
        else:
            assert result["state"] == "complete"
            assert count(handle.connection) == len(valid(iter_exercises()))


def _on(found, work):
    with (
        request_scope(_external()),
        preview_store(found.root, found.manager._collection_store),
        found.manager.mutation_guard(found.root),
    ):
        return work()


def _import_request(found, **body):
    return _on(
        found,
        lambda: record_memory(found.root, action="import", collection=GATE_CID, import_request=body),
    )


def _finish_on_host_b(found, continuation, rows):
    # A spawned host: the parent's monkeypatch does not reach it, so set the batch size here.
    importer().MAX_BATCH_ROWS = rows
    admitted = found.open()
    _on(found, lambda: importer().run_jobs(found.root))
    result = _import_request(found, mode="status", continuation=continuation)
    tenures = len(json.loads(found.meta()[schema.META_LINEAGE]))
    return admitted, tenures, result["state"], result["rows"]["imported"]


def test_a_job_continues_on_the_host_that_takes_over_its_store(abc, tmp_path, monkeypatch):
    """A job bound to the store's exact lineage stalls once a takeover appends a tenure."""
    source = "Knowledge Base/Evidence/gate/export/rows.ndjson"
    write_source(
        abc.root, ndjson({"title": f"Imported {i}", "count": i} for i in range(30)), source
    )
    small(monkeypatch, rows=10)
    job = _import_request(
        abc,
        mode="start",
        source_ref=source,
        format="ndjson",
        mapping={"fields": {"title": "title", "count": "count"}},
    )
    _on(abc, lambda: importer().run_jobs(abc.root, max_batches=1))
    abc.release()

    host_b = functools.partial(_finish_on_host_b, continuation=job["continuation"], rows=10)
    assert run_host(tmp_path, tmp_path / "state-b", "host-b", host_b) == (
        {"status": "admitted"},
        2,
        "complete",
        30,
    )


@pytest.mark.parametrize("change", ["store_id", "lineage"])
def test_a_job_pauses_when_its_store_is_replaced_or_forked(store, monkeypatch, change):
    """A job that accepts another store, or a forked lineage, writes where it never bound."""
    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store, max_batches=1)
    committed = count(store.connection)
    lineage = json.loads(
        count(store.connection, "SELECT value FROM store_meta WHERE key=?", (schema.META_LINEAGE,))
    )
    forked = [{**lineage[0], "instance_id": "00000000-0000-4000-8000-0000000000ee"}, *lineage[1:]]
    key, value = {
        "store_id": (schema.META_STORE_ID, "00000000-0000-4000-8000-0000000000ff"),
        "lineage": (schema.META_LINEAGE, json.dumps(forked, separators=(",", ":"))),
    }[change]
    with store.handle.transaction() as conn:
        conn.execute("UPDATE store_meta SET value=? WHERE key=?", (value, key))
    run(store)
    result = status(store, job)
    assert (result["state"], result["reason"], result["rows"]["imported"]) == (
        "partial",
        "authority_lost",
        committed,
    )


# Preview and mapping


def test_preview_reports_real_time_fields_and_never_guesses_a_day(store):
    """An inferring preview assigns flagged rows a host-zone or UTC day."""
    records = list(iter_exercises(300))
    setup(store, ndjson(records))
    mapping = {**MAPPING, "fields": {**MAPPING["fields"], "calories": "kind"}}
    before = tuple(store.connection.iterdump())
    result = call(store, mode="preview", source_ref=SOURCE, format="ndjson", mapping=mapping)
    assert tuple(store.connection.iterdump()) == before
    assert result["rows"]["sampled"] == 100 and result["rows"]["complete"] is False
    assert {"start", "start_utc", "utc_offset"} <= set(result["time"]["fields"])
    flagged = {row["row"]: row["code"] for row in result["time"]["flagged"]}
    expected = {
        index: record for index, record in enumerate(records[:100]) if local_day(record) is None
    }
    assert set(flagged) == set(expected)
    assert flagged[5] == "TIME_BASIS_UNZONED" and flagged[7] == "TIME_BASIS_ABSENT"
    assert all("local_date" not in row for row in result["time"]["flagged"])
    assert {"route": "array<object>", "place": "object"}.items() <= result["nested"].items()
    conflicts = {finding["at"]: finding for finding in result["mapping"]["findings"]}
    assert conflicts["mapping.fields.calories"]["expected"] == "integer"
    assert conflicts["mapping.fields.calories"]["allowed"] == ["string"]
    assert result["identity"] == {"natural_key": ["exercise_id"], "mapped": True}
    assert result["source"]["bytes"] == len(ndjson(records))


def test_preview_bounds_every_shape_section_and_says_so(store):
    """An unbounded preview echoes a wide row back many times over."""
    row = {
        "id": "x",
        **{f"day{i}": "2026-03-01" for i in range(4000)},
        "samples": {f"s{i}": {} for i in range(4000)},
        "k" * 300: 1,
    }
    setup(store, ndjson([row]))
    result = call(store, mode="preview", source_ref=SOURCE, format="ndjson", mapping=MAPPING)
    assert len(json.dumps(result).encode()) < 64 << 10
    assert result["truncated"] == {"fields": True, "nested": True, "time_fields": True}
    assert len(result["fields"]) == len(result["nested"]) == len(result["time"]["fields"]) == 64


@pytest.mark.parametrize(
    ("mapping", "at"),
    [
        ({"fields": {"exercise_id": "id", "nope": "kind"}}, "mapping.fields.nope"),
        ({"fields": {"exercise_id": "id"}, "transform": "lambda row: row"}, "mapping.transform"),
        (
            {"fields": {"exercise_id": {"from": "id", "type": "integer"}}},
            "mapping.fields.exercise_id.type",
        ),
        ({"fields": {"kind": "kind"}}, "mapping.fields"),
        (
            {"fields": {"exercise_id": "id"}, "time": {"from": [{"instant": "start"}]}},
            "mapping.time.local_date",
        ),
    ],
    ids=[
        "unknown-target",
        "executable-key",
        "json-coercion",
        "natural-key-unmapped",
        "time-without-day",
    ],
)
def test_bad_mapping_refuses_before_creating_a_job(store, mapping, at):
    """An unchecked mapping creates a job that fails late or runs caller-supplied logic."""
    setup(store)
    before = tuple(store.connection.iterdump())
    error = refused(start, store, mapping=mapping)
    assert error.code == "IMPORT_MAPPING_INVALID" and error.details["at"] == at
    assert {"expected", "allowed", "repair", "retryable"} <= set(error.details)
    assert tuple(store.connection.iterdump()) == before


@pytest.mark.parametrize(
    "reference",
    [
        "/etc/passwd",
        "Knowledge Base/Evidence/../Notes/private.md",
        "https://example.invalid/export.ndjson",
        "Knowledge Base/Notes/export.ndjson",
        "Knowledge Base/Evidence/wearable/missing.ndjson",
        SOURCE,
    ],
    ids=["absolute", "traversal", "url", "unpreserved-lane", "missing", "withheld"],
)
def test_source_ref_must_be_a_released_preserved_ref(store, reference):
    """An importer accepting paths or URLs reads files that ordinary authorization never released."""
    setup(store)
    write_source(store.root, b'{"id": "x"}\n', "Knowledge Base/Notes/export.ndjson")
    if reference == SOURCE:
        write_scope(store.root, paths="Evidence/**")
        write_rule(store.root, ceiling=0)
    for mode in ("preview", "start"):
        error = refused(
            call, store, mode=mode, source_ref=reference, format="ndjson", mapping=MAPPING
        )
        assert (error.code, error.details["at"]) == (
            "IMPORT_SOURCE_NOT_FOUND",
            "import_request.source_ref",
        )
    assert count(store.connection, "SELECT COUNT(*) FROM import_jobs", ()) == 0


# Policies, resume and crash safety


def test_stop_policy_fails_at_the_first_invalid_row_keeping_earlier_batches(store, monkeypatch):
    """A stop policy that commits the failing batch's earlier rows reports a position that skips nothing."""
    setup(store)
    small(monkeypatch, rows=4)
    job = start(
        store, mapping={key: value for key, value in MAPPING.items() if key != "on_invalid"}
    )
    run(store)
    result = status(store, job)
    assert (result["state"], result["reason"]) == ("failed", "invalid_row")
    assert result["error"]["row"] == 5 and result["error"]["code"] == "TIME_BASIS_UNZONED"
    assert (
        result["next_position"]["row"] == 4
        and result["rows"]["imported"] == count(store.connection) == 4
    )


def test_skip_policy_records_every_rejected_position_and_reason(store, monkeypatch):
    """A skip policy that drops rows silently presents a partial import as complete."""
    records = list(iter_exercises())
    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store)
    result = status(store, job)
    flagged = [index for index, record in enumerate(records) if local_day(record) is None]
    assert result["state"] == "complete" and result["rows"]["rejected"] == len(flagged)
    assert [row["row"] for row in result["rejections"]] == flagged
    assert {row["code"] for row in result["rejections"]} == {
        "TIME_BASIS_UNZONED",
        "TIME_BASIS_ABSENT",
    }
    assert result["rows"]["imported"] == count(store.connection) == len(records) - len(flagged)


FORMATS = {
    "ndjson": (lambda: ndjson(iter_exercises()), MAPPING, 40),
    "json-array": (lambda: json_array(iter_exercises()), MAPPING, 40),
    "csv": (lambda: daily_csv(), DAILY_MAPPING, 4),
}


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_continuation_resumes_from_the_bound_hash_and_exact_parser_position(
    store, monkeypatch, fmt
):
    """Resuming from row count or re-parsing from the start skips or duplicates rows."""
    data, mapping, rows = FORMATS[fmt]
    daily = fmt == "csv"
    cid = DAILY if daily else CID
    setup(
        store,
        data(),
        cid=cid,
        title="Daily" if daily else "Workouts",
        fields=DAILY_FIELDS if daily else WORKOUT_FIELDS,
    )
    small(monkeypatch, rows)
    job = call(store, cid=cid, mode="start", source_ref=SOURCE, format=fmt, mapping=mapping)
    run(store, max_batches=1)
    import time

    later = time.time() + importer().JOB_WINDOW_SECONDS + 1
    monkeypatch.setattr("time.time", lambda: later)
    run(store)
    paused = call(store, cid=cid, mode="status", continuation=job["continuation"])
    assert (paused["state"], paused["reason"], paused["rows"]["remaining"]) == (
        "partial",
        "time_cap",
        None,
    )
    assert paused["source_bytes"]["consumed"] < paused["source_bytes"]["total"]
    call(store, cid=cid, mode="start", continuation=job["continuation"])
    run(store)
    done = call(store, cid=cid, mode="status", continuation=job["continuation"])
    assert (done["state"], done["rows"]["remaining"]) == ("complete", 0)
    stored = dict(typed_storage.collection_values(store.connection, cid))
    if daily:
        expected = {row["date"]: (row["steps"], row["resting_hr"]) for row in daily_summaries()}
        actual = {
            values["date"]: (values["steps"], values["resting_hr"]) for values in stored.values()
        }
    else:
        expected = {
            record["id"]: (local_day(record).isoformat(), record["metrics"]["calories"])
            for record in valid(iter_exercises())
        }
        actual = {
            values["exercise_id"]: (values["local_date"], values["calories"])
            for values in stored.values()
        }
    assert actual == expected and done["rows"]["imported"] == len(expected)


def test_uncertain_commit_is_settled_once_without_duplicate_effects(store, monkeypatch):
    """A runner that retries a batch whose commit outcome it lost duplicates rows and audit."""
    from exomem import writer_lease

    setup(store)
    small(monkeypatch)
    job = start(store)
    mark = writer_lease.mark_active_mutation_committed
    calls = []

    def lose_second_response():
        calls.append(1)
        mark()
        if len(calls) == 2:
            raise ConnectionResetError("response lost after commit")

    monkeypatch.setattr(writer_lease, "mark_active_mutation_committed", lose_second_response)
    assert run(store)["errors"] == 1
    run(store)
    result = status(store, job)
    assert result["state"] == "complete"
    expected = len(valid(iter_exercises()))
    assert result["rows"]["imported"] == count(store.connection) == expected
    assert count(store.connection, "SELECT COUNT(*) FROM audit_effects", ()) == expected
    assert import_txns(store.connection) == result["batches"] == 3


class _Crash(BaseException):
    """Process death mid-batch: no handler in the process sees it."""


def test_crash_inside_a_batch_leaves_it_absent_and_resumes_once(store, monkeypatch):
    """A checkpoint advanced outside the batch transaction skips or repeats rows after a crash."""
    from exomem.collection_store.writer import CollectionWriter

    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store, max_batches=1)
    committed, txns = count(store.connection), import_txns(store.connection)
    write_item = CollectionWriter._write_item
    writes = []

    def crash(self, *args, **kwargs):
        writes.append(1)
        if len(writes) == 5:
            raise _Crash
        return write_item(self, *args, **kwargs)

    monkeypatch.setattr(CollectionWriter, "_write_item", crash)
    with pytest.raises(_Crash):
        run(store, max_batches=1)
    monkeypatch.setattr(CollectionWriter, "_write_item", write_item)
    assert (count(store.connection), import_txns(store.connection)) == (committed, txns)
    assert status(store, job)["next_position"]["row"] == 40
    run(store)
    result = status(store, job)
    assert result["state"] == "complete" and count(store.connection) == len(valid(iter_exercises()))


def test_an_unexpected_batch_error_fails_the_job_with_a_typed_code(store, monkeypatch):
    """A runner that swallows a batch error retries it every tick while reporting running."""
    from exomem.collection_store.writer import CollectionWriter

    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store, max_batches=1)
    committed = count(store.connection)

    def broken(self, *args, **kwargs):
        raise RuntimeError("defect inside the writer")

    monkeypatch.setattr(CollectionWriter, "_write_item", broken)
    assert run(store)["errors"] == 1
    result = status(store, job)
    assert (result["state"], result["reason"], result["error"]["code"]) == (
        "failed",
        "batch_error",
        "IMPORT_BATCH_FAILED",
    )
    assert count(store.connection) == result["rows"]["imported"] == committed
    assert run(store)["errors"] == 0


def test_a_store_refusal_pauses_the_job_until_the_store_accepts_writes(store, monkeypatch):
    """A runner that swallows a store refusal shows a stuck job as running with no reason."""
    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store, max_batches=1)
    committed = count(store.connection)
    store.connection.execute("INSERT INTO store_meta(key,value) VALUES ('diverged','1')")
    assert run(store)["errors"] == 1
    paused = status(store, job)
    assert (paused["state"], paused["reason"], paused["error"]["code"]) == (
        "partial",
        "store_unavailable",
        "COLLECTION_STORE_DIVERGED",
    )
    assert count(store.connection) == paused["rows"]["imported"] == committed
    store.connection.execute("DELETE FROM store_meta WHERE key='diverged'")
    run(store)
    result = status(store, job)
    assert (result["state"], result["error"]) == ("complete", None)
    assert result["rows"]["imported"] == count(store.connection) == len(valid(iter_exercises()))


def test_duplicate_keys_resolve_to_the_last_occurrence_whatever_the_batching(store, monkeypatch):
    """Batch boundaries decide which duplicate wins, and whether a stop-policy job fails."""
    rows = [{"id": f"r{i}", "kind": "first"} for i in range(12)]
    rows.insert(3, {"id": "r1", "kind": "second"})
    mapping = {"fields": {"exercise_id": "id", "kind": "kind"}, "on_invalid": "stop"}
    outcomes = []
    for cid, title, size in ((CID, "Workouts", 40), (DAILY, "Workouts B", 2)):
        setup(store, ndjson(rows), cid=cid, title=title)
        small(monkeypatch, rows=size)
        job = start(store, cid=cid, mapping=mapping)
        run(store)
        result = status(store, job, cid=cid)
        stored = {
            values["exercise_id"]: values["kind"]
            for _, values in typed_storage.collection_values(store.connection, cid)
        }
        outcomes.append((result["state"], result["rows"], stored))
    assert outcomes[0] == outcomes[1]
    state, counts, stored = outcomes[0]
    assert state == "complete" and stored["r1"] == "second" and len(stored) == 12
    assert (counts["imported"], counts["duplicates"], counts["rejected"]) == (12, 1, 0)


def test_an_identical_start_replays_a_live_job_and_restarts_a_cancelled_one(store, monkeypatch):
    """A retried start that binds a second job duplicates work; replaying a cancelled one dead-ends."""
    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store, max_batches=1)
    again = start(store)
    assert (again["continuation"], again["replayed"]) == (job["continuation"], True)
    call(store, mode="cancel", continuation=job["continuation"])
    fresh = start(store)
    assert fresh["continuation"] != job["continuation"] and "replayed" not in fresh
    run(store)
    assert status(store, fresh)["state"] == "complete"
    # A long-lived writer would otherwise hold a proof for every job it ever ran.
    assert store.handle.import_proofs == {}


def test_cancel_stops_between_batches_and_cannot_be_resumed(store, monkeypatch):
    """A cancel that does not persist lets the service keep importing after the owner stopped it."""
    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store, max_batches=1)
    committed = count(store.connection)
    result = call(store, mode="cancel", continuation=job["continuation"])
    assert (result["state"], result["reason"], result["rows"]["imported"]) == (
        "partial",
        "cancelled",
        committed,
    )
    assert run(store)["batches"] == 0 and count(store.connection) == committed
    assert (
        refused(call, store, mode="start", continuation=job["continuation"]).code
        == "IMPORT_JOB_NOT_RESUMABLE"
    )


def test_each_committed_batch_has_one_value_free_receipt(store, monkeypatch):
    """Batch receipts that are missing, merged or carry values lose the audit trail or leak data."""
    setup(store)
    small(monkeypatch)
    job = start(store)
    run(store)
    result = status(store, job)
    receipts = store.connection.execute(
        "SELECT request_id, receipt_json FROM txns WHERE request_id LIKE 'import:%' ORDER BY txn_id"
    ).fetchall()
    assert len(receipts) == result["batches"] == 3
    assert [request.rsplit(":", 1)[1] for request, _ in receipts] == ["0", "1", "2"]
    bodies = [json.loads(receipt) for _, receipt in receipts]
    assert sum(body["counts"]["inserted"] for body in bodies) == result["rows"]["imported"]
    assert result["last_receipt"]["transition_id"] == bodies[-1]["first_transition"]
    assert "ex-000001" not in " ".join(receipt for _, receipt in receipts)


# Agent surface


def test_import_is_unavailable_for_file_authoritative_collections(tmp_path):
    """An import route reachable outside the store bypasses its job, lineage and batch contract."""
    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base/log.md").write_text("# Log\n")
    records.create_collection(tmp_path, "Knowledge Base/Records/Workouts/_collection.md", manifest_text(),
                              why="file collection", scaffold=True)
    error = refused(
        record_memory,
        tmp_path,
        action="import",
        collection=CID,
        import_request={"mode": "status", "continuation": "import-job:" + "0" * 32},
    )
    assert error.code == "IMPORT_UNAVAILABLE"


def test_owner_journey_through_preserve_and_record_memory(store, monkeypatch):
    """The agent route must preserve, preview, start, observe, read back and pause honestly."""
    small(monkeypatch)
    setup(store, b"")
    owner = owner_principal(surface="mcp")
    records = list(iter_exercises(192))
    with request_scope(owner):
        first = commands.op_preserve_evidence(
            store.root, "wearable", "export", "first.ndjson", ndjson(records[:96]).decode()
        )
        second = commands.op_preserve_evidence(
            store.root, "wearable", "export", "second.ndjson", ndjson(records[96:]).decode()
        )

    def agent(**request):
        with request_scope(owner), preview_store(store.root, store.handle):
            return commands.op_record_memory(
                store.root, action="import", collection=CID, import_request=request
            )

    with request_scope(owner), preview_store(store.root, store.handle):
        described = commands.op_record_memory(store.root, action="describe")["import"]
    assert set(described["import_request"]) == {
        "mode",
        "source_ref",
        "format",
        "mapping",
        "continuation",
    }
    preview = agent(mode="preview", source_ref=first["path"], format="ndjson", mapping=MAPPING)
    assert preview["mapping"]["findings"] == [] and preview["rows"]["sampled"] == 96
    job = agent(mode="start", source_ref=first["path"], format="ndjson", mapping=MAPPING)
    assert job["state"] == "running" and job["rows"]["remaining"] is None
    for _ in range(8):
        run(store)
        if agent(mode="status", continuation=job["continuation"])["state"] != "running":
            break
    done = agent(mode="status", continuation=job["continuation"])
    assert (done["state"], done["rows"]["imported"]) == ("complete", len(valid(records[:96])))
    with request_scope(owner), preview_store(store.root, store.handle):
        inspected = commands.op_record_memory(store.root, action="inspect", collection=CID)
    assert inspected["coverage"]["committed"] == len(valid(records[:96]))
    paused = agent(mode="start", source_ref=second["path"], format="ndjson", mapping=MAPPING)
    run(store, max_batches=1)
    access = store.root / "Knowledge Base/_access.yaml"
    access.write_text('excluded: ["Evidence/wearable"]\n')
    run(store)
    assert (
        refused(agent, mode="status", continuation=paused["continuation"]).code
        == "IMPORT_JOB_NOT_FOUND"
    )
    access.unlink()
    result = agent(mode="status", continuation=paused["continuation"])
    first_batch = len(valid(records[96:136]))
    assert (result["state"], result["reason"], result["rows"]["imported"]) == (
        "partial",
        "authority_lost",
        first_batch,
    )
    assert count(store.connection) == len(valid(records[:96])) + first_batch
