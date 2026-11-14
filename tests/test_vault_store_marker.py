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
import time
from contextlib import closing

import pytest
from test_collection_store_migration import OPERATOR
from test_collection_store_replica import _commit as commit_a_collection_row
from test_managed_collections_migration import OPERATOR_SECRET, coordinator_url  # noqa: F401
from test_vault_store_migration import (  # noqa: F401
    migrate_apply,
    rollback_apply,
    store_mode,
    vault,
)

from exomem import (
    doctor,
    epistemic_graph,
    lexstore,
    memory_refs,
    server_runtime,
    writer_lease,
)
from exomem.collection_store import admission, connection, replica, schema
from exomem.vault_store import availability, marker, mode


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


def rollback_that_publishes_nothing(vault_root, export, monkeypatch):
    """A rollback that commits and whose lease hand-back then fails to copy the replica."""

    def fail(*_args, **_kwargs):
        raise connection.CollectionStoreError("COLLECTION_STORE_FLUSH_PENDING", "drill")

    with monkeypatch.context() as broken:
        broken.setattr(replica, "publish_replica", fail)
        rollback_apply(vault_root, export)


def rollback_that_dies_after_publishing(vault_root, export, monkeypatch):
    """A rollback whose hand-back copies the replica and then fails, as a crash after it does."""
    publish = replica.publish_replica

    def publish_then_fail(*args, **kwargs):
        publish(*args, **kwargs)
        raise connection.CollectionStoreError("COLLECTION_STORE_FLUSH_PENDING", "drill")

    with monkeypatch.context() as broken:
        broken.setattr(replica, "publish_replica", publish_then_fail)
        rollback_apply(vault_root, export)


def wait_out_the_lease():
    """Wait until no lease holder is live: a hand-back that failed leaves its lease to expire."""
    client = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env())
    give_up = time.monotonic() + 15
    while True:
        held = client.status()
        if held.holder is None or held.expires_at is None or held.expires_at <= time.time():
            return
        assert time.monotonic() < give_up, "the lease did not expire"
        time.sleep(0.25)


def commit_as_current_root(vault_root):
    """One collection commit by the current root's offline owner: it moves the store's head."""
    with pytest.MonkeyPatch.context() as shell:
        shell.setenv(OPERATOR, OPERATOR_SECRET)
        admission.store_mode_route(
            vault_root,
            "store-mode test commit",
            lambda _session, _token, writer: commit_a_collection_row(writer.handle),
        )


def decision_in_store(vault_root):
    """The store-mode decision that the current root's own store carries, or None."""
    meta = marker.read_store_meta(connection.store_path(vault_root)) or {}
    return marker.order(meta).decision


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
    claim = marker.read_replica(vault).order.decision.id
    assert f"exomem store-mode release --apply --claim {claim} --why" in error.reason
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
    assert decision_in_store(vault).mode == mode.FILE_MODE

    availability.require_servable(vault)

    on_state_root(monkeypatch, other_state_root)
    assert refused(vault).code == availability.STORE_ELSEWHERE
    as_root(monkeypatch, tmp_path, "service", state="state")
    admit_after_the_lease_expires(vault)
    assert marker.read_replica(vault).state == marker.CLEAR


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

    claim = marker.read_replica(vault).order.decision.id
    assert (check.status, check.message) == ("fail", availability.ELSEWHERE_REASON)
    assert f"exomem store-mode release --apply --claim {claim}" in check.remediation
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


def test_a_takeover_keeps_the_newest_decision_and_this_roots_own_claim(tmp_path):
    """A takeover that merges by arrival, which lets an old replica overwrite a newer rollback or a
    replica's claim id replace this root's own, so a root reads another's claim as its own."""

    def decided(n, ident, decided_mode):
        found = marker.Decision(n, ident, decided_mode, "then")
        bit = {marker.KEY: marker.ACTIVE} if decided_mode == mode.STORE_MODE else {}
        return {marker.DECISION_KEY: found.encode(), **bit}

    def merged(local, found):
        with connection.open_writer(tmp_path / "store.sqlite", lease_check=lambda: True) as handle:
            with handle.transaction() as tx:
                tx.execute("DELETE FROM store_meta WHERE key LIKE 'vault_store_%'")
                tx.executemany("INSERT INTO store_meta(key, value) VALUES (?, ?)", local.items())
                marker.carry(tx, found, local)
            return marker.read_meta(handle.connection)

    local_file = decided(2, "a", mode.FILE_MODE)
    replica_store = decided(1, "b", mode.STORE_MODE)
    assert marker.order(merged(local_file, replica_store)).decision.id == "a", "newer local wins"
    assert marker.KEY not in merged(local_file, replica_store), "the bit follows the decision"
    assert marker.order(merged(replica_store, local_file)).decision.id == "a", "newer replica wins"
    tie = merged(decided(3, "a", mode.STORE_MODE), decided(3, "b", mode.FILE_MODE))
    assert marker.order(tie).decision.id == "b", "a tie goes to the replica's copy"
    own = {**decided(1, "a", mode.STORE_MODE), marker.CLAIM_KEY: "a"}
    kept = merged(own, {**decided(2, "b", mode.STORE_MODE), marker.CLAIM_KEY: "b"})
    assert kept[marker.CLAIM_KEY] == "a", "the claim never comes from the replica"
    garbled = {marker.KEY: marker.ACTIVE, marker.DECISION_KEY: "not json"}
    assert merged(local_file, garbled) == garbled, "an unreadable replica is carried as it is"
    assert merged(garbled, local_file) == garbled, "an unreadable local order is left as it is"


