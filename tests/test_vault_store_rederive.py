"""A store-mode commit that changes the schema overlay is settled by a background job.

OpenSpec make-vault-store-canonical D2 and task 2.4. The vault is the migration suite's generated
vault in store mode, with the store's writer bound as the store thread holds it. The projector has
its own suite, so ``project`` stands in for it: it puts each queued file on disk, which the job
waits for. The job is driven one batch at a time, as the store thread's tick drives it. Each case
compares the rows with what the owners derive from the files (``verify.derive_expected``), which is
how migration proves its own rows.
"""

from __future__ import annotations

import functools
import hashlib
import json
import multiprocessing
import os
import sqlite3
from contextlib import closing, contextmanager
from types import SimpleNamespace

import pytest
from hard_kill import KILLED, kill_self
from test_collection_store_runtime import Client
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import (  # noqa: F401
    ALPHA,
    BETA,
    migrate_apply,
    rollback_apply,
    vault,
)
from test_vault_store_read_write import stored, writer  # noqa: F401

from exomem import writer_lease
from exomem.collection_store import connection, preview, runtime
from exomem.collection_store.writer import CollectionWriter
from exomem.vault_store import derive, mode, projection, rederive, verify, write

POLICY = "Knowledge Base/_access.yaml"
PROFILES = "Knowledge Base/_Schema/traversal-profiles.yaml"
READ_ONLY_NOTES = "readonly:\n  - Notes/\n"
GAMMA = "Knowledge Base/Notes/Gamma.md"
GAMMA_TEXT = (
    b"---\ntitle: Gamma\ntype: note\n---\n# Gamma\n\nThe harbour master keeps the tide ledger.\n"
)


def rows(root, sql, *arguments):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute(sql, arguments).fetchall()


def tier(root, rel):
    sql = "SELECT access_tier FROM graph_nodes WHERE path = ? AND kind = 'file'"
    return rows(root, sql, rel)[0][0]


def revision(root):
    return rows(root, "SELECT revision FROM store_mode")[0][0]


@contextmanager
def store_writer(root):
    """A second store writer, once a fixture's is closed, as a restarted service opens one."""
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        yield CollectionWriter(root, handle)


def commit_policy(bound, root, text):
    """The access policy changes as a governed binary does: the file is durable, then the store
    records it."""
    (root / POLICY).write_text(text)
    body = text.encode()
    write.commit(
        bound,
        write.StoreChange(
            write.Origin.GOVERNED,
            "policy",
            binaries=(write.BinaryRecord(POLICY, hashlib.sha256(body).hexdigest(), len(body)),),
        ),
    )


def project(bound, root):
    """What the projector does for each queued intent: make the file say what the store says, then
    clear it."""
    queued = bound.connection.execute(
        "SELECT path, operation FROM store_projection_queue"
    ).fetchall()
    for rel, operation in queued:
        if operation == "write":
            body, mtime_ns = bound.connection.execute(
                "SELECT body, mtime_ns FROM store_pages WHERE path = ?", (rel,)
            ).fetchone()
            target = projection.write_file(root, rel, body, durable=False)
            # The file carries the page's own mtime, so a cold derivation of the files can equal
            # the stored rows.
            os.utime(target, ns=(mtime_ns, mtime_ns))
        else:
            raise AssertionError(f"this suite projects writes only, not {operation}")
    with bound.handle.transaction() as tx:
        tx.execute("DELETE FROM store_projection_queue")


def settle_to_the_end(root, bound, limit=50):
    """Run the job as the tick does, one batch per call, until it reports done; return each
    outcome."""
    outcomes = []
    for _ in range(limit):
        outcomes.append(rederive.settle(root, bound))
        if outcomes[-1] in (rederive.DONE, rederive.IDLE):
            return outcomes
    raise AssertionError(f"the job did not finish in {limit} batches: {outcomes}")


def assert_cold_derivation(root, tmp_path):
    """Every derived table equals what the owners derive from the files now, and the stamps
    agree."""
    expected = tmp_path / "expected.sqlite"
    verify.derive_expected(root, expected)
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        comparison = verify.compare_tables(reader, expected)
        assert [difference.describe() for difference in comparison.differences] == []
        live = dict(reader.execute("SELECT key, value FROM graph_meta"))
    with closing(sqlite3.connect(expected)) as cold:
        cold_stamps = dict(cold.execute("SELECT key, value FROM graph_meta"))
    stamps = ("core_registry_version", "extension_registry_hash", "traversal_profile_hash")
    assert {key: live.get(key) for key in stamps} == {key: cold_stamps.get(key) for key in stamps}


