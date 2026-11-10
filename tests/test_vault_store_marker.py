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
import sqlite3
import sys
import time
from contextlib import closing

import pytest
from test_collection_store_migration import OPERATOR
from test_managed_collections_migration import OPERATOR_SECRET, coordinator_url  # noqa: F401
from test_vault_store_migration import (  # noqa: F401
    migrate_apply,
    rollback_apply,
    store_mode,
    vault,
)

from exomem import doctor, epistemic_graph, lexstore, memory_refs, server_runtime
from exomem.collection_store import admission, connection, replica, schema
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


def as_root(monkeypatch, tmp_path, name, *, state):
    """Act as one root over the vault: its own state root, lease state and lease holder name."""
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / state))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", name)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / f"lease-{name}"))


def admit_once(vault_root):
    """One offline-owner admission by the current root, which is the takeover that a start runs."""
    def step(session, manager, fence_client):
        return admission.open_store(session, manager, fence_client=fence_client)

    with pytest.MonkeyPatch.context() as shell:
        shell.setenv(OPERATOR, OPERATOR_SECRET)
        assert admission._offline_owner(vault_root, "admit", step)["status"] == "admitted"


def unpublished_clear(vault_root):
    """Whether this state root's store still says that its rollback's clear replica is owed."""
    reader, _ = connection._open_read_only(
        connection.store_path(vault_root), connection.BUSY_TIMEOUT_MS
    )
    with closing(reader):
        return reader.execute(
            "SELECT 1 FROM store_meta WHERE key = ?", (marker.CLEAR_UNPUBLISHED_KEY,)
        ).fetchone() is not None


def rollback_that_publishes_nothing(vault_root, export, monkeypatch):
    """A rollback that commits and whose lease hand-back then fails to copy the replica."""

    def fail(*_args, **_kwargs):
        raise connection.CollectionStoreError("COLLECTION_STORE_FLUSH_PENDING", "drill")

    with monkeypatch.context() as broken:
        broken.setattr(replica, "publish_replica", fail)
        rollback_apply(vault_root, export)


def admit_after_the_lease_expires(vault_root):
    """The admission that follows a failed hand-back, which waits for the owner's short lease."""
    give_up = time.monotonic() + 15
    while True:
        try:
            return admit_once(vault_root)
        except connection.CollectionStoreError as error:
            if error.code != "COLLECTION_STORE_SERVICE_ACTIVE" or time.monotonic() > give_up:
                raise
            time.sleep(0.5)


def index_paths(vault_root):
    """Where the current root keeps its lexical, graph and reference indexes."""
    return [
        find(vault_root)
        for find in (lexstore.lexical_path, epistemic_graph.sidecar_path, memory_refs.sidecar_path)
    ]


def copy_indexes(sources, targets):
    """Give a root the indexes that its own first service start would have built."""
    for source, target in zip(sources, targets, strict=True):
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source)) as found, closing(sqlite3.connect(target)) as copy:
            found.backup(copy)


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


def test_a_replica_from_a_newer_schema_that_says_store_still_refuses_a_file_mode_start(
    vault, other_state_root, monkeypatch  # noqa: F811
):
    """A replica that a newer build published, which this build reads as unreadable and so starts
    file mode beside the store."""
    assert migrate_apply(vault) == 0
    with closing(sqlite3.connect(replica.replica_path(vault))) as newer:
        newer.execute(
            "UPDATE store_meta SET value = ? WHERE key = ?",
            (str(schema.SCHEMA_VERSION + 1), schema.META_SCHEMA_VERSION),
        )
        newer.commit()
    on_state_root(monkeypatch, other_state_root)

    assert marker.read_replica(vault).state == marker.STORE
    assert refused(vault).code == availability.STORE_ELSEWHERE


def test_the_state_root_that_rolled_back_starts_while_its_replica_still_says_store(
    vault, other_state_root, monkeypatch, tmp_path  # noqa: F811
):
    """A failed hand-back after the rollback that leaves the replica saying store, and the owner
    locked out of the only root that could publish the replica that clears it."""
    assert migrate_apply(vault) == 0
    rollback_that_publishes_nothing(vault, tmp_path / "export", monkeypatch)
    assert marker.read_replica(vault).state == marker.STORE
    assert unpublished_clear(vault)

    availability.require_servable(vault)

    on_state_root(monkeypatch, other_state_root)
    assert refused(vault).code == availability.STORE_ELSEWHERE
    as_root(monkeypatch, tmp_path, "service", state="state")
    admit_after_the_lease_expires(vault)
    assert marker.read_replica(vault).state == marker.CLEAR
    assert not unpublished_clear(vault), "the published clear ends this root's say over the marker"


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


def test_a_root_that_continues_from_the_replica_republishes_the_marker_that_the_owner_published(
    vault, tmp_path, monkeypatch  # noqa: F811
):
    """A takeover that drops the marker: the other root's next lease tenure publishes a replica
    that says file, and the store owner's vault is open to it."""
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    admit_once(vault)
    as_root(monkeypatch, tmp_path, "service", state="state")
    assert migrate_apply(vault) == 0
    assert marker.read_replica(vault).state == marker.STORE

    as_root(monkeypatch, tmp_path, "other", state="other-state")
    admit_once(vault)

    assert marker.read_replica(vault).state == marker.STORE
    assert refused(vault).code == availability.STORE_ELSEWHERE