def test_a_root_that_rolled_back_carries_the_marker_of_the_root_that_migrated_after_it(
    vault, tmp_path, monkeypatch  # noqa: F811
):
    """A file row left by an old rollback that still decides the marker: the next admission of the
    root that rolled back publishes a replica that says file over the pages the other root holds."""
    assert migrate_apply(vault) == 0
    assert rollback_apply(vault, tmp_path / "export") == 0
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


def join_as_other_root(vault_root, tmp_path, monkeypatch):
    """The other root's first admission: its store continues the replica, as a first start does."""
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    admit_once(vault_root)
    as_root(monkeypatch, tmp_path, "service", state="state")


def test_a_rollback_whose_clear_never_published_converges_after_the_other_root_carried_the_marker(
    vault, tmp_path, monkeypatch  # noqa: F811
):
    """A takeover that copies the replica's store marker over a rollback that the replica never
    saw, which refuses a start on both roots with nobody left to publish the clear."""
    join_as_other_root(vault, tmp_path, monkeypatch)
    assert migrate_apply(vault) == 0
    rollback_that_publishes_nothing(vault, tmp_path / "export", monkeypatch)
    wait_out_the_lease()
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    admit_after_the_lease_expires(vault)
    assert marker.read_replica(vault).state == marker.STORE

    as_root(monkeypatch, tmp_path, "service", state="state")
    availability.require_servable(vault)
    admit_after_the_lease_expires(vault)

    assert marker.read_replica(vault).state == marker.CLEAR
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    availability.require_servable(vault)


def test_a_root_that_adopts_the_replica_keeps_its_own_newer_rollback(
    vault, tmp_path, monkeypatch  # noqa: F811
):
    """An adoption that replaces the live store with the replica's bytes and loses the rollback
    decision that only the live store held."""
    join_as_other_root(vault, tmp_path, monkeypatch)
    assert migrate_apply(vault) == 0
    rollback_that_publishes_nothing(vault, tmp_path / "export", monkeypatch)
    wait_out_the_lease()
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    commit_as_current_root(vault)
    assert marker.read_replica(vault).state == marker.STORE

    as_root(monkeypatch, tmp_path, "service", state="state")
    admit_after_the_lease_expires(vault)

    assert marker.read_replica(vault).state == marker.CLEAR
    availability.require_servable(vault)
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    availability.require_servable(vault)


def test_a_root_that_published_its_rollback_and_died_does_not_republish_over_the_next_claim(
    vault, tmp_path, monkeypatch  # noqa: F811
):
    """A hand-back that dies after the clear replica is out and leaves the rolled-back root owing
    a publication, which then copies a clear replica over the store that another root claimed."""
    join_as_other_root(vault, tmp_path, monkeypatch)
    assert migrate_apply(vault) == 0
    service_indexes = index_paths(vault)
    rollback_that_dies_after_publishing(vault, tmp_path / "export", monkeypatch)
    assert marker.read_replica(vault).state == marker.CLEAR
    wait_out_the_lease()
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    copy_indexes(service_indexes, index_paths(vault))
    assert migrate_apply(vault) == 0
    assert marker.read_replica(vault).state == marker.STORE

    as_root(monkeypatch, tmp_path, "service", state="state")
    admit_after_the_lease_expires(vault)

    assert marker.read_replica(vault).state == marker.STORE
    assert refused(vault).code == availability.STORE_ELSEWHERE


