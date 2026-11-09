"""A store-mode commit that changes the schema overlay is settled by a background job.

OpenSpec make-vault-store-canonical D2 and task 2.4. The vault is the migration suite's generated vault in
store mode, with the store's writer bound as the store thread holds it. The projector has its own suite, so
``project`` stands in for it: it puts each queued file on disk, which the job waits for. The job is driven one
batch at a time, as the store thread's tick drives it. Each case compares the rows with what the owners derive
from the files (``verify.derive_expected``), which is how migration proves its own rows.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import signal
import sqlite3
import sys
from contextlib import closing, contextmanager

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, BETA, migrate_apply, rollback_apply, vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401

from exomem.collection_store import connection
from exomem.collection_store.writer import CollectionWriter
from exomem.vault_store import derive, mode, projection, rederive, verify, write

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

POLICY = "Knowledge Base/_access.yaml"
PROFILES = "Knowledge Base/_Schema/traversal-profiles.yaml"
READ_ONLY_NOTES = "readonly:\n  - Notes/\n"
GAMMA = "Knowledge Base/Notes/Gamma.md"
GAMMA_TEXT = b"---\ntitle: Gamma\ntype: note\n---\n# Gamma\n\nThe harbour master keeps the tide ledger.\n"


def rows(root, sql, *arguments):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute(sql, arguments).fetchall()


def tier(root, rel):
    return rows(root, "SELECT access_tier FROM graph_nodes WHERE path = ? AND kind = 'file'", rel)[0][0]


def revision(root):
    return rows(root, "SELECT revision FROM store_mode")[0][0]


@contextmanager
def store_writer(root):
    """A second store writer, once the one that a fixture holds is closed, as a restarted service opens one."""
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        yield CollectionWriter(root, handle)


def commit_policy(writer, root, text):
    """The access policy changes as a governed binary does: the file is durable, then the store records it."""
    (root / POLICY).write_text(text)
    body = text.encode()
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED,
            "policy",
            binaries=(write.BinaryRecord(POLICY, hashlib.sha256(body).hexdigest(), len(body)),),
        ),
    )


def project(writer, root):
    """What the projector does for each queued intent: make the file say what the store says, then clear it."""
    queued = writer.connection.execute("SELECT path, operation FROM store_projection_queue").fetchall()
    for rel, operation in queued:
        if operation == "write":
            body, mtime_ns = writer.connection.execute(
                "SELECT body, mtime_ns FROM store_pages WHERE path = ?", (rel,)
            ).fetchone()
            target = projection.write_file(root, rel, body, durable=False)
            # The file carries the page's own mtime, so a cold derivation of the files can equal the stored rows.
            os.utime(target, ns=(mtime_ns, mtime_ns))
        else:
            raise AssertionError(f"this suite projects writes only, not {operation}")
    with writer.handle.transaction() as tx:
        tx.execute("DELETE FROM store_projection_queue")


def settle_to_the_end(root, writer, limit=50):
    """Run the job as the tick does, one batch per call, until it reports done; return each outcome."""
    outcomes = []
    for _ in range(limit):
        outcomes.append(rederive.settle(root, writer))
        if outcomes[-1] in (rederive.DONE, rederive.IDLE):
            return outcomes
    raise AssertionError(f"the job did not finish in {limit} batches: {outcomes}")


def assert_cold_derivation(root, tmp_path):
    """Every derived table equals what the owners derive from the files now, and the stamps agree."""
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


def test_a_policy_commit_returns_before_its_rows_and_the_job_then_derives_what_a_cold_derivation_gives(
    stored, writer, tmp_path  # noqa: F811
):
    """A write that waits for the whole vault's rows, or a job that leaves the tiers it was meant to settle."""
    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert tier(stored, ALPHA) == "read-write"
    commit_policy(writer, stored, READ_ONLY_NOTES)
    project(writer, stored)

    behind = mode.status(stored)
    assert behind["registry"] == rederive.REBUILDING
    assert behind["registry_target_revision"] == revision(stored)
    assert behind["registry_revision"] < behind["registry_target_revision"]
    assert tier(stored, ALPHA) == "read-write", "the commit derived with the policy that the disk held"

    settle_to_the_end(stored, writer)

    settled = mode.status(stored)
    assert settled["registry"] == rederive.CURRENT
    assert settled["registry_revision"] == behind["registry_target_revision"]
    assert tier(stored, ALPHA) == tier(stored, BETA) == "readonly"
    assert_cold_derivation(stored, tmp_path)


