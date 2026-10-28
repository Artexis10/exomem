"""Imports from export members and nested JSON documents (OpenSpec bring-in-large-exports §3).

Each test names the defect only it catches. An export here is an invented zip that the
real archive expansion preserves as members: an owner-only manifest and one gzip blob
per distinct member, pooled across the family. The data is invented: a device's days of
unzoned local samples and a positional series, crossing the Europe/Tallinn autumn fold
(2026-10-25, 04:00 EEST back to 03:00 EET) and spring gap (2026-03-29, 03:00 EET to
04:00 EEST). Every expected instant below is computed by hand from those two rules.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import gzip
import hashlib
import io
import json
import zipfile
import zoneinfo
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import tzdata
from test_collection_store_importer import CID, call, count, manifest_text, refused, release, setup
from test_collection_store_importer import run as run_jobs
from test_collection_store_s1_gate import _until
from test_collection_store_service import (  # noqa: F401 - the service fixture
    ON,
    Service,
    imported,
    service,
)
from test_collection_store_service import call as served
from test_collection_store_writer import manifest_path
from test_collection_store_writer import store as store
from test_governance_egress import _external

from exomem import __version__, archive_members, commands, records
from exomem import structured_collections as collections
from exomem.collection_store import capability, connection, importer, tokens, typed_storage
from exomem.collection_store.preview import preview_store
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import owner_principal, request_scope

OWNER = owner_principal(surface="mcp")
ZONE = "Europe/Tallinn"
FIELDS = (
    "  natural_key: [series, at]\n  fields:\n"
    "    series: {type: string, required: true}\n    at: {type: datetime, required: true}\n"
    "    utc_offset: {type: string}\n    local_date: {type: date}\n    day: {type: date}\n"
    "    device: {type: string}\n    value: {type: number}\n"
)
TIMES = {"instant": "at", "offset": "utc_offset", "local_date": "local_date"}
SAMPLES = {
    "rows": "days[].samples[]",
    "fields": {
        "series": {"const": "samples"},
        "device": "$.device",
        "day": "$.days[].date",
        "value": {"from": "v", "scale": 0.1},
    },
    "time": {"from": [{"instant": "t", "zone": ZONE, "fold": "order"}], **TIMES},
}
POSITIONS = {
    "rows": "values[]",
    "fields": {"series": {"const": "positions"}, "device": "$.device", "value": "$value"},
    "time": {
        "from": [
            {
                "date": "$.date",
                "zone": ZONE,
                "fold": "earlier",
                "index": "$index",
                "every": {"s": "$.interval_s"},
                "clock": "elapsed",
            }
        ],
        **TIMES,
    },
}


def samples(*days):
    return {"device": "unit-1", "days": [{"date": day, "samples": rows} for day, rows in days]}


def at(day, *clock):
    return [{"t": f"{day}T{time}:00", "v": value} for time, value in clock]


SPRING = samples(
    ("2026-03-28", at("2026-03-28", ("12:00", 40))),
    ("2026-03-29", at("2026-03-29", ("02:30", 10), ("03:30", 20), ("04:30", 30))),
)
AUTUMN = samples(
    (
        "2026-10-25",
        at(
            "2026-10-25",
            ("02:30", 1), ("03:10", 2), ("03:40", 3), ("03:10", 4), ("03:40", 5), ("04:10", 6),
        ),
    )
)
NOVEMBER = samples(("2026-11-01", at("2026-11-01", ("09:00", 7))))
SHORT_DAY = {"device": "unit-1", "date": "2026-03-29", "interval_s": 3600, "values": list(range(23))}

# Hand-computed: EET is +02:00 and EEST +03:00. The 03:30 spring sample does not exist.
EXPECTED_SAMPLES = {
    "2026-03-28T10:00:00Z": ("+02:00", "2026-03-28", 4.0),
    "2026-03-29T00:30:00Z": ("+02:00", "2026-03-29", 1.0),
    "2026-03-29T01:30:00Z": ("+03:00", "2026-03-29", 3.0),
    "2026-10-24T23:30:00Z": ("+03:00", "2026-10-25", 0.1),
    "2026-10-25T00:10:00Z": ("+03:00", "2026-10-25", 0.2),
    "2026-10-25T00:40:00Z": ("+03:00", "2026-10-25", 0.3),
    "2026-10-25T01:10:00Z": ("+02:00", "2026-10-25", 0.4),
    "2026-10-25T01:40:00Z": ("+02:00", "2026-10-25", 0.5),
    "2026-10-25T02:10:00Z": ("+02:00", "2026-10-25", 0.6),
}
# Local midnight of the 23-hour day is 22:00Z; the clock jumps after the third hour.
SHORT_DAY_INSTANTS = (
    ["2026-03-28T22:00:00Z", "2026-03-28T23:00:00Z"]
    + [f"2026-03-29T{hour:02d}:00:00Z" for hour in range(21)]
)
SHORT_DAY_OFFSETS = ["+02:00"] * 3 + ["+03:00"] * 20
DAILY = {
    ("samples", "2026-03-28"): (1, 4.0),
    ("samples", "2026-03-29"): (2, 4.0),
    ("samples", "2026-10-25"): (6, 2.1),
    ("positions", "2026-03-29"): (23, 253),
}

MIXED_FIELDS = (
    "  natural_key: [day, position]\n  fields:\n"
    "    day: {type: date, required: true, sortable: true}\n"
    "    position: {type: integer, required: true, sortable: true}\n"
    "    local_date: {type: date, filterable: true}\n"
    "    numeric: {type: number, filterable: true}\n    text: {type: string, filterable: true}\n"
)
MIXED_MAPPING = {
    "rows": "days[].values[]",
    "fields": {
        "day": "$.days[].day", "position": "$index",
        "numeric": {"from": "$value", "source_type": "number"},
        "text": {"from": "$value", "source_type": "string"},
    },
    "time": {"from": [{"date": "$.days[].day"}], "local_date": "local_date"},
}
MIXED_DOCUMENT = {"days": [
    {"day": "2026-11-01", "values": [7, 3.25, "7", "ää😀", 7]},
    {"day": "2026-11-02", "values": [-0.0, "3.25", 3]},
]}


def preserve_export(store, name, members):
    """Zip the invented member documents and preserve the zip through archive expansion."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path, document in members.items():
            zipped.writestr(path, document if type(document) is bytes else json.dumps(document))
    receipt, stored = archive_members.preserve_members(
        store.root, guard=contextlib.nullcontext, scope="device", category="export", filename=f"{name}.zip",
        stream=archive, max_bytes=archive.getbuffer().nbytes, verified="upload",
    )
    assert stored
    return receipt["path"], (store.root / receipt["path"]).parent


