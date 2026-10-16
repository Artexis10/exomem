"""Native indexed operands survive storage transactions and physical file aliases."""

import datetime as dt
import sqlite3
import struct
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects.sqlite import dialect
from test_collection_store_writer import store as store

from exomem.collection_store import (
    connection,
    derived_rows,
    native_layout,
    native_metadata,
    native_operands,
    native_rows,
)
from exomem.collection_store.native_layout import Codec
from exomem.collection_store.native_metadata import FieldMetadata
from exomem.collection_store.native_operands import FileRole, scalar_operand
from exomem.collection_store.native_projections import NativeProjectionPlan
from exomem.collection_store.query_indexes import build_projection_plan
from exomem.collection_store.typed_storage import TypedStorageError, canonical_json
from exomem.query_engine.indexes import IndexKey, IndexSpec
from exomem.structured_collections import FieldSpec

CID = "00000000-0000-4000-8000-000000000001"
MICROSECOND = dt.timedelta(microseconds=1)


def write(conn, plan, ordinal, values):
    """Execute the production Core upsert through its raw SQLite compilation."""
    parameters = plan.parameters(conn, ordinal, f"00000000-0000-4000-8000-{ordinal:012}", 1, values)
    compiled = plan.upsert.compile(dialect=dialect())
    conn.execute(str(compiled), tuple(parameters[name] for name in compiled.positiontup))


def test_native_bulk_temporal_and_enum_indexes_survive_failed_payload_metadata_reuse(store):
    """A failed current payload must not leave reconstruction metadata in the next writer operation."""
    from test_collection_store_summary import summary_text
    from test_collection_store_writer import CID as collection_id
    from test_collection_store_writer import manifest_path, manifest_text
    from test_records_bulk_upsert import EVIDENCE, _evidence

    from exomem.collection_store import index_migrations, typed_storage

    text = summary_text(manifest_text()).replace("    count: {type: integer}\n", (
        "    when: {type: datetime, sortable: true}\n"
        "    kind: {type: enum, enum: [true, 1, one], filterable: true}\n"))
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    _evidence(store.root)
    guard = store.inspect_collection(collection_id)["snapshot"]
    failed = [{"title": "First", "when": "2024-01-01T00:00:00Z", "kind": True},
              {"title": "Failure", "when": "2024-01-01T00:00:00.0000001Z", "kind": "one"}]
    with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
        store.bulk_upsert_records(collection_id, rows=[{"item": item} for item in failed],
                                  source=EVIDENCE, why="import", expected_container_hash=guard)
    assert store.connection.execute("SELECT count(*) FROM items").fetchone() == (0,)
    assert store.connection.execute("SELECT count(*) FROM typed_field_epochs").fetchone() == (0,)
    assert store.connection.execute("SELECT count(*) FROM typed_value_formats").fetchone() == (0,)
    accepted = [{"title": f"Row {index}", "when": "2024-02-03t01:00:00,1200+01:00", "kind": kind}
                for index, kind in enumerate(("one", "one", 1, 1))]
    result = store.bulk_upsert_records(collection_id, rows=[{"item": item} for item in accepted],
                                      source=EVIDENCE, why="import", expected_container_hash=guard)
    assert result["counts"]["inserted"] == 4
    current = {values["title"]: values for _, values in typed_storage.collection_values(store.connection, collection_id)}
    assert {name: canonical_json(values) for name, values in current.items()} == {
        item["title"]: canonical_json(item) for item in accepted}
    plan = index_migrations.ready_plan(store.connection, collection_id)
    for field in ("when", "kind"):
        tag, key = plan.key_columns(field)
        columns = f"{tag},{plan.band_column(field)},{key}"
        index = next(name for name, spec in zip(plan.index_names, plan.indexes, strict=True)
                     if spec.keys[0].field == field)
        expected = sorted(scalar_operand(item[field], "datetime" if field == "when" else "enum") for item in accepted)
        assert sorted(store.connection.execute(
            f"SELECT {columns} FROM {plan.read_relation('main', index=index)}"
        ).fetchall()) == expected