def test_a_registry_page_commit_settles_the_graph_stamps_that_a_cold_derivation_gives(
    stored, writer, tmp_path  # noqa: F811
):
    """A traversal profile that the commit stored and the rows that still carry the old profile's hash."""
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
    """A job that runs a whole-vault pass for every commit, as it cannot tell an overlay page from a note."""
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "note", puts=(write.PagePut(GAMMA, GAMMA_TEXT),)),
    )
    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert rederive.settle(stored, writer) == rederive.IDLE


def test_the_job_waits_until_the_projector_has_put_the_overlay_file_on_disk(stored, writer):  # noqa: F811
    """Rows derived from the old policy file on disk and recorded as current, while the new one is still queued."""
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED, "profiles", puts=(write.PagePut(PROFILES, b"profiles: {}\n"),)
        ),
    )
    assert rederive.settle(stored, writer) == rederive.WAITING
    assert mode.status(stored)["registry"] == rederive.REBUILDING
    project(writer, stored)
    assert rederive.settle(stored, writer) != rederive.WAITING


def test_a_commit_between_two_batches_is_not_blocked_and_its_page_is_derived_by_the_new_policy(
    stored, writer, tmp_path, monkeypatch  # noqa: F811
):
    """A write that waits for the job to finish, or a page written mid-pass that the pass then misses."""
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
    project(writer, stored)

    settle_to_the_end(stored, writer, limit=200)

    assert mode.status(stored)["registry_revision"] == revision(stored)
    assert_cold_derivation(stored, tmp_path)


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
                os.kill(os.getpid(), signal.SIGKILL)

    with store_writer(root) as batch_writer:
        batch_writer.handle.connection.set_trace_callback(trace)
        while rederive.settle(root, batch_writer) != rederive.IDLE:
            pass
    os._exit(0)


def test_a_kill_inside_a_batch_leaves_the_rows_behind_the_store_and_the_next_start_resumes(
    stored, tmp_path  # noqa: F811
):
    """Rows ahead of the recorded revision, or a restart that begins again or skips the pages that the kill cut."""
    with store_writer(stored) as before_kill:
        commit_policy(before_kill, stored, READ_ONLY_NOTES)
        project(before_kill, stored)
    child = multiprocessing.get_context("spawn").Process(
        target=_settle_and_die, args=(dict(os.environ), stored, 2)
    )
    child.start()
    child.join(180)
    assert child.exitcode == -signal.SIGKILL

    status = mode.status(stored)
    assert status["registry"] == rederive.REBUILDING
    cursor = rows(stored, "SELECT value FROM store_meta WHERE key = ?", rederive.PASS)
    assert cursor, "the first batch committed its cursor before the kill"
    derived = {
        rel: value
        for rel, value in rows(stored, "SELECT path, access_tier FROM graph_nodes WHERE kind = 'file'")
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


def test_a_failure_in_a_batch_rolls_back_its_rows_and_its_cursor(stored, writer, monkeypatch):  # noqa: F811
    """A batch whose rows commit without its cursor, so the next run derives them again over a moved state."""
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
    """Keys that outlive store mode, so a later migration reads a pass that belongs to another vault life."""
    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert rederive.KEYS[1] in dict(rows(stored, "SELECT key, value FROM store_meta"))
    assert rollback_apply(stored, tmp_path / "export") == 0
    kept = dict(rows(stored, "SELECT key, value FROM store_meta"))
    assert not set(rederive.KEYS) & set(kept)
