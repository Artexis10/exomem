"""Whether a vault is in store mode: the manifest's id and the store's mode row must both say so."""

from __future__ import annotations

import sqlite3

import pytest

from exomem import state_migration
from exomem.collection_store import connection, schema
from exomem.vault_store.mode import store_mode_active

AT = "2026-11-08T00:00:00Z"


def _record_id(vault, *, active=True):
    state_migration.record_vault_store_compatibility(vault, authority_check=lambda: True, active=active)


def _store(vault):
    return connection.open_writer(connection.store_path(vault), lease_check=lambda: True)


def _set_mode(writer, mode):
    with writer.transaction() as tx:
        tx.execute("DELETE FROM store_mode")
        tx.execute("INSERT INTO store_mode(id, mode, revision, changed_at, release) VALUES (1, ?, 1, ?, 'test')",
                   (mode, AT))


def test_a_vault_that_never_recorded_the_id_is_in_file_mode_and_its_store_stays_unopened(vault):
    """Defect: a vault with no store reads as store mode, or the probe fails on a vault that has no store."""
    assert not connection.store_path(vault).exists()
    assert store_mode_active(vault) is False


def test_a_store_that_says_store_without_the_recorded_id_is_file_mode(vault):
    """Defect: a store file restored onto another vault flips that vault to store mode with no pages in it."""
    with _store(vault) as writer:
        _set_mode(writer, "store")
    assert store_mode_active(vault) is False


def test_the_recorded_id_without_a_store_file_is_file_mode(vault):
    """Defect: a vault restored onto a host without its store reads as store mode and serves no pages."""
    _record_id(vault)
    assert store_mode_active(vault) is False


def test_the_recorded_id_with_a_store_older_than_schema_15_is_file_mode(vault, monkeypatch):
    """Defect: an interrupted activation, which recorded the id before the store migrated, reads as store mode."""
    with monkeypatch.context() as earlier:
        earlier.setattr(schema, "SCHEMA_VERSION", 14)
        with _store(vault) as writer:
            assert schema.schema_version(writer.connection) == 14
    _record_id(vault)
    assert store_mode_active(vault) is False


def test_the_recorded_id_before_the_activation_commit_is_file_mode(vault):
    """Defect: a crash between recording the id and committing the mode row leaves the vault half in store mode."""
    with _store(vault):
        pass
    _record_id(vault)
    assert store_mode_active(vault) is False


def test_the_recorded_id_after_a_rollback_committed_is_file_mode(vault):
    """Defect: a rollback that committed but did not yet drop the id leaves the vault in store mode."""
    with _store(vault) as writer:
        _set_mode(writer, "file")
    _record_id(vault)
    assert store_mode_active(vault) is False


def test_the_recorded_id_and_the_store_mode_row_together_are_store_mode(vault):
    """Defect: no complete activation is ever recognised, so a migrated vault serves from files."""
    with _store(vault) as writer:
        _set_mode(writer, "store")
    _record_id(vault)
    assert store_mode_active(vault) is True


def test_the_answer_follows_each_change_of_either_record_without_a_cache(vault):
    """Defect: a cached answer keeps a rolled-back vault in store mode, or a migrated one in file mode."""
    with _store(vault) as writer:
        _set_mode(writer, "store")
        _record_id(vault)
        assert store_mode_active(vault) is True
        _set_mode(writer, "file")
        assert store_mode_active(vault) is False
        _set_mode(writer, "store")
        assert store_mode_active(vault) is True
        _record_id(vault, active=False)
        assert store_mode_active(vault) is False


def test_a_store_that_cannot_be_read_raises_instead_of_reading_as_file_mode(vault):
    """Defect: a corrupt store reads as file mode, so a service writes Markdown into a vault that the store owns."""
    _record_id(vault)
    connection.store_path(vault).parent.mkdir(parents=True, exist_ok=True)
    connection.store_path(vault).write_bytes(b"not a database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        store_mode_active(vault)


def test_dropping_the_id_leaves_the_other_collection_store_ids_recorded(vault):
    """Defect: a rollback drops the collection store's own ids with the pages' id, which an export alone may retire."""
    state_migration.record_collection_store_compatibility(vault, authority_check=lambda: True)
    _record_id(vault)
    recorded = set(state_migration.recorded_descriptor_ids(vault))
    assert {"vault-store-v1", "collections-store-v1"} <= recorded

    _record_id(vault, active=False)

    recorded = set(state_migration.recorded_descriptor_ids(vault))
    assert "vault-store-v1" not in recorded and "collections-store-v1" in recorded


def test_a_collection_export_retirement_keeps_the_pages_id(vault):
    """Defect: retiring the collection store's ids also drops the id that makes the vault store mode."""
    state_migration.record_collection_store_compatibility(vault, authority_check=lambda: True)
    _record_id(vault)

    state_migration.retire_collection_store_compatibility(vault, authority_check=lambda: True)

    recorded = set(state_migration.recorded_descriptor_ids(vault))
    assert "vault-store-v1" in recorded and "collections-store-v1" not in recorded