def collection(store):
    setup(store, b"", title="Samples", fields=FIELDS)


def agent(store, handle=None, **request):
    with request_scope(OWNER), preview_store(store.root, handle or store.handle):
        return commands.op_record_memory(store.root, action="import", collection=CID, import_request=request)


def finish(store, job, handle=None):
    for _ in range(64):
        run_jobs(store, handle=handle)
        status = agent(store, handle, mode="status", continuation=job["continuation"])
        if status["state"] != "running":
            return status
    raise AssertionError("the import did not settle")


def stored(conn):
    return {
        (values["series"], values["at"]): values
        for values in dict(typed_storage.collection_values(conn, CID)).values()
    }


def logged(conn):
    """The import log: each member's sequence, index, member and manifest hashes, counts, digest,
    row count and transition."""
    return conn.execute(
        "SELECT m.seq, m.member_index, m.member_sha256, m.manifest_sha256, m.accepted, m.rejected, "
        "m.rows_digest, m.row_count_after, t.operation FROM import_members m JOIN txns t ON t.txn_id=m.txn_id "
        "WHERE m.collection_id=? ORDER BY m.seq",
        (CID,),
    ).fetchall()


def versions(conn):
    """The importer and zone-rules versions the import log records."""
    return set(conn.execute("SELECT importer_version, zone_rules FROM import_members WHERE collection_id=?", (CID,)))


def digest(conn, series, instants):
    """A member's row digest recomputed from its stored rows: a SHA-256 chained over each
    accepted row's item key and payload hash, in member order."""
    keys = {(values["series"], values["at"]): key for key, values in typed_storage.collection_values(conn, CID)}
    hashes = dict(conn.execute("SELECT item_key, payload_hash FROM items WHERE collection_id=?", (CID,)))
    running = hashlib.sha256(b"").hexdigest()
    for instant in instants:
        key = keys[(series, instant)]
        running = hashlib.sha256(f"{running}\0{key}\0{hashes[key]}".encode()).hexdigest()
    return running


def sha256(document):
    return hashlib.sha256(json.dumps(document).encode()).hexdigest()


def file_sha256(store, path):
    return hashlib.sha256((store.root / path).read_bytes()).hexdigest()


def test_logged_import_reuses_validation_without_losing_superseded_values_or_refusals(store, monkeypatch):
    """A later round-trip refusal must neither replace a valid duplicate nor alter the member digest."""
    fields = ("  natural_key: [exercise_id]\n  fields:\n"
              "    exercise_id: {type: string, required: true}\n    distance_m: {type: number}\n")
    text = manifest_text(fields=fields).replace("lifecycle: active\n", "lifecycle: active\nview_mode: summary\n")
    setup(store, b"", text=text)
    source_rows = [{"exercise_id": "same", "distance_m": 1},
                   {"exercise_id": "same", "distance_m": 2},
                   {"exercise_id": "same", "distance_m": 1e100},
                   {"exercise_id": "other", "distance_m": 3}]
    source, _ = preserve_export(store, "duplicates", {"rows/first.json": {"rows": source_rows}})
    mapping = {"rows": "rows[]", "fields": {name: name for name in ("exercise_id", "distance_m")},
               "on_invalid": "skip"}
    job = agent(store, mode="start", source_ref=source, format="json-document", members="rows/*", mapping=mapping)
    validate, checked = records._bulk_row_values, []

    def counted(*args):
        checked.append(args[1])
        return validate(*args)

    monkeypatch.setattr(records, "_bulk_row_values", counted)
    done = finish(store, job)
    assert done["state"] == "complete"
    assert {name: done["rows"][name] for name in ("imported", "duplicates", "rejected")} == {
        "imported": 2, "duplicates": 1, "rejected": 1}
    assert [(row["row"], row["code"]) for row in done["rejections"]] == [(2, "UNREPRESENTABLE_RECORD_VALUE")]
    parsed = collections.parse_manifest_bytes(store.root,
        store.root / "Knowledge Base/Records/Workouts/_collection.md", text.encode())
    assert dict(typed_storage.collection_values(store.connection, CID)) == {
        collections.derived_item_key(parsed, item): item for item in (source_rows[1], source_rows[3])}
    expected = hashlib.sha256(b"").hexdigest()
    for item in (source_rows[0], source_rows[1], source_rows[3]):
        key = collections.derived_item_key(parsed, item)
        payload = tokens.payload_hash(1, key, item, "")
        expected = hashlib.sha256(f"{expected}\0{key}\0{payload}".encode()).hexdigest()
    assert store.connection.execute("SELECT accepted,rejected,rows_digest FROM import_members").fetchall() == [(3, 1, expected)]
    assert len(checked) == len(source_rows), "diagnostic: each original mapped row validates once"


