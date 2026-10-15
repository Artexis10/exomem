"""Disk metadata preserves source spelling, enum subtype and snapshot boundaries."""

import json
import sqlite3
import struct
from dataclasses import asdict

import pytest

from exomem import structured_collections as collections
from exomem.collection_store import native_enum, native_metadata, native_temporal
from exomem.collection_store.typed_storage import TypedStorageError
from exomem.query_engine.buckets import Basis
from exomem.query_engine.scalars import InstantSpelling, ScalarValueError, scalar_key

CID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "reconstruction.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    native_metadata.install(conn, canonical=False)
    conn.execute("CREATE TABLE payloads(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,value ANY) STRICT")
    conn.commit()
    try:
        yield conn, path
    finally:
        conn.close()


def test_temporal_cells_preserve_all_accepted_spellings_after_disk_reopen(database):
    conn, path = database
    dates = ("2024-01-01", "2023-12-31", "20240102", "2024-W01-3")
    instants = (
        "2024-01-01T12:34:56Z", "2024-01-02t12:34:56,1200-00:00",
        "2023-12-31 00:00:00.1234560000+02:00", "2024-01-01", "20240101T123456Z",
        "2024-W01-1T12:34:56Z", "2024-01-01\u202f12:34:56+00:00", "2024-01-01T12:34:56",
        "2024-01-01T12:34:56.1234567Z", "2024-01-01T12:34:56+00:00:30.5", "0001-01-01T00:00:00+01:00",
    )
    source = [("date", value) for value in dates] + [("datetime", value) for value in instants]
    conn.execute("BEGIN")
    for key, (kind, value) in enumerate(source):
        collections.validate_field_value("value", value, collections.FieldSpec(kind))
        metadata = native_metadata.FieldMetadata(conn, CID, 1, 0 if kind == "date" else 1)
        cell = native_temporal.encode(value, kind, metadata)
        conn.execute("INSERT INTO payloads VALUES(?,?,?)", (key, kind, cell))
    conn.commit()
    with sqlite3.connect(path) as reopened:
        actual = []
        for key, kind, cell in reopened.execute("SELECT * FROM payloads ORDER BY id"):
            metadata = native_metadata.FieldMetadata(reopened, CID, 1, 0 if kind == "date" else 1)
            actual.append((kind, native_temporal.decode(cell, kind, metadata, max_bytes=200)))
            if kind == "date":
                assert native_temporal.operand(cell, kind, metadata, max_bytes=200) == int(scalar_key(source[key][1], kind)[1], 16)
            elif type(cell) is str:
                with pytest.raises(ScalarValueError):
                    native_temporal.operand(cell, kind, metadata, max_bytes=200)
        assert actual == source
        assert reopened.execute("SELECT typeof(value) FROM payloads WHERE id=1").fetchone() == ("integer",)
        assert reopened.execute("SELECT value FROM payloads WHERE id=1").fetchone()[0] < 0
        assert reopened.execute("SELECT typeof(value) FROM payloads WHERE id=5").fetchone() == ("blob",)


