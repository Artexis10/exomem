"""Time bases and row paths of import mappings (OpenSpec scale-derived-imports-and-widen-time-bases §5).

Each test names the defect only it catches. The data is invented: a generic "sensor" export
of samples at offsets from a start time, a "journal" export of wall-clock entries with no zone,
daily summaries, and one document per session. Every expected value is computed by hand.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from test_collection_store_import_exports import agent, finish, preserve_export
from test_collection_store_importer import CID, setup
from test_collection_store_writer import store as store

from exomem import commands
from exomem import structured_collections as collections
from exomem.collection_store import importer, typed_storage
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import owner_principal, request_scope

OWNER = owner_principal(surface="mcp")
FIELDS = (
    "  natural_key: [series, ref]\n  fields:\n"
    "    series: {type: string, required: true, sortable: true}\n"
    "    ref: {type: string, required: true, sortable: true}\n"
    "    at: {type: datetime, filterable: true}\n    utc_offset: {type: string}\n"
    "    local_date: {type: date}\n    local_clock: {type: string}\n    value: {type: number}\n"
    "    count: {type: integer}\n    seen: {type: datetime}\n"
)
# Fields that name no time target, for row-path cases that read plain values.
ROW_FIELDS = (
    "  natural_key: [ref]\n  fields:\n"
    "    ref: {type: string, required: true}\n    steps: {type: number}\n    position: {type: integer}\n"
    "    score: {type: number}\n"
)
TIMES = {"instant": "at", "offset": "utc_offset", "local_date": "local_date", "local_datetime": "local_clock"}
START = "2025-03-01T10:00:00Z"


def manifest(root: Path, fields: str = FIELDS) -> collections.CollectionManifest:
    (root / "Knowledge Base").mkdir(exist_ok=True)
    text = (
        f"---\ntype: collection\nexomem_id: {CID}\ntitle: T\nsemantic_profile: records\ncollection_version: 1\n"
        "schema_version: 1\nlifecycle: active\nstorage:\n  strategy: markdown-items\n  source: Items\n"
        f"  format_version: 1\nitem_schema:\n{fields}---\n"
    )
    return collections.parse_manifest_bytes(root, root / "Knowledge Base/Records/T/_collection.md", text.encode())


def compiled(basis: dict, root: Path, **time):
    """A mapping of constant series and ref, so each case reads only its time basis."""
    mapping = {
        "fields": {"series": {"const": "x"}, "ref": {"const": "r"}},
        "time": {"from": [basis], **{**TIMES, **time}},
    }
    return importer.compile_mapping(mapping, manifest(root), "ndjson")


def placed(basis: dict, row: dict, root: Path):
    return compiled(basis, root).apply(row)


@pytest.mark.parametrize(
    ("basis", "row", "expected"),
    [
        ({"instant": "t", "floating": True}, {"t": "2025-06-14T21:30:15.25"},
         {"local_date": "2025-06-14", "local_clock": "2025-06-14T21:30:15.250000"}),
        # A value that spells its own offset is an instant, floating or not.
        ({"instant": "t", "floating": True}, {"t": "2025-06-15T08:05:00+02:00"},
         {"at": "2025-06-15T06:05:00Z", "utc_offset": "+02:00", "local_date": "2025-06-15",
          "local_clock": "2025-06-15T08:05:00"}),
        ({"date": "d", "floating": True, "seconds": "s"}, {"d": "2025-06-14", "s": 5400},
         {"local_date": "2025-06-14", "local_clock": "2025-06-14T01:30:00"}),
        # A date with no increment names no clock, so it gets none.
        ({"date": "d", "floating": True}, {"d": "2025-06-14"}, {"local_date": "2025-06-14"}),
    ],
    ids=["wall-clock", "own-offset", "date-and-increment", "date-alone"],
)
def test_a_floating_row_gets_a_day_and_a_wall_clock_and_never_an_invented_instant(tmp_path, basis, row, expected):
    """A floating row read as UTC or as the host's zone gets an instant that no source stated."""
    values, error = placed(basis, row, tmp_path)
    assert error is None
    assert values == {"series": "x", "ref": "r", **expected}