def test_a_policy_commit_returns_before_its_rows_and_the_job_then_matches_a_cold_derivation(
    stored, writer, tmp_path  # noqa: F811
):
    """A write that waits for the whole vault's rows, or a job that leaves the tiers it should
    settle."""
    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert tier(stored, ALPHA) == "read-write"
    commit_policy(writer, stored, READ_ONLY_NOTES)
    project(writer, stored)

    behind = mode.status(stored)
    assert behind["registry"] == rederive.REBUILDING
    assert behind["registry_target_revision"] == revision(stored)
    assert behind["registry_revision"] < behind["registry_target_revision"]
    assert tier(stored, ALPHA) == "read-write", "the commit derived with the policy on the disk"

    settle_to_the_end(stored, writer)

    settled = mode.status(stored)
    assert settled["registry"] == rederive.CURRENT
    assert settled["registry_revision"] == behind["registry_target_revision"]
    assert tier(stored, ALPHA) == tier(stored, BETA) == "readonly"
    assert_cold_derivation(stored, tmp_path)


def test_a_registry_page_commit_settles_the_graph_stamps_that_a_cold_derivation_gives(
    stored, writer, tmp_path  # noqa: F811
):
    """A traversal profile that the commit stored and rows that still carry the old profile's
    hash."""
    old = rows(stored, "SELECT value FROM graph_meta WHERE key = 'traversal_profile_hash'")[0][0]
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED, "profiles", puts=(write.PagePut(PROFILES, b"profiles: {}\n"),)
        ),
    )
    project(writer, stored)
    assert mode.status(stored)["registry"] == rederive.REBUILDING

    settle_to_the_end(stored, writer)

    new = rows(stored, "SELECT value FROM graph_meta WHERE key = 'traversal_profile_hash'")[0][0]
    assert new != old
    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert_cold_derivation(stored, tmp_path)


def test_a_page_outside_the_overlay_leaves_the_registry_state_current(stored, writer):  # noqa: F811
    """A job that runs a whole-vault pass for every commit, as it cannot tell an overlay page from
    a note."""
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "note", puts=(write.PagePut(GAMMA, GAMMA_TEXT),)),
    )
    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert rederive.settle(stored, writer) == rederive.IDLE


def test_a_kept_registry_version_is_no_overlay_change(stored, writer):  # noqa: F811
    """A whole-vault pass for each registry save, which keeps the prior version in the history
    folder that no loader reads."""
    kept = "Knowledge Base/_Schema/history/traversal-profiles/20261110T120000Z-0a1b2c3d.yaml"
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED, "kept", puts=(write.PagePut(kept, b"profiles: {}\n"),)
        ),
    )
    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert rederive.settle(stored, writer) == rederive.IDLE


def test_each_batch_of_a_pass_moves_the_store_revision(stored, writer):  # noqa: F811
    """A reader cache keyed on the store revision that keeps serving the rows a pass has since
    changed."""
    commit_policy(writer, stored, READ_ONLY_NOTES)
    project(writer, stored)
    before = revision(stored)

    outcomes = settle_to_the_end(stored, writer)

    assert outcomes[-1] == rederive.DONE
    assert revision(stored) == before + len(outcomes)
    assert mode.status(stored)["registry"] == rederive.CURRENT


def test_the_job_waits_until_the_projector_has_put_the_overlay_file_on_disk(
    stored, writer  # noqa: F811
):
    """Rows derived from the old policy file on disk and recorded as current, while the new one is
    still queued."""
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED, "profiles", puts=(write.PagePut(PROFILES, b"profiles: {}\n"),)
        ),
    )
    assert rederive.settle(stored, writer) == rederive.WAITING
    waiting = mode.status(stored)
    assert (waiting["registry"], waiting["registry_waiting_on_paths"]) == (rederive.REBUILDING, 1)
    project(writer, stored)
    assert rederive.settle(stored, writer) != rederive.WAITING
    assert mode.status(stored)["registry_waiting_on_paths"] == 0