def test_a_migration_refuses_inside_the_lease_when_the_replica_gained_a_claim_after_its_preflight(
    vault, tmp_path, monkeypatch, capsys  # noqa: F811
):
    """A preflight that is the only check: two roots pass it before either publishes, and both
    then move the vault into store mode."""
    join_as_other_root(vault, tmp_path, monkeypatch)
    assert migrate_apply(vault) == 0
    service_indexes = index_paths(vault)
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    copy_indexes(service_indexes, index_paths(vault))
    monkeypatch.setattr(availability, "require_no_store_elsewhere", lambda _root: None)
    capsys.readouterr()

    assert migrate_apply(vault) == 1

    assert capsys.readouterr().err.startswith(availability.STORE_ELSEWHERE)
    assert not mode.store_mode_active(vault)


def test_a_migration_whose_claim_cannot_be_published_stays_in_file_mode_and_leaves_one_holder(
    vault, tmp_path, monkeypatch, capsys  # noqa: F811
):
    """An install that comes before its publication: the first root holds pages that no other root
    can see, the second root migrates, and both then hold the vault."""
    join_as_other_root(vault, tmp_path, monkeypatch)
    service_indexes = index_paths(vault)

    real_publish = replica.publish_replica

    def fail_once_the_store_says_store(vault_root, *args, **kwargs):
        # The admission that opens the migration still publishes; the claim's publication does not.
        uri = f"{connection.store_path(vault_root).as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as store:
            row = store.execute(
                "SELECT value FROM store_meta WHERE key = ?", (marker.KEY,)
            ).fetchone()
        if row is not None and row[0] == marker.ACTIVE:
            raise connection.CollectionStoreError("COLLECTION_STORE_FLUSH_PENDING", "drill")
        return real_publish(vault_root, *args, **kwargs)

    with monkeypatch.context() as broken:
        broken.setattr(replica, "publish_replica", fail_once_the_store_says_store)
        assert migrate_apply(vault) == 1
    assert not mode.store_mode_active(vault)
    assert marker.read_replica(vault).state == marker.CLEAR
    wait_out_the_lease()
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    copy_indexes(service_indexes, index_paths(vault))
    assert migrate_apply(vault) == 0
    assert marker.read_replica(vault).state == marker.STORE

    as_root(monkeypatch, tmp_path, "service", state="state")
    admit_after_the_lease_expires(vault)

    assert marker.read_replica(vault).state == marker.STORE
    assert refused(vault).code == availability.STORE_ELSEWHERE
    assert not mode.store_mode_active(vault)
    capsys.readouterr()


def service_migrated_and_other_root_released(vault_root, tmp_path, monkeypatch, capsys):
    """The service migrates, the other root refuses and releases it; returns the claim's id."""
    assert migrate_apply(vault_root) == 0
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    claim = marker.read_replica(vault_root).order.decision.id
    capsys.readouterr()
    assert store_mode(vault_root, "release", "--apply", "--claim", claim, "--why", "gone") == 0
    assert last_reported(capsys)["status"] == "released"
    return claim


def test_the_other_root_releases_a_claim_that_it_names_and_then_starts(
    vault, tmp_path, monkeypatch, capsys  # noqa: F811
):
    """A refusal with no way out: the root that holds the store is gone, and the owner cannot start
    this root or move the vault back without it."""
    assert migrate_apply(vault) == 0
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    claim = marker.read_replica(vault).order.decision.id
    assert claim in refused(vault).reason
    capsys.readouterr()

    assert store_mode(vault, "release", "--dry-run") == 0
    assert claim in last_reported(capsys)["outcome"]
    assert store_mode(vault, "release", "--apply", "--claim", "another-id", "--why", "gone") == 1
    refusal = capsys.readouterr().err
    assert refusal.startswith("VAULT_STORE_RELEASE_CLAIM_MISMATCH")
    assert f"--claim {claim}" in refusal, "the refusal prints the remedy that names the real claim"
    assert marker.read_replica(vault).state == marker.STORE
    assert store_mode(vault, "release", "--apply", "--why", "gone") == 2
    capsys.readouterr()
    assert store_mode(vault, "release", "--apply", "--claim", claim, "--why", "gone") == 0

    released = last_reported(capsys)
    assert released["outcome"].startswith(f"Released claim {claim}")
    assert released["diagnostics"]["vault_marker"] == marker.CLEAR
    availability.require_servable(vault)
    assert store_mode(vault, "release", "--dry-run") == 1
    assert capsys.readouterr().err.startswith("VAULT_STORE_RELEASE_NOTHING_HELD")