@pytest.mark.parametrize(
    ("increment", "offset"),
    [
        ("seconds", 5400.5),
        ("milliseconds", 5_400_500),
        ("duration", "PT1H30M0.5S"),
        ("duration", "P0DT1H30M0.5S"),
        ("duration", "PT90M0.5S"),
    ],
)
def test_every_increment_form_moves_the_start_to_the_same_instant(tmp_path, increment, offset):
    """One form that counts differently moves every sample of an export by the wrong span."""
    values, error = placed({"instant": "t", increment: "n"}, {"t": START, "n": offset}, tmp_path)
    assert error is None
    assert (values["at"], values["utc_offset"], values["local_clock"]) == (
        "2025-03-01T11:30:00.500000Z", "+00:00", "2025-03-01T11:30:00.500000",
    )


@pytest.mark.parametrize(
    ("duration", "instant"),
    [
        ("-P1DT2H", "2025-02-28T08:00:00Z"),
        ("P1W", "2025-03-08T10:00:00Z"),
        ("P2W3D", "2025-03-18T10:00:00Z"),
        ("P0.5D", "2025-03-01T22:00:00Z"),
        ("PT0.000001S", "2025-03-01T10:00:00.000001Z"),
    ],
)
def test_a_duration_reads_its_sign_weeks_days_and_fraction(tmp_path, duration, instant):
    """A reader that drops the sign or counts a week as a day shifts rows by whole days."""
    values, error = placed({"instant": "t", "duration": "n"}, {"t": START, "n": duration}, tmp_path)
    assert error is None and values["at"] == instant


@pytest.mark.parametrize(
    "duration",
    ["P1Y", "P1M", "P1Y2M", "P1Y1D", "PT", "P", "P1DT", "PT1.5M30S", "1H", "PT1H-", "-", "PT1H ", 90, None, True],
)
def test_a_duration_with_a_year_a_month_or_a_broken_shape_is_a_refused_row(tmp_path, duration):
    """A month read as a minute, or as a fixed number of days, places a row at an instant no source gave."""
    values, error = placed({"instant": "t", "duration": "n"}, {"t": START, "n": duration}, tmp_path)
    assert values is None
    assert error == ("TIME_BASIS_INVALID", "mapping.time.from[0].duration")


@pytest.mark.parametrize(
    ("basis", "text", "expected"),
    [
        ({"instant": "t", "offset_minutes": "m"}, "2025-03-01T07:45", ("2025-03-01T05:45:00Z", "+02:00")),
        ({"instant": "t", "floating": True}, "2025-03-01T07:45+02:00", ("2025-03-01T05:45:00Z", "+02:00")),
        ({"instant": "t", "floating": True}, "2025-03-01T07:45Z", ("2025-03-01T07:45:00Z", "+00:00")),
        ({"instant": "t", "zone": "Europe/Tallinn", "fold": "earlier"}, "2025-03-01T07:45",
         ("2025-03-01T05:45:00Z", "+02:00")),
    ],
    ids=["fixed-offset", "own-offset", "own-utc", "zone"],
)
def test_a_time_written_to_the_minute_reads_as_second_zero(tmp_path, basis, text, expected):
    """A reader that needs seconds refuses every export that writes `2025-03-01T07:45`."""
    values, error = compiled(basis, tmp_path).apply({"t": text, "m": 120})
    assert error is None and (values["at"], values["utc_offset"]) == expected
    assert values["local_clock"] == "2025-03-01T07:45:00"


@pytest.mark.parametrize("text", ["2025-03-01T07:45.5", "2025-03-01T07", "2025-03-01T07:45:", "2025-03-01T07:4"])
def test_minute_precision_does_not_loosen_the_rest_of_the_instant_grammar(tmp_path, text):
    """A reader that pads anything short turns a truncated or fractional-minute value into a time."""
    values, error = placed({"instant": "t", "floating": True}, {"t": text}, tmp_path)
    assert values is None and error == ("TIME_BASIS_INVALID", "mapping.time.from[0].instant")


def rows_of(document, rows: str, fields: dict, tmp_path: Path, **mapping):
    """Every batch row and rejection of one document, through the streaming reader."""
    plan = importer.compile_mapping(
        {"rows": rows, "fields": fields, **mapping}, manifest(tmp_path, ROW_FIELDS), "json-document"
    )
    batches = list(importer.iter_batches(io.BytesIO(json.dumps(document).encode()), "json-document", plan))
    return (
        [values for batch in batches for _, values in batch.rows],
        [(row.ordinal, code) for batch in batches for row, code, _ in batch.rejections],
        batches,
    )