def test_guarded_scalar_aliases_keep_exact_values_ordinals_and_numeric_counts_after_restart(store, monkeypatch):
    """A mixed scalar array that loses strings, coerces them, or includes them in a numeric average."""
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 2)
    setup(store, b"", title="Mixed", fields=MIXED_FIELDS)
    manifest, _ = preserve_export(store, "mixed", {"days/first.json": MIXED_DOCUMENT})
    request = {"source_ref": manifest, "format": "json-document", "members": "days/*", "mapping": MIXED_MAPPING}
    preview = agent(store, mode="preview", **request)
    assert preview["mapping"]["findings"] == []
    assert preview["rows"] == {"sampled": 8, "valid": 8, "invalid": 0, "complete": True}
    job = agent(store, mode="start", **request)
    run_jobs(store, max_batches=1)
    assert agent(store, mode="status", continuation=job["continuation"])["next_position"]["member_row"] == 2
    path = store.handle.path
    store.handle.close()
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        done = finish(store, job, handle)
        assert (done["state"], done["rows"]["imported"], done["rows"]["rejected"]) == ("complete", 8, 0)
        with request_scope(OWNER), preview_store(store.root, handle):
            rows = commands.op_record_memory(store.root, action="query", collection=CID,
                query={"version": 1, "select": ["day", "position", "numeric", "text"],
                       "order_by": [{"field": "day"}, {"field": "position"}]})["rows"]
            daily = commands.op_record_memory(store.root, action="query", collection=CID, query={
                "version": 1, "group_by": [{"field": "local_date", "bucket": "day"}],
                "aggregates": {"rows": {"op": "count"}, "numbers": {"op": "count", "field": "numeric"},
                    "strings": {"op": "count", "field": "text"}, "total": {"op": "sum", "field": "numeric"},
                    "mean": {"op": "avg", "field": "numeric"}},
            })["groups"]
        expected = [{"day": day["day"], "position": position,
                     "text" if type(value) is str else "numeric": value}
                    for day in MIXED_DOCUMENT["days"] for position, value in enumerate(day["values"])]
        assert rows == expected
        assert [type(row.get("numeric", row.get("text"))) for row in rows] == [
            type(row.get("numeric", row.get("text"))) for row in expected]
        assert json.dumps(rows[5]["numeric"]) == "-0.0"
        assert [(group["rows"], group["numbers"], group["strings"], group["total"], group["mean"])
                for group in daily] == [(5, 3, 2, 17.25, 5.75), (3, 2, 1, 3, 1.5)]
        before = logged(handle.connection)
        replay = agent(store, handle, mode="start", **request)
        assert replay["continuation"] == job["continuation"] and replay["state"] == "complete"
        again, _ = preserve_export(store, "mixed-again", {
            "days/first.json": MIXED_DOCUMENT, "metadata/info.json": {"version": 1},
        })
        skipped = finish(store, agent(store, handle, mode="start", **{**request, "source_ref": again}), handle)
        assert skipped["members"] == {"selected": 1, "read": 0, "skipped": 1}
        assert skipped["rows"]["imported"] == 0 and logged(handle.connection) == before


@pytest.mark.parametrize("invalid", [True, None, {"unmapped": 1}, [1]])
@pytest.mark.parametrize("policy", ["stop", "skip"])
def test_present_unmapped_kinds_after_preview_are_visible_rejections(store, monkeypatch, invalid, policy):
    """A late scalar kind silently becomes a row with neither alias, or a boolean becomes a number."""
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 50)
    setup(store, b"", title="Mixed", fields=MIXED_FIELDS)
    document = {"days": [{"day": "2026-11-01", "values": [1] * 100 + [invalid, 2.5]}]}
    manifest, _ = preserve_export(store, "late-kind", {"days/first.json": document})
    request = {"source_ref": manifest, "format": "json-document", "members": "days/*",
               "mapping": {**MIXED_MAPPING, "on_invalid": policy}}
    preview = agent(store, mode="preview", **request)
    assert preview["rows"] == {"sampled": 100, "valid": 100, "invalid": 0, "complete": False}
    done = finish(store, agent(store, mode="start", **request))
    assert (done["state"], done["rows"]["imported"], done["rows"]["rejected"]) == (
        "failed" if policy == "stop" else "complete", 100 if policy == "stop" else 101,
        0 if policy == "stop" else 1)
    assert [(row["row"], row["code"]) for row in done["rejections"]] == (
        [] if policy == "stop" else [(100, "IMPORT_SOURCE_TYPE_UNMAPPED")])
    rows = list(typed_storage.collection_values(store.connection, CID))
    assert len(rows) == done["rows"]["imported"] and all("numeric" in values for _, values in rows)
    if policy == "stop":
        assert done["error"]["code"] == "IMPORT_SOURCE_TYPE_UNMAPPED" and done["next_position"]["row"] == 100
        assert done["error"]["row"] == 100 and done["error"]["at"] == "mapping.fields.numeric.source_type"


def test_ancestor_source_guard_groups_preserve_matching_aliases_and_missing_paths(store):
    """Guards grouped by target or row-relative spelling reject a matching enclosing value."""
    setup(store, b"", title="Mixed", fields=MIXED_FIELDS)
    document = {"days": [
        {"day": "2026-11-01", "sample": "7", "values": [0, 0]},
        {"day": "2026-11-02", "values": [0]},
    ]}
    mapping = {**MIXED_MAPPING, "fields": {**MIXED_MAPPING["fields"],
        "numeric": {"from": "$.days[].sample", "source_type": "number"},
        "text": {"from": "$.days[].sample", "source_type": "string"}}}
    manifest, _ = preserve_export(store, "ancestor", {"days/first.json": document})
    job = agent(store, mode="start", source_ref=manifest, format="json-document", members="days/*", mapping=mapping)
    assert finish(store, job)["rows"]["imported"] == 3
    rows = [values for _, values in typed_storage.collection_values(store.connection, CID)]
    assert sorted((row["day"], row["position"], row.get("text")) for row in rows) == [
        ("2026-11-01", 0, "7"), ("2026-11-01", 1, "7"), ("2026-11-02", 0, None)]
    assert all("numeric" not in row for row in rows) and "text" not in rows[2]


def test_unmapped_source_kind_preview_reports_bounded_errors_without_writing_rows(store):
    """Preview hides why guarded rows are invalid, leaks their content, or produces unbounded findings."""
    setup(store, b"", title="Mixed", fields=MIXED_FIELDS)
    document = {"days": [{"day": "2026-11-01", "values": [{"unmapped": "invented-private"}] * 25}]}
    manifest, _ = preserve_export(store, "invalid-preview", {"days/first.json": document})
    result = agent(store, mode="preview", source_ref=manifest, format="json-document", members="days/*",
                   mapping=MIXED_MAPPING)
    assert result["rows"] == {"sampled": 25, "valid": 0, "invalid": 25, "complete": True}
    assert len(result["errors"]) == 20 and {error["code"] for error in result["errors"]} == {"IMPORT_SOURCE_TYPE_UNMAPPED"}
    assert "invented-private" not in json.dumps(result["errors"])
    assert count(store.connection) == 0 and store.connection.execute("SELECT COUNT(*) FROM import_jobs").fetchone() == (0,)