def test_operand_scope_keeps_connections_roles_and_layout_namespaces_separate(tmp_path):
    """Colliding format IDs in another file, generation, collection or ordinal retain their own values."""
    from exomem.collection_store.native_operands import operand_snapshot, register_native_operands

    with closing(sqlite3.connect(tmp_path / "main.sqlite")) as conn, closing(
            sqlite3.connect(tmp_path / "other.sqlite")) as other:
        for opened in (conn, other):
            native_metadata.install(opened, canonical=False)
        conn.execute("ATTACH DATABASE ? AS payload", (str(tmp_path / "payload.sqlite"),))
        conn.execute("CREATE TABLE payload.typed_field_epochs AS SELECT * FROM main.typed_field_epochs")
        conn.execute("CREATE TABLE payload.typed_value_formats AS SELECT * FROM main.typed_value_formats")
        conn.execute("INSERT INTO payload.typed_value_formats VALUES(?,7,0,1,?)", (CID, '"derived"'))
        alternate = "00000000-0000-4000-8000-000000000002"
        for collection_id, generation, ordinal, value in (
                (CID, 7, 0, "main"), (CID, 7, 1, "ordinal"), (CID, 8, 0, "generation"),
                (alternate, 7, 0, "collection")):
            FieldMetadata(conn, collection_id, generation, ordinal).intern(value)
        other.execute("BEGIN")
        FieldMetadata(other, CID, 7, 0).intern("other")
        register_native_operands(conn, databases={FileRole.STORE: "main", FileRole.DERIVED: "payload"})
        register_native_operands(other, databases={FileRole.STORE: "main"})
        layout = native_layout.build(CID, 7, {"kind": FieldSpec("enum", enum=("main",))}, identity="uuid16")
        with operand_snapshot(conn, layout, role=FileRole.STORE):
            for opened, role, collection_id, generation, ordinal, expected in (
                    (conn, FileRole.STORE, CID, 7, 0, "main"),
                    (other, FileRole.STORE, CID, 7, 0, "other"),
                    (conn, FileRole.DERIVED, CID, 7, 0, "derived"),
                    (conn, FileRole.STORE, CID, 8, 0, "generation"),
                    (conn, FileRole.STORE, alternate, 7, 0, "collection"),
                    (conn, FileRole.STORE, CID, 7, 1, "ordinal")):
                assert opened.execute(f"SELECT {native_operands.FUNCTION}(?,?,?,?,?,?,?,?,?,?)",
                    (Codec.ENUM.value, collection_id, generation, ordinal, "enum", "[]", role.value, 1, 128, "key")).fetchone() == (expected,)
            with operand_snapshot(conn, replace(layout, database="payload"), role=FileRole.DERIVED):
                assert conn.execute(f"SELECT {native_operands.FUNCTION}(?,?,?,?,?,?,?,?,?,?)",
                    (Codec.ENUM.value, CID, 7, 0, "enum", "[]", "derived", 1, 128, "key")).fetchone() == ("derived",)
            with operand_snapshot(conn, replace(layout, database="payload"), role=FileRole.STORE):
                assert conn.execute(f"SELECT {native_operands.FUNCTION}(?,?,?,?,?,?,?,?,?,?)",
                    (Codec.ENUM.value, CID, 7, 0, "enum", "[]", "store", 1, 128, "key")).fetchone() == ("main",)
            assert conn.execute(f"SELECT {native_operands.FUNCTION}(?,?,?,?,?,?,?,?,?,?)",
                (Codec.ENUM.value, CID, 7, 0, "enum", "[]", "store", 1, 128, "key")).fetchone() == ("main",)


def test_operand_scope_resets_after_failure_and_keeps_each_access_budget(tmp_path):
    """A warm format cannot authorize a smaller read or survive rollback under a reused format ID."""
    from exomem.collection_store.native_operands import operand_snapshot, register_native_operands

    with closing(sqlite3.connect(tmp_path / "metadata.sqlite")) as conn:
        native_metadata.install(conn, canonical=False)
        conn.commit()
        register_native_operands(conn, databases={FileRole.STORE: "main"})
        layout = native_layout.build(CID, 7, {"kind": FieldSpec("enum", enum=("retained",))}, identity="uuid16")
        statement = f"SELECT {native_operands.FUNCTION}(?,?,?,?,?,?,?,?,?,?)"
        arguments = (Codec.ENUM.value, CID, 7, 0, "enum", "[]", "store", 1)
        conn.execute("BEGIN")
        assert FieldMetadata(conn, CID, 7, 0).intern("retained") == 1
        with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
            with operand_snapshot(conn, layout, role=FileRole.STORE):
                assert conn.execute(statement, (*arguments, 128, "key")).fetchone() == ("retained",)
                conn.execute(statement, (*arguments, 2, "key")).fetchone()
        conn.rollback()
        conn.execute("BEGIN")
        assert FieldMetadata(conn, CID, 7, 0).intern("replacement") == 1
        assert conn.execute(statement, (*arguments, 128, "key")).fetchone() == ("replacement",)
        with operand_snapshot(conn, layout, role=FileRole.STORE):
            assert conn.execute(statement, (*arguments, 128, "key")).fetchone() == ("replacement",)
        conn.rollback()


