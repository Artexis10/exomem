"""Imports from export members and nested JSON documents (OpenSpec bring-in-large-exports §3).

Each test names the defect only it catches. An export here is built the way archive
expansion lays one out: a manifest preserved through the ordinary owner-only preserve
path, and one gzip blob per member beside it. The data is invented: a device's days of
unzoned local samples and a positional series, crossing the Europe/Tallinn autumn fold
(2026-10-25, 04:00 EEST back to 03:00 EET) and spring gap (2026-03-29, 03:00 EET to
04:00 EEST). Every expected instant below is computed by hand from those two rules.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pytest
import tzdata
from test_collection_store_importer import CID, count, manifest_text, refused, setup
from test_collection_store_importer import run as run_jobs
from test_collection_store_writer import store as store

from exomem import commands
from exomem import structured_collections as collections
from exomem.collection_store import connection, importer, typed_storage
from exomem.collection_store.preview import preview_store
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


def preserve_export(store, name, members):
    """Preserve an export as archive expansion does: an owner-only manifest and gzip blobs."""
    entries, blobs = [], {}
    for path, document in sorted(members.items()):
        data = document if type(document) is bytes else json.dumps(document).encode()
        sha256 = hashlib.sha256(data).hexdigest()
        blob = f"__exomem_raw_v1__members/{sha256[:2]}/{sha256}.gz"
        blobs[blob] = gzip.compress(data)
        entries.append({"path": path, "sha256": sha256, "bytes": len(data), "modified": None,
                        "blob": blob, "stored_bytes": len(blobs[blob])})
    archive = hashlib.sha256(b"".join(entry["sha256"].encode() for entry in entries)).hexdigest()
    manifest = {"schema_version": 1, "members": entries,
                "archive": {"filename": f"{name}.zip", "sha256": archive, "bytes": 1, "verified": "upload"}}
    with request_scope(OWNER):
        receipt = commands.op_preserve_evidence(
            store.root, "device", "export", f"{name}.export.json", json.dumps(manifest, sort_keys=True),
            raw_protection=True,
        )
    family = (store.root / receipt["path"]).parent
    for blob, data in blobs.items():
        (family / blob).parent.mkdir(parents=True, exist_ok=True)
        (family / blob).write_bytes(data)
    return receipt["path"], family


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
    run_jobs(store, max_batches=4)
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
    ("change", "at"),
    [
        ({"rows": None}, "mapping.rows"),
        ({"rows": "days[].samples"}, "mapping.rows"),
        ({"fields": {**SAMPLES["fields"], "day": "$.days"}}, "mapping.fields.day"),
        ({"fields": {**SAMPLES["fields"], "day": "days[].date"}}, "mapping.fields.day"),
        ({"fields": {**SAMPLES["fields"], "series": {"const": "x", "from": "v"}}}, "mapping.fields.series"),
        ({"fields": {**SAMPLES["fields"], "value": {"from": "v", "scale": "0.1"}}}, "mapping.fields.value.scale"),
        ({"time": {"from": [{"instant": "t", "zone": ZONE}], **TIMES}}, "mapping.time.from[0].fold"),
        ({"time": {"from": [{"instant": "t", "zone": "Nowhere/Else", "fold": "order"}], **TIMES}},
         "mapping.time.from[0].zone"),
        ({"time": {"from": [{"date": "$.days[].date", "zone": ZONE, "fold": "order", "seconds": "s"}], **TIMES}},
         "mapping.time.from[0].clock"),
        ({"time": {"from": [{"date": "$.days[].date", "seconds": "s"}], **TIMES}}, "mapping.time.from[0]"),
        ({"time": {"from": [{"instant": "t", "offset": "o", "offset_minutes": "m"}], **TIMES}},
         "mapping.time.from[0]"),
        ({"time": {"from": [{"date": "$.days[].date", "zone": ZONE, "fold": "order", "index": "$index",
                             "clock": "wall"}], **TIMES}}, "mapping.time.from[0].every"),
    ],
    ids=["rows-missing", "rows-not-an-array", "ancestor-holds-rows", "array-in-row-path", "const-with-path",
         "scale-not-number", "zone-without-fold", "zone-unknown", "zone-increment-without-clock",
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


def plan(time, root):
    text = f"---\ntype: collection\nexomem_id: {CID}\ntitle: T\nsemantic_profile: records\ncollection_version: 1\n" \
           "schema_version: 1\nlifecycle: active\nstorage:\n  strategy: markdown-items\n  source: Items\n" \
           f"  format_version: 1\nitem_schema:\n{FIELDS}---\n"
    manifest = collections.parse_manifest_bytes(root, root / "Knowledge Base/Records/T/_collection.md", text.encode())
    mapping = {"fields": {"series": {"const": "x"}}, "time": {"from": [time], **TIMES}}
    return importer.compile_mapping(mapping, manifest, "ndjson")


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
