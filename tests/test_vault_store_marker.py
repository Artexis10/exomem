"""A store-mode vault says so in its replica, and a file-mode service elsewhere reads it.

OpenSpec make-vault-store-canonical D8. The vault is the migration suite's generated vault, moved
by the real migrate and rollback routes. The second state root stands for the other machine that
shares the vault folder: it has no manifest and no store of this vault, and sees only what the
vault folder carries.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import migrate_apply, rollback_apply, vault  # noqa: F401

from exomem import doctor, server_runtime
from exomem.collection_store import connection, replica
from exomem.vault_store import availability, marker, mode

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)


@pytest.fixture
def other_state_root(tmp_path):
    """The state root of the other machine: a directory that holds nothing of this vault."""
    return tmp_path / "other-state"


def on_state_root(monkeypatch, root):
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(root))


def refused(vault_root):
    with pytest.raises(connection.CollectionStoreError) as error:
        availability.require_servable(vault_root)
    return error.value


def last_reported(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_a_migration_leaves_the_marker_in_the_replica_and_another_state_root_refuses_to_start(
    vault, other_state_root, monkeypatch, capsys  # noqa: F811
):
    """A marker that stays in the owner's state root, so a second machine starts file mode beside
    the store."""
    assert marker.read_replica(vault).state == marker.CLEAR
    capsys.readouterr()

    assert migrate_apply(vault) == 0

    assert last_reported(capsys)["vault_marker"] == marker.STORE
    assert marker.read_replica(vault).state == marker.STORE
    on_state_root(monkeypatch, other_state_root)
    assert not mode.store_mode_active(vault), "the other state root holds no record of store mode"
    error = refused(vault)
    assert error.code == availability.STORE_ELSEWHERE
    assert "exomem store-mode rollback" in error.reason
    with pytest.raises(connection.CollectionStoreError) as started:
        server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)
    assert started.value.code == availability.STORE_ELSEWHERE


def test_a_rollback_removes_the_marker_from_the_replica_and_the_other_state_root_starts(
    vault, other_state_root, monkeypatch, tmp_path, capsys  # noqa: F811
):
    """A marker that outlives the rollback, which locks every other machine out of a file-mode
    vault."""
    assert migrate_apply(vault) == 0
    capsys.readouterr()

    assert rollback_apply(vault, tmp_path / "export") == 0

    assert last_reported(capsys)["vault_marker"] == marker.CLEAR
    assert marker.read_replica(vault).state == marker.CLEAR
    on_state_root(monkeypatch, other_state_root)
    availability.require_servable(vault)


def test_a_vault_that_never_migrated_starts_on_another_state_root(
    vault, other_state_root, monkeypatch  # noqa: F811
):
    """A refusal that reaches the vaults that never entered store mode, whose replica has no
    marker."""
    on_state_root(monkeypatch, other_state_root)
    availability.require_servable(vault)


def test_a_vault_with_no_replica_starts_on_another_state_root(
    vault, other_state_root, monkeypatch  # noqa: F811
):
    """A refusal that waits for a replica that a sync has not delivered yet."""
    shutil.rmtree(replica.replica_path(vault).parent)
    assert marker.read_replica(vault).state == marker.ABSENT
    on_state_root(monkeypatch, other_state_root)
    availability.require_servable(vault)


def test_an_unreadable_replica_does_not_block_a_file_mode_start_and_status_says_why(
    vault, other_state_root, monkeypatch, caplog  # noqa: F811
):
    """A half-synced replica that locks a file-mode owner out of a vault in file mode."""
    assert migrate_apply(vault) == 0
    replica.replica_path(vault).write_bytes(b"half of a database file")
    assert marker.read_replica(vault).state == marker.UNREADABLE
    on_state_root(monkeypatch, other_state_root)

    with caplog.at_level(logging.WARNING, logger=marker.log.name):
        availability.require_servable(vault)

    assert "could not be read" in caplog.text
    reported = marker.report(vault)
    assert reported["vault_marker"] == marker.UNREADABLE
    assert reported["vault_marker_unreadable"]


def test_the_state_root_that_rolled_back_starts_while_its_replica_still_says_store(
    vault, other_state_root, monkeypatch, tmp_path  # noqa: F811
):
    """A failed hand-back after the rollback that leaves the replica saying store, and the owner
    locked out of the only root that could publish the replica that clears it."""
    assert migrate_apply(vault) == 0
    published = tmp_path / "published-at-migration.sqlite"
    shutil.copyfile(replica.replica_path(vault), published)
    assert rollback_apply(vault, tmp_path / "export") == 0
    shutil.copyfile(published, replica.replica_path(vault))
    assert marker.read_replica(vault).state == marker.STORE

    availability.require_servable(vault)

    on_state_root(monkeypatch, other_state_root)
    assert refused(vault).code == availability.STORE_ELSEWHERE


def test_the_status_and_the_doctor_show_the_marker_that_the_replica_carries(
    vault, other_state_root, monkeypatch  # noqa: F811
):
    """A status or a doctor that says nothing of the record that lets another machine start."""
    assert mode.status(vault)["vault_marker"] == marker.CLEAR
    assert doctor._check_vault_store_records(vault) is None
    assert migrate_apply(vault) == 0
    assert mode.status(vault)["vault_marker"] == marker.STORE
    assert doctor._check_vault_store_records(vault) is None, "the root with the store: no finding"

    on_state_root(monkeypatch, other_state_root)
    check = doctor._check_vault_store_records(vault)

    assert (check.status, check.message) == ("fail", availability.ELSEWHERE_REASON)
    assert check.remediation == availability.ELSEWHERE_REMEDY
    assert mode.status(vault)["vault_marker"] == marker.STORE