def test_late_temporal_metadata_rollback_and_attached_file_use_the_payload_generation(tmp_path):
    """Late values and rolled-back format IDs cannot change an existing indexed answer."""
    store = derived_rows.Store(tmp_path, SimpleNamespace(path=tmp_path / "collections.sqlite"))
    try:
        conn = store.conn
        native_metadata.install(conn, canonical=False)
        fields = {"when": FieldSpec("datetime"), "kind": FieldSpec("enum", enum=(True, 1, "one"))}
        layout = native_layout.build(CID, 7, fields, identity="uuid16")
        legacy = build_projection_plan(CID, {name: {"type": spec.type} for name, spec in fields.items()},
                                       (IndexSpec("when", (IndexKey("when"),)), IndexSpec("kind", (IndexKey("kind"),))))
        plan = NativeProjectionPlan(CID, 13, legacy.scalars, legacy.indexes, layout, FileRole.DERIVED)
        for statement in plan.ddl:
            conn.execute(statement)
        conn.execute("BEGIN")
        write(conn, plan, 1, {})
        conn.commit()
        assert conn.execute("SELECT count(*) FROM typed_field_epochs").fetchone() == (0,)

        conn.execute("BEGIN")
        write(conn, plan, 2, {"when": "2024-01-01T00:00:00Z", "kind": True})
        conn.commit()
        conn.execute("BEGIN")
        write(conn, plan, 3, {"when": "2024-01-02t00:00:00,1200+01:00", "kind": "one"})
        assert conn.execute("SELECT max(format_id) FROM typed_value_formats WHERE field_ordinal=1").fetchone() == (2,)
        conn.rollback()
        retained = {"when": "2023-12-31T23:00:00.120000-00:00", "kind": 1.0}
        conn.execute("BEGIN")
        write(conn, plan, 3, retained)
        conn.commit()
        expected = scalar_operand(retained["when"], "datetime")
        assert conn.execute(f"SELECT k0_tag,k0_band,k0_key FROM {plan.read_relation('main')} WHERE row_id=3").fetchone() == expected
        assert conn.execute(f"SELECT row_id FROM {plan.read_relation('main', index=plan.index_names[0])} "
                            "WHERE k0_rank=0 AND k0_tag=? AND k0_band=? AND k0_key=?", expected).fetchall() == [(3,)]
        assert conn.execute("SELECT DISTINCT generation FROM typed_field_epochs").fetchall() == [(7,)]
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        store.close()

    # The ordinary query reader registers the same derived role under another alias.
    with closing(connection._connect(str(tmp_path / "reader.sqlite"))) as reader:
        reader.execute("ATTACH DATABASE ? AS derived", (str(store.path),))
        actual = reader.execute(f"SELECT k0_tag,k0_band,k0_key FROM {plan.read_relation('derived')} WHERE row_id=3").fetchone()
        assert actual == expected
        assert reader.execute("PRAGMA derived.integrity_check").fetchone() == ("ok",)