def test_a_held_overlay_file_keeps_the_job_waiting_and_status_counts_it(
    stored, writer  # noqa: F811
):
    """A held edit of a registry file that keeps the vault rebuilding with nothing in status that
    says what it waits for."""
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED, "profiles", puts=(write.PagePut(PROFILES, b"profiles: {}\n"),)
        ),
    )
    project(writer, stored)
    with writer.handle.transaction() as tx:
        tx.execute(
            "INSERT INTO store_held(path, kind, detected_at) VALUES (?, 'edit', 'now')", (PROFILES,)
        )

    assert rederive.settle(stored, writer) == rederive.WAITING
    waiting = mode.status(stored)
    assert (waiting["registry"], waiting["registry_waiting_on_paths"]) == (rederive.REBUILDING, 1)


def test_a_commit_between_two_batches_is_not_blocked_and_its_page_is_derived_by_the_new_policy(
    stored, writer, tmp_path, monkeypatch  # noqa: F811
):
    """A write that waits for the job to finish, or a page written mid-pass that the pass then
    misses."""
    monkeypatch.setattr(rederive, "BATCH_PAGES", 1)
    commit_policy(writer, stored, READ_ONLY_NOTES)
    project(writer, stored)
    assert rederive.settle(stored, writer) == rederive.ADVANCED
    during = revision(stored)

    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "note", puts=(write.PagePut(GAMMA, GAMMA_TEXT),)),
    )
    project(writer, stored)
    assert revision(stored) == during + 1, "the write committed while the pass was part way"
    assert mode.status(stored)["registry"] == rederive.REBUILDING

    settle_to_the_end(stored, writer, limit=200)

    assert tier(stored, GAMMA) == "readonly"
    assert_cold_derivation(stored, tmp_path)


def test_a_second_overlay_commit_during_a_pass_restarts_it_at_the_new_target(
    stored, writer, tmp_path, monkeypatch  # noqa: F811
):
    """A pass that finishes for the old overlay and records the new revision as reflected."""
    monkeypatch.setattr(rederive, "BATCH_PAGES", 1)
    commit_policy(writer, stored, READ_ONLY_NOTES)
    project(writer, stored)
    assert rederive.settle(stored, writer) == rederive.ADVANCED
    commit_policy(writer, stored, "readonly:\n  - Notes/\n  - Sources/\n")
    second_overlay = revision(stored)
    project(writer, stored)

    settle_to_the_end(stored, writer, limit=200)

    assert mode.status(stored)["registry_revision"] == second_overlay
    assert_cold_derivation(stored, tmp_path)


def test_the_store_threads_tick_derives_one_batch_per_call_and_asks_to_come_back_at_once_until_done(
    stored, writer, monkeypatch  # noqa: F811
):
    """A tick that never reaches the job, or one that asks to wait a second between every batch of
    a big vault."""
    monkeypatch.setattr(rederive, "BATCH_PAGES", 1)
    commit_policy(writer, stored, READ_ONLY_NOTES)
    project(writer, stored)

    @contextmanager
    def checkout(root, handle):
        yield writer

    # The store thread's checkout needs a lease manager; this suite holds the writer itself, as
    # the thread does.
    monkeypatch.setattr(preview, "preview_store", checkout)
    thread = SimpleNamespace(
        root=stored,
        runtime=SimpleNamespace(path=connection.store_path(stored), reader_pool=lambda: None),
        _idle_released=lambda: False,
        _write=lambda method, *arguments: method(*arguments),
    )

    waits = [runtime.StoreServer._registry_tick(thread) for _ in range(200)]
    first_idle = waits.index(runtime.IMPORT_IDLE_SECONDS)

    assert set(waits[:first_idle]) == {0.0}, "each batch but the last asks to run again at once"
    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert tier(stored, ALPHA) == "readonly"


def _settle_and_die(environment, root, nth):
    """The job in a fresh interpreter that the operating system kills inside its ``nth`` batch."""
    os.environ.clear()
    os.environ.update(environment)
    rederive.BATCH_PAGES = 1
    seen = []

    def trace(statement):
        if statement.startswith("INSERT INTO store_meta"):
            seen.append(statement)
            if len(seen) == nth:
                kill_self()

    with store_writer(root) as batch_writer:
        batch_writer.handle.connection.set_trace_callback(trace)
        while rederive.settle(root, batch_writer) != rederive.IDLE:
            pass
    os._exit(0)