@pytest.mark.parametrize("fmt", ["ndjson", "json-array"])
def test_source_guards_distinguish_missing_null_and_all_json_kinds_before_scale(store, fmt):
    """A guard that converts JSON, scales a string, or treats present null as an absent path."""
    fields = (
        "  natural_key: [id]\n  fields:\n    id: {type: string, required: true}\n"
        "    numeric: {type: number}\n    text: {type: string}\n    flag: {type: boolean}\n"
        "    nothing: {type: string}\n    object: {type: object}\n"
        "    array: {type: array, items: {type: string}}\n"
    )
    source = [{"id": str(index), "value": value}
              for index, value in enumerate([10, "10", True, None, {"nested": "ää"}, ["one", "two"]])]
    source.append({"id": "missing"})
    mapping = {"fields": {"id": "id", **{
        name: {"from": "value", "source_type": kind, **({"scale": 0.1} if kind == "number" else {})}
        for name, kind in (("numeric", "number"), ("text", "string"), ("flag", "boolean"),
                           ("nothing", "null"), ("object", "object"), ("array", "array"))
    }}}
    data = json.dumps(source).encode() if fmt == "json-array" else b"".join(
        json.dumps(row).encode() + b"\n" for row in source)
    setup(store, data, fields=fields)
    from test_collection_store_importer import SOURCE

    job = agent(store, mode="start", source_ref=SOURCE, format=fmt, mapping=mapping)
    done = finish(store, job)
    assert (done["state"], done["rows"]["imported"], done["rows"]["rejected"]) == ("complete", 7, 0), done
    rows = {values["id"]: values for _, values in typed_storage.collection_values(store.connection, CID)}
    assert rows == {
        "0": {"id": "0", "numeric": 1.0}, "1": {"id": "1", "text": "10"},
        "2": {"id": "2", "flag": True}, "3": {"id": "3", "nothing": None},
        "4": {"id": "4", "object": {"nested": "ää"}}, "5": {"id": "5", "array": ["one", "two"]},
        "missing": {"id": "missing"},
    }


@pytest.mark.parametrize(("spec", "fmt", "at"), [
    ({"from": "v", "source_type": "integer"}, "json-document", "source_type"),
    ({"from": "v", "source_type": None}, "json-document", "source_type"),
    ({"from": "v", "source_type": ["number"]}, "json-document", "source_type"),
    ({"from": "v", "source_type": "number", "other": True}, "json-document", "other"),
    ({"from": "v", "source_type": "number"}, "csv", "source_type"),
    ({"const": 1, "source_type": "number"}, "json-document", ""),
])
def test_source_guards_refuse_unknown_kinds_keys_csv_and_guarded_literals(tmp_path, spec, fmt, at):
    """An untrusted guard widens the closed mapping grammar or changes CSV conversion."""
    mapping = {**SAMPLES, "fields": {**SAMPLES["fields"], "value": spec}}
    if fmt == "csv":
        mapping.pop("rows")
    with pytest.raises(collections.CollectionError) as error:
        importer.compile_mapping(mapping, manifest(tmp_path), fmt)
    assert error.value.code == "IMPORT_MAPPING_INVALID"
    assert error.value.details["at"] == "mapping.fields.value" + (f".{at}" if at else "")
    assert len(json.dumps(error.value.details)) < 1024


def test_source_guards_keep_old_mapping_identities_and_change_only_declared_guard_identity(tmp_path):
    """An optional guard rewrites old replay/member identities, or guarded and unguarded imports collide."""
    parsed = manifest(tmp_path)
    old = importer.compile_mapping(SAMPLES, parsed, "json-document")
    assert hashlib.sha256(importer._json(old.canonical).encode()).hexdigest() == (
        "e1106ad1cfdfbc5dfeafa59d36676670bfd934befe21451fb2d4a6e0bcd53dc9")
    positions = importer.compile_mapping(POSITIONS, parsed, "json-document")
    assert hashlib.sha256(importer._json(positions.canonical).encode()).hexdigest() == (
        "92b985141677d8c9f0f05d8fc4a51b299a01c717b083a0482517e30ccd85a888")
    guarded = importer.compile_mapping({**SAMPLES, "fields": {**SAMPLES["fields"],
        "value": {**SAMPLES["fields"]["value"], "source_type": "number"}}}, parsed, "json-document")
    assert guarded.canonical["fields"]["value"] == {**old.canonical["fields"]["value"], "source_type": "number"}
    assert hashlib.sha256(importer._json(guarded.canonical).encode()).digest() != (
        hashlib.sha256(importer._json(old.canonical).encode()).digest())