def test_integer_widening_and_exact_overflow_share_one_native_operand_family(tmp_path):
    """Widening an index without rebuilding must admit fractions and order exact overflow values."""
    with closing(connection._connect(str(tmp_path / "native.sqlite"))) as conn:
        conn.execute("CREATE TABLE items(row_id INTEGER PRIMARY KEY) STRICT")
        native_metadata.install(conn, canonical=False)
        layout = native_layout.build(CID, 7, {"amount": FieldSpec("integer")}, identity="uuid16")
        legacy = build_projection_plan(CID, {"amount": {"type": "integer"}}, (IndexSpec("amount", (IndexKey("amount"),)),))
        initial = NativeProjectionPlan(CID, 13, legacy.scalars, legacy.indexes, layout)
        for statement in initial.ddl:
            conn.execute(statement)
        widened = replace(initial, scalars=(replace(initial.scalars[0], kind="number"),))
        values = [-(2**90), -9223372036854775808, -1.25, -0.0, 0, 0.5, 2**63 - 1, 2**80, float(2**80), 2**80 + 1]
        conn.execute("BEGIN")
        for ordinal, value in enumerate(values, 1):
            conn.execute("INSERT INTO items VALUES(?)", (ordinal,))
            write(conn, widened, ordinal, {"amount": value})
        conn.commit()
        actual = conn.execute(f"SELECT row_id FROM {widened.read_relation('main')} ORDER BY k0_rank,k0_tag,k0_band,k0_key,item_key").fetchall()
        assert actual == [(number,) for number in range(1, len(values) + 1)]
        # Equality ignores the payload subtype while reconstruction retains it.
        tag, band, key = scalar_operand(2**80, "number")
        assert conn.execute(f"SELECT row_id FROM {widened.read_relation('main')} WHERE k0_tag=? AND k0_band=? AND k0_key=? "
                            "ORDER BY item_key", (tag, band, key)).fetchall() == [(8,), (9,)]
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_revised_escaped_and_residual_paths_keep_large_integer_queries_exact(tmp_path):
    """Adding an index after a legal revision cannot narrow an escaped or residual integer through JSON1."""
    with closing(connection._connect(str(tmp_path / "native.sqlite"))) as conn:
        conn.execute("CREATE TABLE items(row_id INTEGER PRIMARY KEY) STRICT")
        native_metadata.install(conn, canonical=False)
        layout = native_layout.build(CID, 7, {"evolving": FieldSpec("string")}, identity="utf8")
        fields = {"evolving.value": {"type": "number"}, "later.value": {"type": "number"}}
        legacy = build_projection_plan(CID, fields, (IndexSpec("evolving", (IndexKey("evolving.value"),)),
                                                     IndexSpec("later", (IndexKey("later.value"),))),
                                       scalar_paths={"evolving.value": ("evolving", "value"), "later.value": ("later", 0, "value")})
        plan = NativeProjectionPlan(CID, 13, legacy.scalars, legacy.indexes, layout)
        for statement in plan.ddl:
            conn.execute(statement)
        values = {"evolving": {"value": 2**100 + 1, "excluded": "unselected"},
                  "later": [{"value": -(2**100) - 1}, {"value": 0}]}
        conn.execute("BEGIN")
        conn.execute("INSERT INTO items VALUES(1)")
        write(conn, plan, 1, values)
        conn.execute("INSERT INTO items VALUES(2)")
        write(conn, plan, 2, {"evolving": "accepted before revision", "later": {"value": 3}})
        conn.execute("INSERT INTO items VALUES(3)")
        write(conn, plan, 3, {"evolving": {"other": []}, "later": [None, {"value": 3}]})
        conn.commit()
        assert conn.execute(f"SELECT k0_tag,k0_band,k0_key,k1_tag,k1_band,k1_key FROM {plan.read_relation('main')} WHERE row_id=1").fetchone() == (
            *scalar_operand(values["evolving"]["value"], "number"), *scalar_operand(values["later"][0]["value"], "number"))
        assert conn.execute(f"SELECT k0_tag,k1_tag FROM {plan.read_relation('main')} WHERE row_id IN (2,3) ORDER BY row_id").fetchall() == [(0, 0), (0, 0)]
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_admitted_temp_relation_retains_only_released_operands_and_identity(tmp_path):
    """Scoped admission cannot put withheld native payloads or values into its TEMP relation."""
    with closing(connection._connect(str(tmp_path / "reader.sqlite"))) as conn:
        layout = native_layout.build(CID, 7, {"amount": FieldSpec("number"), "withheld": FieldSpec("string")}, identity="uuid16")
        legacy = build_projection_plan(CID, {"amount": {"type": "number"}}, (IndexSpec("amount", (IndexKey("amount"),)),))
        plan = NativeProjectionPlan(CID, 13, legacy.scalars, legacy.indexes, layout)
        for statement in plan.create_ddl(temporary=True):
            conn.execute(statement)
        parameters = plan.admitted_parameters(1, CID, 1, {"amount": 2**90 + 1})
        columns = ",".join(parameters)
        conn.execute(f"INSERT INTO temp.{plan.table_name}({columns}) VALUES({','.join('?' for _ in parameters)})", tuple(parameters.values()))
        declared = [column[1] for column in conn.execute(f"PRAGMA temp.table_xinfo({plan.table_name})")]
        assert declared == ["row_id", "item_key", "row_version", "k0_rank", "k0_tag", "k0_band", "k0_key"]
        assert conn.execute(f"SELECT k0_tag,k0_band,k0_key FROM temp.{plan.table_name}").fetchone() == scalar_operand(2**90 + 1, "number")