def test_an_object_row_path_gives_one_row_per_enclosing_element(tmp_path):
    """A path that ends at a name must take the object there for each element, and skip an element without one."""
    document = {"device": "s1", "days": [
        {"date": "2025-03-01", "summary": {"steps": 8000}},
        {"date": "2025-03-02"},
        {"date": "2025-03-03", "summary": {"steps": 1200}},
    ]}
    fields = {"ref": "$.days[].date", "steps": "steps", "position": "$index"}
    rows, rejected, _ = rows_of(document, "days[].summary", fields, tmp_path)
    assert rows == [
        {"ref": "2025-03-01", "steps": 8000, "position": 0},
        {"ref": "2025-03-03", "steps": 1200, "position": 2},
    ] and rejected == []


@pytest.mark.parametrize("summary", [None, 7, [1], "text"], ids=["null", "number", "array", "string"])
def test_a_value_at_an_object_row_path_that_is_not_an_object_is_a_visible_refusal(tmp_path, summary):
    """A path that forgot its `[]` and met an array must not end as a job that imported zero rows."""
    document = {"days": [{"date": "2025-03-01", "summary": summary}, {"date": "2025-03-02", "summary": {"steps": 5}}]}
    rows, rejected, _ = rows_of(
        document, "days[].summary", {"ref": "$.days[].date", "steps": "steps"}, tmp_path, on_invalid="skip"
    )
    assert [row["ref"] for row in rows] == ["2025-03-02"]
    assert rejected == [(0, "IMPORT_ROW_NOT_OBJECT")]


def test_a_root_row_path_makes_the_document_one_row_inside_the_row_bounds(tmp_path, monkeypatch):
    """A root row that skipped the row bound would load a whole member into memory."""
    document = {"session": "s1", "score": 7.5, "log": ["x" * 40] * 8}
    fields = {"ref": "session", "score": "score"}
    rows, rejected, _ = rows_of(document, "$", fields, tmp_path)
    assert rows == [{"ref": "s1", "score": 7.5}] and rejected == []
    monkeypatch.setattr(importer, "MAX_ROW_BYTES", 100)
    rows, rejected, _ = rows_of(document, "$", fields, tmp_path, on_invalid="skip")
    assert rows == [] and rejected == [(0, "IMPORT_ROW_TOO_LARGE")]


COMPILE_REFUSALS = [
    pytest.param(
        {"rows": "$", "fields": {"ref": "$index"}}, None, "mapping.fields.ref",
        id="index-on-the-root-row-path",
    ),
    pytest.param(
        {"rows": "meta.entry", "fields": {"ref": "$index"}}, None, "mapping.fields.ref",
        id="index-on-a-path-with-no-array",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "floating": True, "zone": "Europe/Tallinn"}], **TIMES}},
        None, "mapping.time.from[0].zone", id="floating-with-a-zone",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "floating": True, "offset": "o"}], **TIMES}},
        None, "mapping.time.from[0].offset", id="floating-with-an-offset",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "floating": True, "offset_minutes": "o"}], **TIMES}},
        None, "mapping.time.from[0].offset_minutes", id="floating-with-offset-minutes",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "floating": True, "clock": "wall"}], **TIMES}},
        None, "mapping.time.from[0].clock", id="floating-with-a-clock",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "floating": True, "fold": "order"}], **TIMES}},
        None, "mapping.time.from[0].fold", id="floating-with-a-fold",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "floating": False}], **TIMES}},
        None, "mapping.time.from[0].floating", id="floating-false",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "floating": True}], **TIMES}},
        ("at: {type: datetime, filterable: true}", "at: {type: datetime, required: true}"), "mapping.time.instant",
        id="floating-with-a-required-instant",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "floating": True}], **TIMES}},
        ("utc_offset: {type: string}", "utc_offset: {type: string, required: true}"), "mapping.time.offset",
        id="floating-with-a-required-offset",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "floating": True}], "instant": "at",
                                         "local_date": "local_date"}},
        ("natural_key: [series, ref]", "natural_key: [series, at]"), "mapping.time.instant",
        id="floating-with-an-instant-in-the-natural-key",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "floating": True}], **{**TIMES, "local_datetime": "count"}}},
        None, "mapping.time.local_datetime", id="local-datetime-into-a-number",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "floating": True}], **{**TIMES, "local_datetime": "seen"}}},
        None, "mapping.time.local_datetime", id="local-datetime-into-a-datetime",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "seconds": "s", "duration": "d"}], **TIMES}},
        None, "mapping.time.from[0]", id="two-increments",
    ),
    pytest.param(
        {"rows": "entries[]", "time": {"from": [{"instant": "t", "milliseconds": "m", "index": "$index",
                                                   "every": {"s": 1}}], **TIMES}},
        None, "mapping.time.from[0]", id="milliseconds-with-an-index",
    ),
]


