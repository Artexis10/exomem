"""Durable built-in registry population; all state is temporary."""

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from exomem.collection_store import connection, types


def test_register_builtins_is_durable_idempotent_and_exact(tmp_path):
    path = tmp_path / "collections.sqlite"
    with connection.open_writer(path, lease_check=lambda: True) as writer:
        with writer.transaction() as conn:
            types.register_builtins(conn, txn_id=1)
        with writer.transaction() as conn:
            types.register_builtins(conn, txn_id=2)
    with closing(connection.open_reader(path)) as conn:
        assert conn.execute(
            "SELECT name, current_version, builtin FROM collection_types ORDER BY name"
        ).fetchall() == [("planning", 1, 1), ("records", 1, 1)]
        rows = conn.execute(
            "SELECT name, version, declaration_json, declaration_hash, change_class, txn_id "
            "FROM collection_type_versions ORDER BY name"
        ).fetchall()
        assert len(rows) == 2
        for name, version, declaration, digest, change, txn in rows:
            assert version == 1 and txn == 1 and change == "compatible"
            assert json.loads(declaration)["name"] == name
            assert json.loads(declaration)["wire"]
            assert digest == hashlib.sha256(types.declaration_text(name).encode()).hexdigest()


@pytest.mark.parametrize("current", [(2, 1), (1, 0), (1, 1)])
def test_registry_never_silently_adopts_a_different_shipped_version(tmp_path, current):
    with connection.open_writer(tmp_path / "collections.sqlite", lease_check=lambda: True) as writer:
        with writer.transaction() as conn:
            conn.execute("INSERT INTO collection_types VALUES (?, ?, ?)", ("records", *current))
            if current == (1, 1):
                conn.execute(
                    "INSERT INTO collection_type_versions VALUES (?, 1, ?, ?, 'compatible', 1)",
                    ("records", '{"name":"records"}', "a" * 64),
                )
        with pytest.raises(types.CollectionTypeError, match="COLLECTION_TYPE_VERSION_MISMATCH"):
            with writer.transaction() as conn:
                types.register_builtins(conn, txn_id=2)
        assert conn.execute("SELECT COUNT(*) FROM collection_types").fetchone()[0] == 1


def test_registration_rolls_back_with_the_enclosing_mutation(tmp_path):
    with connection.open_writer(tmp_path / "collections.sqlite", lease_check=lambda: True) as writer:
        with pytest.raises(RuntimeError, match="mutation refused"):
            with writer.transaction() as conn:
                types.register_builtins(conn, txn_id=1)
                raise RuntimeError("mutation refused")
        assert conn.execute("SELECT COUNT(*) FROM collection_types").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM collection_type_versions").fetchone()[0] == 0


def test_registration_requires_a_transaction(tmp_path):
    with connection.open_writer(tmp_path / "collections.sqlite", lease_check=lambda: True) as writer:
        with pytest.raises(RuntimeError, match="requires a transaction"):
            types.register_builtins(writer.connection, txn_id=1)
        assert writer.connection.execute("SELECT COUNT(*) FROM collection_types").fetchone()[0] == 0


def test_registration_accepts_named_rows(tmp_path):
    with connection.open_writer(tmp_path / "collections.sqlite", lease_check=lambda: True) as writer:
        writer.connection.row_factory = sqlite3.Row
        with writer.transaction() as conn:
            types.register_builtins(conn, txn_id=1)
        with writer.transaction() as conn:
            types.register_builtins(conn, txn_id=2)
        assert conn.execute("SELECT COUNT(*) FROM collection_type_versions").fetchone()[0] == 2