def test_an_export_imports_across_the_fold_and_gap_resumes_and_skips_imported_members(store, monkeypatch):
    """The agent route must read nested members, place local times by declared zone rules,
    resume mid-member after a restart without duplicates, and skip members already imported."""
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 2)
    collection(store)
    manifest, _ = preserve_export(
        store, "first",
        {"samples/2026-03.json": SPRING, "samples/2026-10.json": AUTUMN, "series/2026-03-29.json": SHORT_DAY},
    )
    preview = agent(store, mode="preview", source_ref=manifest, format="json-document", members="samples/*",
                    mapping=SAMPLES)
    assert preview["mapping"]["findings"] == [] and preview["rows"]["sampled"] == 10
    assert preview["time"]["flagged"] == [
        {"row": 2, "code": "TIME_LOCAL_GAP", "at": "mapping.time.from[0].instant"}
    ]

    job = agent(store, mode="start", source_ref=manifest, format="json-document", members="samples/*",
                mapping=SAMPLES)
    run_jobs(store, max_batches=5)  # two batches of spring, its log row, two of autumn
    middle = agent(store, mode="status", continuation=job["continuation"])
    assert middle["state"] == "running" and middle["next_position"]["member"] == 1
    assert middle["next_position"]["member_row"] == 4  # inside the repeated hour, after the step back
    path = store.handle.path
    store.handle.close()
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        done = finish(store, job, handle)
        assert (done["state"], done["rows"]["imported"], done["rows"]["duplicates"]) == ("complete", 9, 0)
        assert (done["rows"]["rejected"], [row["code"] for row in done["rejections"]]) == (1, ["TIME_LOCAL_GAP"])
        bound = handle.connection.execute(
            "SELECT binding_json FROM import_jobs WHERE job_id=?", (job["continuation"].removeprefix("import-job:"),)
        ).fetchone()[0]
        assert json.loads(bound)["mapping"]["zone_rules"] == tzdata.IANA_VERSION
        series = agent(store, handle, mode="start", source_ref=manifest, format="json-document",
                       members=["series/2026-03-29.json"], mapping=POSITIONS)
        assert finish(store, series, handle)["rows"]["imported"] == 23
        rows = stored(handle.connection)
        assert {instant: (values["utc_offset"], values["local_date"], values["value"])
                for (name, instant), values in rows.items() if name == "samples"} == EXPECTED_SAMPLES
        assert all(values["day"] == values["local_date"] and values["device"] == "unit-1"
                   for (name, _), values in rows.items() if name == "samples")
        assert sorted((instant, values["utc_offset"], values["local_date"], values["value"])
                      for (name, instant), values in rows.items() if name == "positions") == [
            (instant, offset, "2026-03-29", index)
            for index, (instant, offset) in enumerate(zip(SHORT_DAY_INSTANTS, SHORT_DAY_OFFSETS, strict=True))
        ]
        daily = defaultdict(list)
        for (name, _), values in rows.items():
            daily[(name, values["local_date"])].append(values["value"])
        assert {key: (len(found), pytest.approx(sum(found))) for key, found in daily.items()} == DAILY

        # A row written outside any import still counts in the next member's row count.
        CollectionWriter(store.root, handle).append_record(
            CID, item={"series": "manual", "at": "2026-11-02T08:00:00Z"}, why="a row of the owner's own"
        )
        again, _ = preserve_export(
            store, "second",
            {"samples/2026-03.json": SPRING, "samples/2026-10.json": AUTUMN, "samples/2026-11.json": NOVEMBER},
        )
        later = agent(store, handle, mode="start", source_ref=again, format="json-document", members="samples/*",
                      mapping=SAMPLES)
        settled = finish(store, later, handle)
        assert settled["members"] == {"selected": 3, "read": 1, "skipped": 2}
        assert (settled["state"], settled["rows"]["imported"], settled["rows"]["rejected"]) == ("complete", 1, 0)
        assert stored(handle.connection)[("samples", "2026-11-01T07:00:00Z")]["utc_offset"] == "+02:00"
        spring, autumn = list(EXPECTED_SAMPLES)[:3], list(EXPECTED_SAMPLES)[3:]
        conn = handle.connection
        first, second = file_sha256(store, manifest), file_sha256(store, again)
        assert logged(conn) == [
            (1, 0, sha256(SPRING), first, 3, 1, digest(conn, "samples", spring), 3, "import_member"),
            (2, 1, sha256(AUTUMN), first, 6, 0, digest(conn, "samples", autumn), 9, "import_member"),
            (3, 0, sha256(SHORT_DAY), first, 23, 0, digest(conn, "positions", SHORT_DAY_INSTANTS), 32,
             "import_member"),
            (4, 2, sha256(NOVEMBER), second, 1, 0, digest(conn, "samples", ["2026-11-01T07:00:00Z"]), 34,
             "import_member"),
        ]
        assert versions(conn) == {(__version__, tzdata.IANA_VERSION)}


class _Crash(BaseException):
    """Process death: no handler in the process sees it."""


def test_a_crash_at_a_members_end_logs_it_exactly_once_on_resume(store, monkeypatch):
    """A log row committed apart from its checkpoint is lost or doubled when the process dies
    after the member's last rows and before its completion."""
    from exomem.collection_store.writer import CollectionWriter

    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 2)
    collection(store)
    day = samples(("2026-11-01", at("2026-11-01", ("09:00", 1), ("10:00", 2))))
    manifest, _ = preserve_export(store, "first", {"samples/2026-11.json": day})
    job = agent(store, mode="start", source_ref=manifest, format="json-document", members="samples/*",
                mapping=SAMPLES)
    run_jobs(store, max_batches=1)  # both rows; the member's end is the next batch
    record = CollectionWriter.record_control_transition

    def crash(self, operation, *args, **kwargs):
        if operation == "import_member":
            raise _Crash
        return record(self, operation, *args, **kwargs)

    monkeypatch.setattr(CollectionWriter, "record_control_transition", crash)
    with pytest.raises(_Crash):
        run_jobs(store, max_batches=1)
    monkeypatch.setattr(CollectionWriter, "record_control_transition", record)
    assert (logged(store.connection), count(store.connection)) == ([], 2)
    assert agent(store, mode="status", continuation=job["continuation"])["next_position"]["member_row"] == 2
    assert finish(store, job)["state"] == "complete"
    instants = ["2026-11-01T07:00:00Z", "2026-11-01T08:00:00Z"]
    assert logged(store.connection) == [
        (1, 0, sha256(day), file_sha256(store, manifest), 2, 0, digest(store.connection, "samples", instants), 2,
         "import_member")
    ]


def test_a_member_whose_bytes_differ_from_its_manifest_loses_authority(store):
    """A reader that trusts a blob's name imports rows the bound member never held."""
    collection(store)
    manifest, family = preserve_export(store, "first", {"samples/2026-10.json": AUTUMN})
    job = agent(store, mode="start", source_ref=manifest, format="json-document", members="samples/*",
                mapping=SAMPLES)
    [blob] = family.glob("__exomem_raw_v1__members/*/*.gz")
    blob.write_bytes(gzip.compress(json.dumps(NOVEMBER).encode()))
    result = finish(store, job)
    assert (result["state"], result["reason"], result["rows"]["imported"]) == ("partial", "authority_lost", 0)
    assert count(store.connection) == 0


COVERED = {**SAMPLES, "coverage": {path: {"classification": None} for path in ("$.device", "$.days[].date", "v", "t")}}