@pytest.mark.parametrize(("change", "field_edit", "at"), COMPILE_REFUSALS)
def test_a_mapping_the_new_forms_cannot_honour_is_refused_at_its_location(tmp_path, change, field_edit, at):
    """A floating basis whose row cannot carry its instant, or a clock text with nowhere to go,
    imports rows that fail one by one, or none at all, instead of failing the mapping once."""
    base = {"rows": "entries[]", "fields": {"series": {"const": "x"}, "ref": "id", "value": "v"}}
    fields = FIELDS if field_edit is None else FIELDS.replace(*field_edit)
    with pytest.raises(collections.CollectionError) as error:
        importer.compile_mapping({**base, **change}, manifest(tmp_path, fields), "json-document")
    assert (error.value.code, error.value.details["at"]) == ("IMPORT_MAPPING_INVALID", at)


JOURNAL = {"entries": [
    {"id": "j1", "written": "2025-06-14T21:30", "mood": 3},
    {"id": "j2", "written": "2025-06-14T23:59:59.5", "mood": 4},
    {"id": "j3", "written": "2025-06-15T08:05+02:00", "mood": 5},
]}
SAMPLES = {"device": "s1", "start": START, "samples": [
    {"id": "a", "after_ms": 0, "after": "PT0S", "v": 1},
    {"id": "b", "after_ms": 1500, "after": "PT1.5S", "v": 2},
    {"id": "c", "after_ms": 90500, "after": "PT1M30.5S", "v": 3},
    {"id": "d", "after_ms": 90_000_000, "after": "P1DT1H", "v": 4},
]}
DAYS = {"device": "s1", "days": [
    {"date": "2025-03-01", "summary": {"steps": 8000}},
    {"date": "2025-03-02", "note": "rest", "summary": {"steps": 1200}},
]}
SESSIONS = {
    "sessions/s1.json": {"session": "s1", "started": "2025-03-02T09:00:00", "score": 7.5},
    "sessions/s2.json": {"session": "s2", "started": "2025-03-02T18:15", "score": 6},
}


def mapping(series: str, rows: str, **more):
    return {"rows": rows, "fields": {"series": {"const": series}, **more.pop("fields")}, **more}