def test_nested_index_and_selected_read_reject_a_damaged_escape_without_hydrating_siblings(tmp_path):
    """Malformed escape bytes must not become indexed facts or selected values after reopen."""
    with closing(connection._connect(str(tmp_path / "native.sqlite"))) as conn:
        conn.execute("CREATE TABLE items(row_id INTEGER PRIMARY KEY) STRICT")
        native_metadata.install(conn, canonical=False)
        layout = native_layout.build(CID, 7, {"evolving": FieldSpec("string")}, identity="uuid16")
        legacy = build_projection_plan(CID, {"nested": {"type": "number"}}, (IndexSpec("nested", (IndexKey("nested"),)),),
                                       scalar_paths={"nested": ("evolving", "value")})
        plan = NativeProjectionPlan(CID, 13, legacy.scalars, legacy.indexes, layout)
        for statement in plan.ddl:
            conn.execute(statement)
        conn.execute("BEGIN")
        conn.execute("INSERT INTO items VALUES(1)")
        write(conn, plan, 1, {"evolving": {"value": 7}})
        conn.commit()
    with closing(connection._connect(str(tmp_path / "native.sqlite"))) as conn:
        for frame, extent in ((native_rows.ESCAPE_PREFIX + b'{"value":7}', 2),
                              (native_rows.ESCAPE_PREFIX + b'{"value":7.00}', len(canonical_json({"evolving": {"value": 7.0}})))):
            with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
                conn.execute(f"UPDATE {plan.table_name} SET v0=?,values_bytes=? WHERE row_id=1", (frame, extent))
        metadata = FieldMetadata(conn, CID, 7, 0)
        for payload in (b'{"value":7.00}', b'{"value":7.0} '):
            with pytest.raises(TypedStorageError, match="not canonical"):
                native_rows.selected_cell(Codec.TEXT, native_rows.ESCAPE_PREFIX + payload, metadata,
                                          {"value": True}, max_bytes=64, check=lambda: None)
        # A small caller budget still permits a selected field in a much larger canonical container.
        payload = {"excluded": "x" * 2000, "value": 7}
        assert native_rows.selected_cell(Codec.TEXT, native_rows.ESCAPE_PREFIX + canonical_json(payload).encode(), metadata,
                                         {"value": True}, max_bytes=32, check=lambda: None) == {"value": 7}
        assert conn.execute(f"SELECT row_id FROM {plan.read_relation('main')} WHERE k0_key=7").fetchall() == [(1,)]


@pytest.mark.parametrize("representation", ["json", "escape", "residual"])
def test_small_selected_field_in_a_large_native_container_survives_the_reader_limit(tmp_path, representation):
    """An admitted property must remain readable when a withheld sibling exceeds SQLite's cell limit."""
    values = {"title": "Large record", "meta": {"parts": ["x" * 10000 for _ in range(140)], "small": 7}}
    fields = {"title": FieldSpec("string")}
    if representation != "residual":
        fields["meta"] = FieldSpec("object" if representation == "json" else "string")
    with closing(connection._connect(str(tmp_path / "native.sqlite"))) as conn:
        conn.execute("CREATE TABLE items(row_id INTEGER PRIMARY KEY,collection_id TEXT,row_version INTEGER) STRICT")
        native_metadata.install(conn, canonical=False)
        layout = native_layout.build(CID, 7, fields, identity="uuid16")
        plan = build_projection_plan(CID, {"title": {"type": "string"}}, (), generation=13, payload_layout=layout)
        for sql in plan.ddl:
            conn.execute(sql)
        conn.execute("BEGIN")
        conn.execute("INSERT INTO items VALUES(1,?,1)", (CID,))
        write(conn, plan, 1, values)
        conn.commit()
    with closing(connection._connect(str(tmp_path / "native.sqlite"))) as conn:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
        current = replace(layout, current=plan.table_name)
        assert native_rows.selected_current_values(conn, current, 1, {"meta": {"small": True}},
                                                   max_bytes=64, check=lambda: None) == {"meta": {"small": 7}}
        # A large unrelated container must not prevent a scalar dense-read fallback either.
        assert native_rows.selected_current_values(conn, current, 1, {"title": True},
                                                   max_bytes=64, check=lambda: None) == {"title": "Large record"}