def test_a_manifest_without_the_owners_raw_binding_opens_no_member_blob(store):
    """An importer that takes any JSON its caller can read for a manifest reads owner-only blobs for anyone."""
    collection(store)
    manifest, _ = preserve_export(store, "first", {"samples/2026-10.json": AUTUMN})
    document = json.loads((store.root / manifest).read_bytes())
    release(store)
    start = {"mode": "start", "format": "json-document", "members": "samples/*", "mapping": COVERED}
    assert refused(call, store, source_ref=manifest, **start).code == "IMPORT_SOURCE_NOT_FOUND"
    copy = {**document, "archive": {**document["archive"], "filename": "forged.zip"}}
    with request_scope(_external()):
        forged = commands.op_preserve_evidence(store.root, "device", "export", "forged.json", json.dumps(copy))["path"]
    assert refused(call, store, source_ref=forged, **start).code == "IMPORT_MANIFEST_INVALID"
    assert count(store.connection) == 0


def test_a_manifest_over_its_size_cap_is_refused(store, monkeypatch):
    """A manifest read whole before any bound holds a writer thread's memory hostage to its size."""
    collection(store)
    manifest, _ = preserve_export(store, "first", {"samples/2026-10.json": AUTUMN})
    monkeypatch.setattr(archive_members, "MAX_MANIFEST_BYTES", (store.root / manifest).stat().st_size - 1)
    error = refused(agent, store, mode="start", source_ref=manifest, format="json-document", members="samples/*",
                    mapping=SAMPLES)
    assert error.code == "IMPORT_MANIFEST_INVALID"


def test_reimport_all_reads_members_already_imported_with_the_mapping(store):
    """A re-import that still skips recorded members can never repair rows after a fix."""
    collection(store)
    manifest, _ = preserve_export(store, "first", {"samples/2026-03.json": SPRING, "samples/2026-10.json": AUTUMN})
    first = agent(store, mode="start", source_ref=manifest, format="json-document", members="samples/*",
                  mapping=SAMPLES)
    assert finish(store, first)["members"] == {"selected": 2, "read": 2, "skipped": 0}
    again = agent(store, mode="start", source_ref=manifest, format="json-document", members="samples/*",
                  mapping=SAMPLES, reimport="all")
    result = finish(store, again)
    assert again["continuation"] != first["continuation"]
    assert (result["members"], result["rows"]["unchanged"]) == ({"selected": 2, "read": 2, "skipped": 0}, 9)


def test_listed_members_import_in_path_order_so_the_later_path_wins(store):
    """Honouring the caller's list order lets an older member overwrite a newer one's value."""
    collection(store)
    older = samples(("2026-11-01", at("2026-11-01", ("09:00", 1))))
    newer = samples(("2026-11-01", at("2026-11-01", ("09:00", 2))))
    manifest, _ = preserve_export(store, "first", {"samples/a.json": older, "samples/b.json": newer})
    job = agent(store, mode="start", source_ref=manifest, format="json-document",
                members=["samples/b.json", "samples/a.json"], mapping=SAMPLES)
    assert finish(store, job)["state"] == "complete"
    assert stored(store.connection)[("samples", "2026-11-01T07:00:00Z")]["value"] == 0.2


def test_a_member_identical_to_an_earlier_one_in_the_same_job_still_wins_by_path(store):
    """A skip that counts the job's own log rows drops a later member whose bytes an earlier one had."""
    collection(store)
    first = samples(("2026-11-01", at("2026-11-01", ("09:00", 1))))
    second = samples(("2026-11-01", at("2026-11-01", ("09:00", 2))))
    manifest, _ = preserve_export(store, "first", {"samples/a.json": first, "samples/b.json": second,
                                                   "samples/c.json": first})
    job = agent(store, mode="start", source_ref=manifest, format="json-document", members="samples/*",
                mapping=SAMPLES)
    assert finish(store, job)["members"] == {"selected": 3, "read": 3, "skipped": 0}
    assert stored(store.connection)[("samples", "2026-11-01T07:00:00Z")]["value"] == 0.1


def test_a_served_export_imports_members_that_start_on_later_store_checkouts(
    service: Service,  # noqa: F811 - the imported fixture, by name
    monkeypatch,
):
    """Defect: the open member reader keeps the skip check of the store checkout that opened it, so a member
    that starts in a later batch, on a later checkout of the served writer, fails and the import never
    completes."""
    monkeypatch.setattr(capability, "RELEASED", ON)
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 3)
    served_cid = "77777777-7777-4777-8777-777777777777"
    text = manifest_text(served_cid, "Served", FIELDS).replace("lifecycle: active\n", "lifecycle: active\nview_mode: summary\n")
    served(service.root, "record_memory", action="create", manifest_path=manifest_path().replace("Work", "Served"),
           manifest_text=text, why="served samples")
    members = {"samples/1-autumn.json": AUTUMN, "samples/2-november.json": NOVEMBER}
    source, _ = preserve_export(SimpleNamespace(root=service.root), "served", members)
    job = imported(service.root, collection=served_cid, mode="start", source_ref=source, format="json-document",
                   members="samples/*", mapping=SAMPLES)

    def status():
        return imported(service.root, collection=served_cid, mode="status", continuation=job["continuation"])

    _until(lambda: status()["state"] != "running", 30)
    assert status()["state"] == "complete"
    rows = served(service.root, "record_memory", action="query", collection=served_cid)["rows"]
    first = len(AUTUMN["days"][0]["samples"])
    assert first > importer.MAX_BATCH_ROWS  # the second member starts in a later batch
    assert len(rows) == first + len(NOVEMBER["days"][0]["samples"])