def test_a_root_that_adopted_the_marker_does_not_keep_it_after_the_owner_rolls_back(
    vault, tmp_path, monkeypatch  # noqa: F811
):
    """A copied marker that the adopting root holds on to: its next tenure republishes store, and
    neither root can serve the vault or run the rollback that clears it."""
    assert migrate_apply(vault) == 0
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    admit_once(vault)
    as_root(monkeypatch, tmp_path, "service", state="state")
    assert rollback_apply(vault, tmp_path / "export") == 0
    assert marker.read_replica(vault).state == marker.CLEAR

    as_root(monkeypatch, tmp_path, "other", state="other-state")
    admit_once(vault)

    assert marker.read_replica(vault).state == marker.CLEAR
    availability.require_servable(vault)
    as_root(monkeypatch, tmp_path, "service", state="state")
    availability.require_servable(vault)


def test_a_takeover_copies_the_replicas_marker_but_not_over_the_root_that_decided_it(tmp_path):
    """A migrated or rolled-back root whose decision was not published yet and that loses it to
    the next takeover, which leaves the replica saying the opposite of the store it holds."""
    says_store = {marker.KEY: marker.ACTIVE}
    with connection.open_writer(tmp_path / "store.sqlite", lease_check=lambda: True) as handle:

        def stored_marker():
            return handle.connection.execute(
                "SELECT value FROM store_meta WHERE key = ?", (marker.KEY,)
            ).fetchone()

        with handle.transaction() as tx:
            marker.carry(tx, says_store)
        assert stored_marker() == (marker.ACTIVE,)
        with handle.transaction() as tx:
            marker.carry(tx, {})
        assert stored_marker() is None

        with handle.transaction() as tx:
            tx.execute(
                "INSERT INTO store_mode(id, mode, revision, changed_at, release) "
                "VALUES (1, ?, 1, 'now', 'test')",
                (mode.STORE_MODE,),
            )
            marker.record(tx)
        with handle.transaction() as tx:
            marker.carry(tx, {})
        assert stored_marker() == (marker.ACTIVE,), "the root that migrated keeps its own record"

        with handle.transaction() as tx:
            tx.execute("UPDATE store_mode SET mode = ? WHERE id = 1", (mode.FILE_MODE,))
            marker.clear(tx)
        with handle.transaction() as tx:
            marker.carry(tx, says_store)
        assert stored_marker() is None, "a rollback whose clear is unpublished keeps deciding"
        with handle.transaction() as tx:
            tx.execute(
                "DELETE FROM store_meta WHERE key = ?", (marker.CLEAR_UNPUBLISHED_KEY,)
            )
            marker.carry(tx, says_store)
        assert stored_marker() == (marker.ACTIVE,), "a published clear leaves the marker to others"


def test_a_root_that_rolled_back_carries_the_marker_of_the_root_that_migrated_after_it(
    vault, tmp_path, monkeypatch  # noqa: F811
):
    """A file row left by an old rollback that still decides the marker: the next admission of the
    root that rolled back publishes a replica that says file over the pages the other root holds."""
    assert migrate_apply(vault) == 0
    assert rollback_apply(vault, tmp_path / "export") == 0
    assert not unpublished_clear(vault), "the rollback's hand-back published the clear replica"
    service_indexes = index_paths(vault)
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    admit_once(vault)
    copy_indexes(service_indexes, index_paths(vault))
    assert migrate_apply(vault) == 0
    assert marker.read_replica(vault).state == marker.STORE
    as_root(monkeypatch, tmp_path, "service", state="state")
    assert refused(vault).code == availability.STORE_ELSEWHERE

    admit_once(vault)

    assert marker.read_replica(vault).state == marker.STORE
    assert refused(vault).code == availability.STORE_ELSEWHERE


def test_a_migration_refuses_while_another_root_holds_the_store(
    vault, tmp_path, monkeypatch, capsys  # noqa: F811
):
    """A second migration that forks the pages: the other root moves a vault into store mode that
    this root already holds there, and both roots then report store mode."""
    assert migrate_apply(vault) == 0
    service_indexes = index_paths(vault)
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    admit_once(vault)
    copy_indexes(service_indexes, index_paths(vault))
    capsys.readouterr()

    assert migrate_apply(vault) == 1

    assert capsys.readouterr().err.startswith(availability.STORE_ELSEWHERE)
    assert not mode.store_mode_active(vault)


def test_the_root_that_rolled_back_can_migrate_again_with_the_clear_replica_published_or_not(
    vault, tmp_path, monkeypatch, capsys  # noqa: F811
):
    """A refusal of the root that holds the vault in store mode: after its own rollback, with the
    replica still saying store or not, it is the one root that may move the vault back."""
    assert migrate_apply(vault) == 0
    assert rollback_apply(vault, tmp_path / "export-1") == 0
    assert migrate_apply(vault) == 0
    rollback_that_publishes_nothing(vault, tmp_path / "export-2", monkeypatch)
    assert marker.read_replica(vault).state == marker.STORE
    capsys.readouterr()

    assert store_mode(vault, "migrate", "--dry-run") == 0


def test_the_doctor_reports_an_unreadable_replica_as_its_own_state_in_both_modes(
    vault, other_state_root, monkeypatch  # noqa: F811
):
    """A half-synced replica that the doctor passes as clean on a file-mode root, or that it
    describes as a replica that does not say store yet on the root that holds the store."""
    assert migrate_apply(vault) == 0
    replica.replica_path(vault).write_bytes(b"half of a database file")

    holder = doctor._check_vault_store_records(vault)
    on_state_root(monkeypatch, other_state_root)
    other = doctor._check_vault_store_records(vault)

    assert holder.status == other.status == "warn"
    assert "cannot be read" in holder.message
    assert "see that this vault is in store mode" in holder.message
    assert "cannot be read" in other.message
    assert "cannot tell whether" in other.message