def test_a_kill_inside_a_batch_leaves_the_rows_behind_the_store_and_the_next_start_resumes(
    stored, tmp_path  # noqa: F811
):
    """Rows ahead of the recorded revision, or a restart that begins again or skips the pages that
    the kill cut."""
    with store_writer(stored) as before_kill:
        commit_policy(before_kill, stored, READ_ONLY_NOTES)
        project(before_kill, stored)
    child = multiprocessing.get_context("spawn").Process(
        target=_settle_and_die, args=(dict(os.environ), stored, 2)
    )
    child.start()
    child.join(180)
    assert child.exitcode == KILLED

    status = mode.status(stored)
    assert status["registry"] == rederive.REBUILDING
    cursor = rows(stored, "SELECT value FROM store_meta WHERE key = ?", rederive.PASS)
    assert cursor, "the first batch committed its cursor before the kill"
    derived = {
        rel: value
        for rel, value in rows(
            stored, "SELECT path, access_tier FROM graph_nodes WHERE kind = 'file'"
        )
        if rel in (ALPHA, BETA)
    }
    assert "read-write" in derived.values(), "the killed batch's rows were never committed"
    with closing(sqlite3.connect(connection.store_path(stored))) as database:
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    with store_writer(stored) as restarted:
        settle_to_the_end(stored, restarted, limit=200)

    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert tier(stored, ALPHA) == tier(stored, BETA) == "readonly"
    assert_cold_derivation(stored, tmp_path)


def test_a_failure_in_a_batch_rolls_back_its_rows_and_its_cursor(
    stored, writer, monkeypatch  # noqa: F811
):
    """A batch whose rows commit without its cursor, so the next run derives them again over a
    moved state."""
    monkeypatch.setattr(rederive, "BATCH_PAGES", 1)
    commit_policy(writer, stored, READ_ONLY_NOTES)
    project(writer, stored)
    real = derive.rederive_pages
    calls = []

    def failing(tx, root, paths):
        real(tx, root, paths)
        calls.append(paths)
        if len(calls) == 2:
            raise RuntimeError("the batch failed after its rows were written")

    monkeypatch.setattr(derive, "rederive_pages", failing)
    assert rederive.settle(stored, writer) == rederive.ADVANCED
    cursor = rows(stored, "SELECT value FROM store_meta WHERE key = ?", rederive.PASS)
    with pytest.raises(RuntimeError, match="the batch failed"):
        rederive.settle(stored, writer)
    assert rows(stored, "SELECT value FROM store_meta WHERE key = ?", rederive.PASS) == cursor
    assert mode.status(stored)["registry"] == rederive.REBUILDING


def test_migration_records_the_overlay_as_reflected_and_rollback_removes_every_key(
    stored, tmp_path  # noqa: F811
):
    """Keys that outlive store mode, so a later migration reads a pass of another vault life."""
    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert rederive.KEYS[1] in dict(rows(stored, "SELECT key, value FROM store_meta"))
    assert rollback_apply(stored, tmp_path / "export") == 0
    kept = dict(rows(stored, "SELECT key, value FROM store_meta"))
    assert not set(rederive.KEYS) & set(kept)


def test_a_pass_over_an_unknown_state_keeps_its_target_when_a_commit_lands(
    stored, writer, monkeypatch  # noqa: F811
):
    """A pass that restarts at the new head on every commit, so it never finishes on a vault that
    is written to."""
    monkeypatch.setattr(rederive, "BATCH_PAGES", 1)
    with writer.handle.transaction() as tx:
        tx.execute("DELETE FROM store_meta WHERE key = ?", (rederive.REGISTRY_REVISION,))
    assert mode.status(stored)["registry"] == mode.UNKNOWN

    def cursor():
        found = rows(stored, "SELECT value FROM store_meta WHERE key = ?", rederive.PASS)
        return json.loads(found[0][0])

    assert rederive.settle(stored, writer) == rederive.ADVANCED
    began = cursor()
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "note", puts=(write.PagePut(GAMMA, GAMMA_TEXT),)),
    )
    project(writer, stored)
    assert rederive.settle(stored, writer) == rederive.ADVANCED

    assert cursor()["target"] == began["target"] < revision(stored)
    assert cursor()["after"] > began["after"]
    settle_to_the_end(stored, writer, limit=200)
    settled = mode.status(stored)
    assert (settled["registry"], settled["registry_revision"]) == (
        rederive.CURRENT,
        began["target"],
    )


