"""The live collection store's placement (move-structured-collections-to-sqlite P1a.3).

The store is canonical data that lives outside the vault: placement class
``external-canonical``. It shares the external state root and its one
resolver seam with machine-local state, but unlike ``external-state`` it is
never rebuilt, reset, deleted or wiped by index maintenance or by the
machine-local state migration and its adoption remedies. It is not part of
the state-migration descriptor set, so its introduction never forces an
offline state migration. All data is invented.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from exomem import reserved_paths, state_migration, state_paths
from exomem.collection_store import connection
from exomem.kbdir import kb_dirname

STORE_DESCRIPTOR = "collection-store"


def _offline_authority():
    return state_migration.assert_offline_migration_authority(source="collection store placement")


def _make_store(vault: Path) -> Path:
    path = connection.store_path(vault)
    with connection.open_writer(path, lease_check=lambda: True) as writer:
        with writer.transaction() as tx:
            tx.execute("INSERT INTO store_meta(key, value) VALUES ('probe', 'invented')")
    return path


def _fingerprint(store: Path) -> dict[str, tuple[int, int, int, str]]:
    """Every member of the store's SQLite family: identity, size, mtime, bytes."""
    found: dict[str, tuple[int, int, int, str]] = {}
    for entry in sorted(store.parent.iterdir()):
        if not entry.name.startswith(store.name):
            continue
        status = entry.stat()
        found[entry.name] = (
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            hashlib.sha256(entry.read_bytes()).hexdigest(),
        )
    assert store.name in found
    return found


def _fresh_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / kb_dirname()).mkdir(parents=True)
    state_migration.reset_state_resolution_cache_for_tests()
    state_migration.migrate_vault_state_offline(vault, authority=_offline_authority())
    return vault


# --- classification ------------------------------------------------------------


def test_the_store_resolves_under_the_external_state_root(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    path = connection.store_path(vault)
    assert path == state_paths.vault_state_dir(vault) / "collections.sqlite"
    assert vault not in path.parents
    assert Path(os.environ["EXOMEM_STATE_ROOT"]) in path.parents


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_the_store_family_is_reserved_as_external_canonical(suffix: str) -> None:
    classified = reserved_paths.classify_logical(f"collections.sqlite{suffix}")
    assert classified.disposition is reserved_paths.PathDisposition.RESERVED
    assert classified.descriptor_id == STORE_DESCRIPTOR
    descriptor = {d.id: d for d in reserved_paths.internal_state_registry()}[STORE_DESCRIPTOR]
    assert descriptor.placement.value == "external-canonical"


def test_the_store_is_not_machine_local_migratable_state() -> None:
    external = {d.id for d in reserved_paths.external_state_descriptors()}
    assert STORE_DESCRIPTOR not in external
    assert STORE_DESCRIPTOR not in state_migration.declared_descriptor_ids()


def test_owner_placement_admits_the_store_only_under_the_external_root(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    route = reserved_paths._owner_anchor(  # noqa: SLF001 - placement authority under test
        vault, connection.store_path(vault), operation="placement regression"
    )
    assert route.external is True
    with pytest.raises(RuntimeError, match="placement"):
        reserved_paths._owner_anchor(  # noqa: SLF001
            vault,
            vault / kb_dirname() / "collections.sqlite",
            operation="placement regression",
        )


# --- state migration never touches the store ---------------------------------------


def test_admission_and_resumed_migration_leave_the_store_untouched(tmp_path: Path) -> None:
    vault = _fresh_vault(tmp_path)
    store = _make_store(vault)
    before = _fingerprint(store)

    state_migration.reset_state_resolution_cache_for_tests()
    state_migration.require_vault_state_ready(vault)
    assert state_migration.scan_vault_state(vault) == {}
    state_migration.migrate_vault_state_offline(vault, authority=_offline_authority())

    assert _fingerprint(store) == before


@pytest.mark.parametrize("keep", ["external", "vault"])
def test_state_adoption_never_deletes_the_store(tmp_path: Path, keep: str) -> None:
    vault = _fresh_vault(tmp_path)
    store = _make_store(vault)
    before = _fingerprint(store)
    # Dual state: an older release left a machine-local family in the vault.
    (vault / kb_dirname() / ".graph-sync.json").write_bytes(b'{"epoch": "stray"}')

    state_migration.reset_state_resolution_cache_for_tests()
    state_migration.migrate_vault_state_offline(vault, authority=_offline_authority(), adopt=keep)

    assert _fingerprint(store) == before, f"--adopt-state {keep} touched the collection store"
    state_migration.reset_state_resolution_cache_for_tests()
    state_migration.require_vault_state_ready(vault)


# --- index rebuild never touches the store --------------------------------------------


def test_index_rebuilds_leave_the_store_untouched(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, lexstore
    from exomem import reconcile as reconcile_module

    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    store = _make_store(vault)
    before = _fingerprint(store)

    assert lexstore.get_store(vault).rebuild_atomic() is True
    reconcile_module.reconcile(vault, dry_run=False, rebuild_graph=True)
    commands.op_maintain_memory(vault, mode="reconcile")

    assert _fingerprint(store) == before


def test_make_store_closes_writer_when_setup_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    opened: list[sqlite3.Connection] = []
    closed: list[sqlite3.Connection] = []

    class FailedSetupConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened.append(self)

        def execute(self, sql, *args):
            if "'probe'" in sql:
                raise sqlite3.OperationalError("invented setup failure")
            return super().execute(sql, *args)

        def close(self):
            closed.append(self)
            return super().close()

    monkeypatch.setattr(connection, "_CONNECTION_FACTORY", FailedSetupConnection)
    try:
        with pytest.raises(sqlite3.OperationalError, match="invented setup failure"):
            _make_store(tmp_path / "vault")
        assert opened
        assert set(opened) <= set(closed)
    finally:
        for conn in opened:
            if conn not in closed:
                conn.close()