def test_an_export_that_uses_every_new_form_imports_the_hand_computed_rows(store):
    """The whole path, from the manifest to stored rows and an instant filter, for one export that
    uses a floating basis with a wall-clock text, minute precision, milliseconds and duration
    increments, an object row path and a root row path."""
    setup(store, b"", title="Readings", fields=FIELDS)
    manifest_ref, _ = preserve_export(store, "readings", {
        "journal/2025-06.json": JOURNAL, "sensor/ms.json": SAMPLES, "sensor/iso.json": {**SAMPLES, "device": "s2"},
        "days/2025-03.json": DAYS, **SESSIONS,
    })
    jobs = {
        "journal": ("journal/*", mapping("journal", "entries[]", fields={"ref": "id", "value": "mood"}, time={
            "from": [{"instant": "written", "floating": True}], **TIMES})),
        "sensor-ms": ("sensor/ms.json", mapping("sensor-ms", "samples[]", fields={"ref": "id", "value": "v"}, time={
            "from": [{"instant": "$.start", "milliseconds": "after_ms"}], **TIMES})),
        "sensor-iso": ("sensor/iso.json", mapping("sensor-iso", "samples[]", fields={"ref": "id", "value": "v"}, time={
            "from": [{"instant": "$.start", "duration": "after"}], **TIMES})),
        "daily": ("days/*", mapping("daily", "days[].summary", fields={"ref": "$.days[].date", "value": "steps"}, time={
            "from": [{"date": "$.days[].date"}], **TIMES})),
        "session": ("sessions/*", mapping("session", "$", fields={"ref": "session", "value": "score"}, time={
            "from": [{"instant": "started", "floating": True}], **TIMES})),
    }
    imported, previews = {}, {}
    for name, (members, mapped) in jobs.items():
        request = {"source_ref": manifest_ref, "format": "json-document", "members": members, "mapping": mapped}
        previews[name] = agent(store, mode="preview", **request)
        done = finish(store, agent(store, mode="start", **request))
        assert done["state"] == "complete", done
        imported[name] = done["rows"]["imported"]
    assert imported == {"journal": 3, "sensor-ms": 4, "sensor-iso": 4, "daily": 2, "session": 2}
    assert {name: (view["rows"]["valid"], view["mapping"]["findings"]) for name, view in previews.items()} == {
        "journal": (3, []), "sensor-ms": (4, []), "sensor-iso": (4, []), "daily": (2, []), "session": (2, []),
    }
    # Preview counts the sampled rows by how their time resolved.
    assert {name: view["time"]["resolution"] for name, view in previews.items()} == {
        "journal": {"local": 2, "instant": 1}, "sensor-ms": {"instant": 4}, "sensor-iso": {"instant": 4},
        "daily": {"day": 2}, "session": {"local": 2},
    }

    # (series, ref, value, at, utc_offset, local_date, local_clock); None is a field the row does not carry.
    expected = [
        ("journal", "j1", 3, None, None, "2025-06-14", "2025-06-14T21:30:00"),
        ("journal", "j2", 4, None, None, "2025-06-14", "2025-06-14T23:59:59.500000"),
        ("journal", "j3", 5, "2025-06-15T06:05:00Z", "+02:00", "2025-06-15", "2025-06-15T08:05:00"),
        ("sensor-ms", "a", 1, "2025-03-01T10:00:00Z", "+00:00", "2025-03-01", "2025-03-01T10:00:00"),
        ("sensor-ms", "b", 2, "2025-03-01T10:00:01.500000Z", "+00:00", "2025-03-01", "2025-03-01T10:00:01.500000"),
        ("sensor-ms", "c", 3, "2025-03-01T10:01:30.500000Z", "+00:00", "2025-03-01", "2025-03-01T10:01:30.500000"),
        ("sensor-ms", "d", 4, "2025-03-02T11:00:00Z", "+00:00", "2025-03-02", "2025-03-02T11:00:00"),
        ("sensor-iso", "a", 1, "2025-03-01T10:00:00Z", "+00:00", "2025-03-01", "2025-03-01T10:00:00"),
        ("sensor-iso", "b", 2, "2025-03-01T10:00:01.500000Z", "+00:00", "2025-03-01", "2025-03-01T10:00:01.500000"),
        ("sensor-iso", "c", 3, "2025-03-01T10:01:30.500000Z", "+00:00", "2025-03-01", "2025-03-01T10:01:30.500000"),
        ("sensor-iso", "d", 4, "2025-03-02T11:00:00Z", "+00:00", "2025-03-02", "2025-03-02T11:00:00"),
        ("daily", "2025-03-01", 8000, None, None, "2025-03-01", None),
        ("daily", "2025-03-02", 1200, None, None, "2025-03-02", None),
        ("session", "s1", 7.5, None, None, "2025-03-02", "2025-03-02T09:00:00"),
        ("session", "s2", 6, None, None, "2025-03-02", "2025-03-02T18:15:00"),
    ]
    names = ("series", "ref", "value", "at", "utc_offset", "local_date", "local_clock")
    rows = {
        (values["series"], values["ref"]): values
        for _, values in typed_storage.collection_values(store.connection, CID)
    }
    assert rows == {
        row[:2]: {name: item for name, item in zip(names, row, strict=True) if item is not None} for row in expected
    }
    # An instant filter does not return a floating row, a day row or a row of an object path.
    with request_scope(OWNER), preview_store(store.root, store.handle):
        found = commands.op_record_memory(store.root, action="query", collection=CID, query={
            "version": 1, "select": ["series", "ref"],
            "where": {"field": "at", "op": "gte", "value": "2000-01-01T00:00:00Z"},
            "order_by": [{"field": "series"}, {"field": "ref"}],
        })["rows"]
    assert [(row["series"], row["ref"]) for row in found] == sorted(row[:2] for row in expected if row[3] is not None)