def test_numbers_beyond_int64_or_a_float_cost_only_their_own_rows(tmp_path):
    """A fast JSON parser that refuses 64-bit unsigned IDs fails whole members of real exports, and an
    interval or value beyond a float fails the job instead of rejecting its row."""
    (tmp_path / "Knowledge Base").mkdir()
    document = {"device_id": 2**64 - 1, "date": "2026-01-10", "values": [1, 2, 10**310]}
    mapping = {
        "rows": "values[]",
        "fields": {"series": {"const": "positions"}, "value": {"from": "$value", "scale": 0.5, "source_type": "number"}},
        "time": {"from": [{"date": "$.date", "zone": ZONE, "fold": "earlier", "index": "$index",
                           "every": {"s": 10**309}, "clock": "elapsed"}], **TIMES},
        "on_invalid": "skip",
    }
    compiled = importer.compile_mapping(mapping, manifest(tmp_path), "json-document")
    [batch] = importer.iter_batches(io.BytesIO(json.dumps(document).encode()), "json-document", compiled)
    assert [(values["value"], values["at"]) for _, values in batch.rows] == [(0.5, "2026-01-09T22:00:00Z")]
    assert [(row.ordinal, code) for row, code, _ in batch.rejections] == [
        (1, "TIME_BASIS_INVALID"), (2, "IMPORT_VALUE_INVALID")
    ]


def test_an_ancestor_after_its_row_array_is_a_row_error_that_preview_reports(store):
    """An ancestor captured after its rows were taken leaves those rows silently without it."""
    collection(store)
    late = {"device": "unit-1", "days": [{"samples": at("2026-11-01", ("09:00", 1)), "date": "2026-11-01"}]}
    with request_scope(OWNER):
        source = commands.op_preserve_evidence(store.root, "device", "export", "late.json", json.dumps(late))["path"]
    preview = agent(store, mode="preview", source_ref=source, format="json-document", mapping=SAMPLES)
    assert [(error["code"], error["at"]) for error in preview["errors"]] == [
        ("IMPORT_ANCESTOR_LATE", "mapping.fields.day")
    ]
    job = agent(store, mode="start", source_ref=source, format="json-document", mapping=SAMPLES)
    result = finish(store, job)
    assert (result["state"], result["error"]["code"]) == ("failed", "IMPORT_ANCESTOR_LATE")


@pytest.mark.parametrize(
    "document",
    [b'{"device": "unit\t1", "days": []}', b'{"device": "unit\xc0\xaf", "days": []}'],
    ids=["raw-control-character", "overlong-utf8"],
)
def test_a_member_that_is_not_json_is_malformed_in_preview_and_in_the_job(store, document):
    """A malformed member that escapes as an untyped error breaks the owner's preview and loses its location."""
    collection(store)
    manifest, _ = preserve_export(store, "bad", {"samples/bad.json": document})
    request = {"source_ref": manifest, "format": "json-document", "members": "samples/*", "mapping": SAMPLES}
    preview = agent(store, mode="preview", **request)
    assert "IMPORT_SOURCE_MALFORMED" in [error["code"] for error in preview["errors"]]
    result = finish(store, agent(store, mode="start", **request))
    assert (result["state"], result["error"]["code"]) == ("failed", "IMPORT_SOURCE_MALFORMED")


@pytest.mark.parametrize(
    ("change", "at"),
    [
        ({"rows": None}, "mapping.rows"),
        ({"rows": "days[].[]"}, "mapping.rows"),
        ({"fields": {**SAMPLES["fields"], "day": "$.days"}}, "mapping.fields.day"),
        ({"fields": {**SAMPLES["fields"], "day": "days[].date"}}, "mapping.fields.day"),
        ({"fields": {**SAMPLES["fields"], "series": {"const": "x", "from": "v"}}}, "mapping.fields.series"),
        ({"fields": {**SAMPLES["fields"], "value": {"from": "v", "scale": "0.1"}}}, "mapping.fields.value.scale"),
        ({"time": {"from": [{"instant": "t", "zone": ZONE}], **TIMES}}, "mapping.time.from[0].fold"),
        ({"time": {"from": [{"instant": "t", "zone": "Nowhere/Else", "fold": "order"}], **TIMES}},
         "mapping.time.from[0].zone"),
        ({"time": {"from": [{"instant": "t", "zone": [ZONE], "fold": "order"}], **TIMES}},
         "mapping.time.from[0].zone"),
        ({"time": {"from": [{"date": "$.days[].date", "zone": ZONE, "fold": "order", "seconds": "s"}], **TIMES}},
         "mapping.time.from[0].clock"),
        ({"time": {"from": [{"date": "$.days[].date", "seconds": "s"}], **TIMES}}, "mapping.time.from[0]"),
        ({"time": {"from": [{"instant": "t", "offset": "o", "offset_minutes": "m"}], **TIMES}},
         "mapping.time.from[0]"),
        ({"time": {"from": [{"date": "$.days[].date", "zone": ZONE, "fold": "order", "index": "$index",
                             "clock": "wall"}], **TIMES}}, "mapping.time.from[0].every"),
    ],
    ids=["rows-missing", "rows-empty-name", "ancestor-holds-rows", "array-in-row-path", "const-with-path",
         "scale-not-number", "zone-without-fold", "zone-unknown", "zone-not-a-name", "zone-increment-without-clock",
         "date-increment-without-place", "two-places", "index-without-every"],
)
def test_a_bad_document_or_time_mapping_refuses_at_its_location(store, change, at):
    """A mapping accepted with an unplaced clock or an impossible path fails late or guesses a time."""
    collection(store)
    manifest, _ = preserve_export(store, "first", {"samples/2026-10.json": AUTUMN})
    mapping = {key: value for key, value in {**SAMPLES, **change}.items() if value is not None}
    before = tuple(store.connection.iterdump())
    error = refused(agent, store, mode="start", source_ref=manifest, format="json-document", members="samples/*",
                    mapping=mapping)
    assert (error.code, error.details["at"]) == ("IMPORT_MAPPING_INVALID", at)
    assert tuple(store.connection.iterdump()) == before


def manifest(root):
    text = f"---\ntype: collection\nexomem_id: {CID}\ntitle: T\nsemantic_profile: records\ncollection_version: 1\n" \
           "schema_version: 1\nlifecycle: active\nstorage:\n  strategy: markdown-items\n  source: Items\n" \
           f"  format_version: 1\nitem_schema:\n{FIELDS}---\n"
    return collections.parse_manifest_bytes(root, root / "Knowledge Base/Records/T/_collection.md", text.encode())