def test_dense_selected_budget_counts_unicode_escapes_and_full_temporal_spelling(tmp_path):
    """A corrupt small extent cannot authorize several individually bounded selected scalar values."""
    values = {"label": '雪\n"\\', "other": "another value", "when": "2024-01-01T23:30:00.1200000000+02:00"}
    budget = len(canonical_json(values).encode("utf-8"))
    fields = {"label": FieldSpec("string"), "other": FieldSpec("string"), "when": FieldSpec("datetime")}
    with closing(connection._connect(str(tmp_path / "native.sqlite"))) as conn:
        conn.execute("CREATE TABLE items(row_id INTEGER PRIMARY KEY,collection_id TEXT,row_version INTEGER) STRICT")
        native_metadata.install(conn, canonical=False)
        layout = native_layout.build(CID, 7, fields, identity="uuid16")
        plan = build_projection_plan(CID, {name: {"type": spec.type} for name, spec in fields.items()},
                                     (), generation=13, payload_layout=layout)
        for sql in plan.ddl:
            conn.execute(sql)
        conn.execute("BEGIN")
        conn.execute("INSERT INTO items VALUES(1,?,1)", (CID,))
        write(conn, plan, 1, values)
        conn.commit()
        current = replace(layout, current=plan.table_name)
        selected = dict.fromkeys(values, True)
        columns, decode = native_rows.prepare_dense_selection(conn, current, selected, max_bytes=budget,
                                                              check=lambda: None, semantic_fields={"when"})
        row = conn.execute(f"SELECT {columns} FROM items i JOIN {plan.table_name} t ON t.row_id=i.row_id").fetchone()
        actual = decode(row)
        assert set(actual) == set(values)
        assert actual["label"] == values["label"] and actual["other"] == values["other"]
        assert actual["when"].text_bytes == len(values["when"].encode())
        conn.execute(f"UPDATE {plan.table_name} SET values_bytes=?", (budget - 1,))
        columns, decode = native_rows.prepare_dense_selection(conn, current, selected, max_bytes=budget - 1,
                                                              check=lambda: None, semantic_fields={"when"})
        row = conn.execute(f"SELECT {columns} FROM items i JOIN {plan.table_name} t ON t.row_id=i.row_id").fetchone()
        with pytest.raises(TypedStorageError, match="budget"):
            decode(row)
        # A compact frame must not use the larger caller budget to amplify a corrupt recorded extent.
        from exomem.collection_store import native_temporal

        conn.execute("BEGIN")
        metadata = FieldMetadata(conn, CID, 7, current.fields.index("when"))
        frame = native_temporal.encode("2024-01-01T23:30:00." + "0" * 10000 + "Z", "datetime", metadata)
        conn.execute(f"UPDATE {plan.table_name} SET v2=?,values_bytes=31", (frame,))
        conn.commit()
        for semantic in (frozenset(), {"when"}):
            columns, decode = native_rows.prepare_dense_selection(conn, current, {"when": True}, max_bytes=256 * 1024,
                                                                  check=lambda: None, semantic_fields=semantic)
            row = conn.execute(f"SELECT {columns} FROM items i JOIN {plan.table_name} t ON t.row_id=i.row_id").fetchone()
            with pytest.raises(TypedStorageError, match="budget"):
                decode(row)
        with pytest.raises(TypedStorageError, match="budget"):
            native_rows.selected_current_values(conn, current, 1, {"when": True}, max_bytes=256 * 1024, check=lambda: None)
        with pytest.raises(TypedStorageError, match="budget"):
            native_rows.selected_current_values(conn, current, 1, {"when": True, "later": True},
                                                 max_bytes=256 * 1024, check=lambda: None)