def test_a_holder_that_returns_after_a_release_reads_displaced_and_does_not_republish(
    vault, tmp_path, monkeypatch, capsys  # noqa: F811
):
    """A holder that reasserts its claim over the release, so the two roots displace each other
    on every start, or a displaced holder whose rollback decides over the root that serves."""
    service_migrated_and_other_root_released(vault, tmp_path, monkeypatch, capsys)
    released = decision_in_store(vault)
    as_root(monkeypatch, tmp_path, "service", state="state")

    admit_after_the_lease_expires(vault)

    assert marker.read_replica(vault).state == marker.CLEAR
    reported = mode.status(vault)
    assert reported["vault_claim"] == marker.DISPLACED
    assert reported["vault_decision"]["mode"] == mode.FILE_MODE
    check = doctor._check_vault_store_records(vault)
    assert check.status == "warn"
    assert "no longer stands" in check.message
    assert rollback_apply(vault, tmp_path / "export") == 0
    assert marker.read_replica(vault).order.decision == released
    assert decision_in_store(vault) == released
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    availability.require_servable(vault)


def test_a_restored_replica_that_says_store_does_not_lock_out_a_root_that_saw_the_rollback(
    vault, tmp_path, monkeypatch  # noqa: F811
):
    """A sync that brings back an old replica: the root that already merged the rollback starts,
    because it knows the decision is older. Its next publication meets the publisher's own digest
    guard, which flags a replica that this store did not write; the replica republishes only
    through `exomem collections adopt-local`. That guard is separate and is not under test."""
    join_as_other_root(vault, tmp_path, monkeypatch)
    assert migrate_apply(vault) == 0
    saved = replica.replica_path(vault).read_bytes()
    assert rollback_apply(vault, tmp_path / "export") == 0
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    admit_once(vault)
    replica.replica_path(vault).write_bytes(saved)
    assert marker.read_replica(vault).state == marker.STORE

    availability.require_servable(vault)


def test_a_takeover_republishes_a_replica_decision_that_this_build_cannot_read(
    vault, tmp_path, monkeypatch  # noqa: F811
):
    """A takeover that keeps its own empty marker over a decision that a newer build wrote: its
    next publication erases the claim, and the refused root starts beside the live store."""
    join_as_other_root(vault, tmp_path, monkeypatch)
    assert migrate_apply(vault) == 0
    newer = json.dumps({"seq": 9, "holder": "newer-build", "mode": "store"})
    with closing(sqlite3.connect(replica.replica_path(vault))) as copy:
        copy.execute("UPDATE store_meta SET value = ? WHERE key = ?", (newer, marker.DECISION_KEY))
        copy.commit()
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    assert not marker.read_replica(vault).order.known
    assert refused(vault).code == availability.STORE_ELSEWHERE

    commit_as_current_root(vault)

    assert marker.read_replica(vault).state == marker.STORE
    assert marker.read_replica(vault).meta[marker.DECISION_KEY] == newer
    assert refused(vault).code == availability.STORE_ELSEWHERE


def test_a_replica_with_the_bit_and_no_decision_is_released_as_the_legacy_claim(
    vault, tmp_path, monkeypatch, capsys  # noqa: F811
):
    """A replica from a build that wrote only the bit: reading it as clear starts a file-mode
    service beside the store, and a refusal with no working remedy leaves the owner stuck once the
    holder is gone. A decision that no build can read has no claim to name, and says so."""
    assert migrate_apply(vault) == 0
    as_root(monkeypatch, tmp_path, "other", state="other-state")
    legacy_replica = replica.replica_path(vault)

    def rewrite_decision(value):
        with closing(sqlite3.connect(legacy_replica)) as copy:
            if value is None:
                copy.execute("DELETE FROM store_meta WHERE key = ?", (marker.DECISION_KEY,))
            else:
                copy.execute(
                    "UPDATE store_meta SET value = ? WHERE key = ?", (value, marker.DECISION_KEY)
                )
            copy.commit()

    rewrite_decision("a decision from a newer build")
    unreadable = refused(vault)
    assert unreadable.code == availability.STORE_ELSEWHERE
    assert "cannot read" in unreadable.reason
    assert "--claim" not in unreadable.reason
    capsys.readouterr()
    assert store_mode(vault, "release", "--dry-run") == 1
    assert capsys.readouterr().err.startswith("VAULT_STORE_RELEASE_ORDER_UNKNOWN")

    rewrite_decision(None)
    error = refused(vault)
    assert error.code == availability.STORE_ELSEWHERE
    assert "--claim legacy" in error.reason
    assert store_mode(vault, "release", "--dry-run") == 0
    assert "--claim legacy" in last_reported(capsys)["outcome"]
    assert store_mode(vault, "release", "--apply", "--claim", "legacy", "--why", "gone") == 0

    assert last_reported(capsys)["diagnostics"]["vault_marker"] == marker.CLEAR
    availability.require_servable(vault)