def test_an_overlay_revision_that_is_not_a_number_reads_unknown_until_a_pass_settles_it(
    stored, writer  # noqa: F811
):
    """A damaged record that reads as no overlay change, which marks stale rows as current."""
    with writer.handle.transaction() as tx:
        tx.execute(
            "UPDATE store_meta SET value = 'damaged' WHERE key = ?", (rederive.OVERLAY_REVISION,)
        )
    assert mode.status(stored)["registry"] == mode.UNKNOWN

    settle_to_the_end(stored, writer, limit=200)

    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert rederive.settle(stored, writer) == rederive.IDLE


@pytest.fixture
def served_store(tmp_path, monkeypatch):
    """The store thread's own objects over a store in store mode that holds no pages: a real
    lease manager and runtime, and the thread's real ``_write``."""
    root = tmp_path / "bare-vault"
    (root / "Knowledge Base").mkdir(parents=True)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(
            url="https://lease.example",
            vault_id="vault",
            replica_id="local",
            state_dir=tmp_path / "lease",
            idle_release_seconds=1,
            ttl_seconds=3,
        ),
        client=Client(),
    )
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        with handle.transaction() as tx:
            tx.execute(
                "INSERT INTO store_mode(id, mode, revision, changed_at, release) "
                "VALUES (1, 'store', 5, 'now', 'test')"
            )
            for key in (rederive.OVERLAY_REVISION, rederive.REGISTRY_REVISION):
                tx.execute("INSERT INTO store_meta(key, value) VALUES (?, '5')", (key,))
            # As this build's migration records it: the store holds this build's owner objects.
            tx.execute(
                "INSERT INTO store_meta(key, value) VALUES (?, ?)",
                (rederive.OWNER_SCHEMA, str(rederive.OWNER_SCHEMA_VERSION)),
            )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: manager)
    # The state manifest of a real migration is another suite's subject; this one needs the answer.
    monkeypatch.setattr(mode, "store_mode_for_reads", lambda _root: True)
    thread = SimpleNamespace(
        root=root,
        runtime=runtime.CollectionStoreRuntime(root, manager, authority_check=lambda: True),
        _idle_released=lambda: False,
    )
    thread._write = functools.partial(runtime.StoreServer._write, thread)
    yield thread
    if thread.runtime._handle is not None:
        thread.runtime._handle.close()
    manager._stop.set()


def test_idle_registry_ticks_leave_the_commit_generation_and_the_idle_clock_alone(served_store):
    """A tick with nothing to derive that takes the vault's mutation boundary every second, which
    advances the commit generation and keeps the lease from ever idle-releasing."""
    manager = served_store.runtime.manager
    manager._last_activity_monotonic -= 10
    clock = manager._last_activity_monotonic
    generation = writer_lease.read_commit_generation(served_store.root)

    waits = [runtime.StoreServer._registry_tick(served_store) for _ in range(3)]

    assert waits == [runtime.IMPORT_IDLE_SECONDS] * 3
    assert writer_lease.read_commit_generation(served_store.root) == generation
    assert manager._last_activity_monotonic == clock


def test_a_registry_tick_runs_a_batch_once_the_overlay_is_on_disk_and_not_before(served_store):
    """A tick that never derives the pages, or one that derives them before the projector has put
    the overlay file on disk."""
    root = served_store.root
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        with handle.transaction() as tx:
            tx.execute(
                "UPDATE store_meta SET value = '6' WHERE key = ?", (rederive.OVERLAY_REVISION,)
            )
            tx.execute(
                "INSERT INTO store_projection_queue(path, revision, operation, queued_at) "
                "VALUES (?, 6, 'write', 'now')",
                (PROFILES,),
            )
    generation = writer_lease.read_commit_generation(root)

    assert runtime.StoreServer._registry_tick(served_store) == runtime.DERIVED_RETRY_SECONDS
    assert writer_lease.read_commit_generation(root) == generation
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        with handle.transaction() as tx:
            tx.execute("DELETE FROM store_projection_queue")

    runtime.StoreServer._registry_tick(served_store)

    assert writer_lease.read_commit_generation(root) == generation + 1
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert rederive.read(reader).state == rederive.CURRENT