def test_a_warm_dense_scan_returns_and_refuses_exactly_what_the_shared_decoders_do(tmp_path):
    """A scan's inline paths for repeated instants, numbers and text must not drift from the shared decoders,
    including on damaged cells those decoders refuse."""
    fields = {"when": FieldSpec("datetime"), "amount": FieldSpec("number"), "label": FieldSpec("string")}
    sources = [
        {"when": "2024-01-01T23:30:00Z", "amount": 7, "label": "plain"},
        {"when": "2024-01-02T00:30:00+02:00", "amount": -0.0},
        {"when": "2024-01-02T00:30:00.120+02:00", "amount": 2**80, "label": '雪\n"'},
        {"when": "2024-01-01T23:30:00", "amount": 5000.0, "label": 7},
        {"when": "2024-01-03T01:00:00Z", "amount": 1.5, "label": "again"},
        {"when": "2024-01-02T00:30:00+02:00", "amount": -(2**63), "label": None},
        {"amount": 3},
    ]
    with closing(connection._connect(str(tmp_path / "native.sqlite"))) as conn:
        conn.execute("CREATE TABLE items(row_id INTEGER PRIMARY KEY,collection_id TEXT,row_version INTEGER) STRICT")
        native_metadata.install(conn, canonical=False)
        layout = native_layout.build(CID, 7, fields, identity="uuid16")
        plan = build_projection_plan(CID, {name: {"type": spec.type} for name, spec in fields.items()},
                                     (), generation=13, payload_layout=layout)
        for sql in plan.ddl:
            conn.execute(sql)
        conn.execute("BEGIN")
        for row_id, values in enumerate(sources, 1):
            conn.execute("INSERT INTO items VALUES(?,?,1)", (row_id, CID))
            write(conn, plan, row_id, values)
        conn.commit()
        current = replace(layout, current=plan.table_name)
        stored = f"SELECT {','.join((*current.value_columns, 'r'))} FROM {plan.table_name} WHERE row_id=?"

        def scan():
            columns, decode = native_rows.prepare_dense_selection(conn, current, dict.fromkeys(fields, True), max_bytes=4096,
                                                                  check=lambda: None, semantic_fields={"when"})
            return decode, f"SELECT {columns} FROM items i JOIN {plan.table_name} t ON t.row_id=i.row_id WHERE i.row_id=?"

        def shared(row_id):
            return native_rows.decode_row(conn, current, conn.execute(stored, (row_id,)).fetchone())

        def exact(values, *names):
            # Equality alone merges 1 with 1.0 and 0.0 with -0.0.
            return {name: (type(value), repr(value)) for name, value in values.items() if name in names}

        def refusal(decode, argument):
            with pytest.raises(TypedStorageError) as refused:
                decode(argument)
            return str(refused.value)

        # One warm scan reuses each accepted format; a cold scan per row decodes its instant through inspect.
        warm, query = scan()
        for row_id in range(1, len(sources) + 1):
            row = conn.execute(query, (row_id,)).fetchone()
            actual = warm(row)
            assert exact(actual, "when") == exact(scan()[0](row), "when")
            assert exact(actual, "amount", "label") == exact(shared(row_id), "amount", "label")

        when, amount = current.fields.index("when"), current.fields.index("amount")
        original = conn.execute(f"SELECT v{when},v{amount} FROM {plan.table_name} WHERE row_id=2").fetchone()
        _, delta, format_id = struct.unpack(">Bqq", original[0])
        # Microseconds from 0001-01-01 UTC: in the calendar, but its +02:00 local time is past the calendar's end.
        edge = (dt.datetime(9999, 12, 31, 23, 30, tzinfo=dt.UTC) - dt.datetime(1, 1, 1, tzinfo=dt.UTC)) // MICROSECOND
        damaged = ((struct.pack(">Bqq", 1, delta + 1, format_id), original[1]),  # sub-second in a whole-second format
                   (struct.pack(">Bqq", 1, edge - native_metadata.read_epoch(conn, CID, 7, when), format_id),
                    original[1]),  # a local time outside the calendar
                   (struct.pack(">Bqq", 1, delta, 99), original[1]),  # a format that no row defines
                   (struct.pack(">Bqq", 1, delta, 1), original[1]),  # the initial format inside a frame
                   (2**62, original[1]),  # an integer instant past the calendar
                   (original[0], float("inf")))  # a number outside the numeric band
        for cells in damaged:
            conn.execute(f"UPDATE {plan.table_name} SET v{when}=?,v{amount}=? WHERE row_id=2", cells)
            row = conn.execute(query, (2,)).fetchone()
            assert refusal(warm, row) == refusal(scan()[0], row)
        assert refusal(warm, row) == refusal(shared, 2)