def test_first_metadata_is_atomic_and_a_later_snapshot_sees_it(database):
    conn, path = database
    field = native_metadata.FieldMetadata(conn, CID, 1, 0)
    conn.execute("BEGIN")
    assert native_temporal.encode(None, "datetime", field) is None
    assert field.epoch() is None
    with sqlite3.connect(path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        metadata = native_metadata.FieldMetadata(writer, CID, 1, 0)
        cell = native_temporal.encode("2024-01-01T00:00:00", "datetime", metadata)
        writer.execute("INSERT INTO payloads VALUES(0,'datetime',?)", (cell,))
    assert field.epoch() is None
    conn.commit()
    conn.execute("BEGIN")
    assert field.epoch() == 0  # A compression origin, not a timezone assignment.
    assert native_temporal.decode(conn.execute("SELECT value FROM payloads").fetchone()[0], "datetime", field, max_bytes=100) == "2024-01-01T00:00:00"
    conn.commit()
    other = native_metadata.FieldMetadata(conn, CID, 2, 0)
    conn.execute("BEGIN")
    native_temporal.encode("2024-01-01T00:00:00Z", "datetime", other)
    conn.execute("INSERT INTO payloads VALUES(1,'datetime',0)")
    conn.rollback()
    assert other.epoch() is None
    assert conn.execute("SELECT count(*) FROM typed_value_formats WHERE generation=2").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM payloads").fetchone() == (1,)


@pytest.mark.parametrize("offset_field", [None, "offset"])
@pytest.mark.parametrize("day_only", [False, True])
def test_temporal_views_keep_local_day_and_recency_when_source_offsets_are_unknown_or_overridden(database, offset_field, day_only):
    """Skipping lexical reconstruction must preserve source-local buckets, flags and exact recency."""
    conn, _ = database
    field = native_metadata.FieldMetadata(conn, CID, 1, 0)
    examples = ("2024-01-01T23:30:00Z", "2024-01-02t00:30:00,1200+02:00",
                "2024-01-01T23:30:00-00:00", "20240101T233000Z", "2024-01-01T23:30:00",
                "1960-01-01T23:30:00.123456-02:00")
    conn.execute("BEGIN")
    cells = [native_temporal.encode(source, "datetime", field) for source in examples]
    conn.commit()
    snapshot = native_metadata.snapshot_fields(conn, CID, 1, (0,), database="main")[0]
    basis = Basis("when", "instant", offset_field)
    for source, cell in zip(examples, cells, strict=True):
        view = native_temporal.inspect(cell, "datetime", snapshot, max_bytes=100, day_only=day_only)
        for supplied in (None, "+05:30", "-03:00", "-00:00", "invalid"):
            offsets = {} if supplied is None else {"offset": supplied}
            for ordered in (True, False):
                assert basis.locate({"when": view, **offsets}, ordered=ordered) == basis.locate(
                    {"when": source, **offsets}, ordered=ordered)
        with pytest.raises(TypedStorageError, match="budget"):
            native_temporal.inspect(cell, "datetime", snapshot, max_bytes=len(source.encode()) - 1, day_only=day_only)


@pytest.mark.parametrize(("source", "offset"), (
    ("0001-01-01T00:00:00Z", "-01:00"), ("9999-12-31T23:59:59Z", "+01:00"),
))
def test_native_time_basis_preserves_source_override_overflow(database, source, offset):
    """A day-only scan must not turn an overflowing supplied offset into a valid bucket."""
    conn, _ = database
    field = native_metadata.FieldMetadata(conn, CID, 1, 0)
    conn.execute("BEGIN")
    cell = native_temporal.encode(source, "datetime", field)
    conn.commit()
    basis = Basis("when", "instant", "offset")
    with pytest.raises(OverflowError):
        basis.locate({"when": source, "offset": offset})
    for day_only in (False, True):
        view = native_temporal.inspect(cell, "datetime", field, max_bytes=100, day_only=day_only)
        with pytest.raises(OverflowError):
            basis.locate({"when": view, "offset": offset})


def test_enum_ids_preserve_equality_equivalent_source_types_and_signed_zero(database):
    conn, path = database
    declared = (True, 1, 1.0, "1", 0.0)
    source = (*declared, -0.0)
    spec = collections.FieldSpec("enum", enum=declared)
    field = native_metadata.FieldMetadata(conn, CID, 1, 0)
    conn.execute("BEGIN")
    native_enum.seed(declared, field)
    for key, value in enumerate(source):
        collections.validate_field_value("value", value, spec)
        conn.execute("INSERT INTO payloads VALUES(?,'enum',?)", (key, native_enum.encode(value, field)))
    conn.commit()
    with sqlite3.connect(path) as reopened:
        metadata = native_metadata.FieldMetadata(reopened, CID, 1, 0)
        ids = [row[0] for row in reopened.execute("SELECT value FROM payloads ORDER BY id")]
        actual = [native_enum.decode(value_id, metadata, max_bytes=100) for value_id in ids]
        assert len(set(ids)) == len(source)
        assert json.dumps(actual) == json.dumps(source)
        for value, decoded, value_id in zip(source, actual, ids, strict=True):
            assert type(decoded) is type(value)
            if type(value) is float:
                assert struct.pack(">d", decoded) == struct.pack(">d", value)
            assert native_enum.operand(value_id, metadata, max_bytes=100) == scalar_key(value, "enum")


def test_bad_frames_unknown_formats_and_large_reconstruction_refuse_with_storage_error(database):
    conn, _ = database
    field = native_metadata.FieldMetadata(conn, CID, 1, 0)
    conn.execute("BEGIN")
    native_temporal.encode("2024-01-01T00:00:00Z", "datetime", field)
    wide = field.intern(asdict(InstantSpelling("T", ".", 10000, "Z")))
    bad = field.intern({"unknown": True})
    conn.commit()
    for frame in (b"", struct.pack(">Bqq", 2, 0, wide), struct.pack(">Bqq", 1, 0, 999), struct.pack(">Bqq", 1, 0, bad)):
        with pytest.raises(TypedStorageError):
            native_temporal.decode(frame, "datetime", field, max_bytes=100)
    with pytest.raises(TypedStorageError, match="budget"):
        native_temporal.decode(struct.pack(">Bqq", 1, 0, wide), "datetime", field, max_bytes=100)
    assert native_temporal.operand(struct.pack(">Bqq", 1, 0, wide), "datetime", field, max_bytes=100) == int(
        scalar_key("2024-01-01T00:00:00Z", "datetime")[1], 16)
    with pytest.raises(TypedStorageError, match="budget"):
        field.format(wide, max_bytes=1)
    for statement in ("UPDATE typed_field_epochs SET epoch=0", "DELETE FROM typed_value_formats",
                      "INSERT OR REPLACE INTO typed_value_formats SELECT * FROM typed_value_formats"):
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(statement)


@pytest.mark.parametrize(("original", "corrupt_delta"), (
    ("2024-01-01T00:00:00Z", 1),
    ("9999-12-31T23:59:59+01:00", 3600 * 1000000),
    ("0001-01-01T01:00:00+01:00", -60 * 1000000),
), ids=("precision", "local-range", "utc-range"))
def test_unrepresentable_temporal_cells_refuse_decode_and_comparison_after_reopen(database, original, corrupt_delta):
    conn, path = database
    conn.execute("BEGIN")
    field = native_metadata.FieldMetadata(conn, CID, 1, 0)
    assert native_temporal.encode(original, "datetime", field) == 0
    conn.execute("INSERT INTO payloads VALUES(0,'datetime',?)", (corrupt_delta,))
    conn.commit()
    with sqlite3.connect(path) as reopened:
        metadata = native_metadata.FieldMetadata(reopened, CID, 1, 0)
        cell = reopened.execute("SELECT value FROM payloads").fetchone()[0]
        with pytest.raises(TypedStorageError):
            native_temporal.decode(cell, "datetime", metadata, max_bytes=100)
        with pytest.raises(TypedStorageError):
            native_temporal.operand(cell, "datetime", metadata, max_bytes=100)
        with pytest.raises(TypedStorageError):
            native_temporal.inspect(cell, "datetime", metadata, max_bytes=100, day_only=True)