def plan(time, root, fmt="ndjson", series=None):
    mapping = {"fields": {"series": series or {"const": "x"}}, "time": {"from": [time], **TIMES}}
    return importer.compile_mapping(mapping, manifest(root), fmt)


@pytest.mark.parametrize(
    ("time", "row", "expected"),
    [
        ({"date": "d", "offset_minutes": "m", "seconds": "s"}, {"d": "2026-01-10", "m": -300, "s": 90000},
         ("2026-01-11T06:00:00Z", "-05:00", "2026-01-11")),
        ({"date": "d", "zone": ZONE, "fold": "earlier", "seconds": "s", "clock": "elapsed"},
         {"d": "2026-03-29", "s": 12600}, ("2026-03-29T01:30:00Z", "+03:00", "2026-03-29")),
        ({"date": "d", "zone": ZONE, "fold": "earlier", "seconds": "s", "clock": "wall"},
         {"d": "2026-03-29", "s": 12600}, "TIME_LOCAL_GAP"),
        ({"instant": "t", "zone": ZONE, "fold": "later"}, {"t": "2026-10-25T03:30:00"},
         ("2026-10-25T01:30:00Z", "+02:00", "2026-10-25")),
        ({"instant": "t", "offset": "o"}, {"t": "2026-10-25T03:30:00", "o": "+05:30"},
         ("2026-10-24T22:00:00Z", "+05:30", "2026-10-25")),
    ],
    ids=["fixed-minutes-past-midnight", "elapsed-across-the-gap", "wall-into-the-gap", "fold-later",
         "unzoned-at-an-offset"],
)
def test_a_time_basis_places_a_wall_clock_by_its_zone_offset_and_clock(tmp_path, time, row, expected):
    """Mixing elapsed and wall time, or a fixed offset with a zone, shifts rows by an hour on change days."""
    (tmp_path / "Knowledge Base").mkdir()
    values, error = plan(time, Path(tmp_path)).apply(row)
    if type(expected) is str:
        assert error[0] == expected
    else:
        assert error is None and (values["at"], values["utc_offset"], values["local_date"]) == expected


def test_a_csv_integer_too_long_to_read_is_a_row_error(tmp_path):
    """Python refuses to read an integer of over 4,300 digits from text; unguarded, one such
    cell fails the whole job instead of its row."""
    (tmp_path / "Knowledge Base").mkdir()
    compiled = plan({"instant": "t"}, tmp_path, "csv", {"from": "n", "type": "integer"})
    assert compiled.apply({"n": "9" * 5000, "t": "2026-01-10T00:00:00Z"}) == (
        None, ("IMPORT_VALUE_INVALID", "mapping.fields.series")
    )


# The wall clocks of one autumn day: before, twice through and after the repeated hour.
FOLD_DAY = (("02:30", 0), ("03:10", 0), ("03:40", 0), ("03:10", 1), ("03:40", 1), ("04:10", 0))


@pytest.mark.parametrize(
    ("fmt", "series"),
    [("ndjson", [("samples", "2025-10-26"), ("samples", "2026-10-25")]),
     ("csv", [("a", "2026-10-25"), ("b", "2026-10-25")])],
    ids=["one-series-over-two-autumns", "two-series-sorted-by-series"],
)
def test_fold_order_starts_over_after_each_repeated_hour(tmp_path, fmt, series):
    """A fold that stays on the later offset after its repeated hour gives the next fold's first
    pass the winter offset, so it collides with the second pass and their values are lost."""
    (tmp_path / "Knowledge Base").mkdir()
    rows = [(name, f"{day}T{clock}:00", fold) for name, day in series for clock, fold in FOLD_DAY]
    if fmt == "ndjson":
        data = "".join(json.dumps({"series": name, "t": wall}) + "\n" for name, wall, _ in rows)
    else:
        data = "series,t\n" + "".join(f"{name},{wall}\n" for name, wall, _ in rows)
    compiled = plan({"instant": "t", "zone": ZONE, "fold": "order"}, tmp_path, fmt, "series")
    [batch] = importer.iter_batches(io.BytesIO(data.encode()), fmt, compiled)
    zone = zoneinfo.ZoneInfo(ZONE)
    assert [values["at"] for _, values in batch.rows] == [
        dt.datetime.fromisoformat(wall).replace(tzinfo=zone, fold=fold).astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for _, wall, fold in rows
    ]


def test_a_saved_import_is_checked_at_revise_and_named_at_start(store):
    """A saved import that stopped fitting its collection fails the next start instead of the
    revise, or a renamed source field imports rows without that field and no warning."""
    collection(store)
    text = manifest_text(CID, "Samples", FIELDS)

    def save(imports):
        guards = store.inspect_collection(CID)["lifecycle_guards"]
        revised = text.removesuffix("---\n") + f"imports: {json.dumps(imports)}\n---\n"
        return store.revise_collection(CID, manifest_text=revised, why="save an import", **guards)

    broken = {**SAMPLES, "fields": {**SAMPLES["fields"], "nope": "v"}}
    with pytest.raises(collections.CollectionError) as error:
        save({"samples": {"format": "json-document", "members": "samples/*", "mapping": broken}})
    assert (error.value.code, error.value.details["at"]) == (
        "IMPORT_MAPPING_INVALID", "imports.samples.mapping.fields.nope"
    )
    save({"samples": {"format": "json-document", "members": "samples/*", "mapping": SAMPLES}})
    manifest, _ = preserve_export(store, "first", {"samples/2026-10.json": AUTUMN})
    preview = agent(store, mode="preview", source_ref=manifest, mapping="samples")
    assert (preview["mapping"]["saved"], preview["mapping"]["absent"]) == ("samples", {})
    job = agent(store, mode="start", source_ref=manifest, mapping="samples")
    assert finish(store, job)["rows"]["imported"] == 6
    renamed = {"device": "unit-1", "days": [{"date": "2026-11-01", "samples": [
        {"t": "2026-11-01T09:00:00", "value": 7}, {"t": "2026-11-01T10:00:00", "v": 8}]}]}
    again, _ = preserve_export(store, "second", {"samples/2026-11.json": renamed})
    assert agent(store, mode="preview", source_ref=again, mapping="samples")["mapping"]["absent"] == {"value": 1}
