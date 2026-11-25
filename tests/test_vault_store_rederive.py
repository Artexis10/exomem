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
from test_vault_store_write_path import commits_since

from exomem import commands, epistemic_graph, lexstore, note_types, semantic_index, writer_lease
from exomem import find as find_module
from exomem.cli_ops import OpError
from exomem.collection_store import connection, preview, runtime, schema
from exomem.collection_store.writer import CollectionWriter
from exomem.governance import connector_boundary
from exomem.vault_store import derive, migrate, mode, projection, read, rederive, verify, write
from exomem.vocabulary import instances

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
        elif operation == "delete":
            (root / rel).unlink(missing_ok=True)
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


@pytest.mark.parametrize("interrupted", [False, True])
def test_legacy_evidence_hashes_refresh_after_a_store_upgrade(vault, monkeypatch, interrupted):  # noqa: F811
    """An old store must retire NULL hashes, report warming, then reuse unchanged originals."""
    from duplicate_fixtures import legacy_evidence_filing
    from test_vault_store_migration import build_indexes

    from exomem import duplicates, freshness, lexstore

    original = b"original retained by an earlier store"
    rel = legacy_evidence_filing(vault, original)
    before = (vault / rel).read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    derive_payload = duplicates.derive_page_payload
    with monkeypatch.context() as earlier:
        earlier.setattr(rederive, "OWNER_SCHEMA_VERSION", 2)
        earlier.setattr(duplicates, "derive_page_payload", lambda path, raw: None if path == rel else derive_payload(path, raw))
        freshness.clear()
        lexstore.clear_stores()
        build_indexes(vault)
        assert migrate_apply(vault) == 0
        assert rows(vault, "SELECT payload_hash FROM pages WHERE path = ?", rel) == [(None,)]
        if interrupted:
            # An old pass already derived this page without a payload hash; its cursor has no version.
            with store_writer(vault) as bound, bound.handle.transaction() as tx:
                tx.execute("DELETE FROM store_meta WHERE key = ?", (rederive.REGISTRY_REVISION,))
                tx.execute(
                    "INSERT INTO store_meta(key, value) VALUES (?, ?)",
                    (rederive.PASS, json.dumps({"target": revision(vault), "after": rel})),
                )
    lexstore.clear_stores()

    assert mode.status(vault)["registry"] == mode.UNKNOWN
    assert lexstore.payload_candidates(vault, digest) is None
    with store_writer(vault) as bound:
        assert rederive.pending(bound.connection, vault) == rederive.READY
        settle_to_the_end(vault, bound)

    assert mode.status(vault)["registry"] == rederive.CURRENT
    hit = duplicates.find_duplicate(vault, sha256=digest, lane=duplicates.EVIDENCE, protected=False).hit
    assert hit is not None and hit.path == rel
    assert (vault / rel).read_bytes() == before
    assert (vault / hit.payload.artifact_path).read_bytes() == original
    assert not lexstore.full_rebuild_in_flight(vault)


def test_a_store_migrated_under_an_older_lexical_schema_answers_the_duplicate_index_once_the_pass_settles(
    vault, monkeypatch  # noqa: F811
):
    """After a lexical schema bump the owner pass must stamp the store, or its index answers None for good."""
    from duplicate_fixtures import legacy_evidence_filing
    from test_vault_store_migration import build_indexes

    from exomem import duplicates, freshness, lexstore

    original = b"original kept by a store migrated before the schema bump"
    rel = legacy_evidence_filing(vault, original)
    digest = hashlib.sha256(original).hexdigest()
    with monkeypatch.context() as earlier:
        # The build that migrated the vault: one lexical schema and one owner generation older.
        earlier.setattr(lexstore, "SCHEMA_VERSION", lexstore.SCHEMA_VERSION - 1)
        earlier.setattr(rederive, "OWNER_SCHEMA_VERSION", rederive.OWNER_SCHEMA_VERSION - 1)
        freshness.clear()
        lexstore.clear_stores()
        build_indexes(vault)
        assert migrate_apply(vault) == 0
    lexstore.clear_stores()

    assert mode.status(vault)["owner_schema"] == "behind"
    assert lexstore.payload_candidates(vault, digest) is None
    with store_writer(vault) as bound:
        settle_to_the_end(vault, bound)

    assert mode.status(vault)["owner_schema"] == "current"
    assert lexstore.payload_candidates(vault, digest) == (rel,)
    lookup = duplicates.find_duplicate(vault, sha256=digest, lane=duplicates.EVIDENCE, protected=False)
    assert lookup.available and lookup.hit is not None and lookup.hit.path == rel


@pytest.mark.parametrize(
    ("old_stamp", "schema_16_shape"),
    [(True, False), (True, True), (False, True)],
    ids=["stamp-only", "without-schema-17-columns", "current-stamp-over-schema-16-shape"],
)
def test_a_store_with_a_current_owner_version_and_an_old_lexical_catalogue_answers_once_the_pass_settles(
    vault, old_stamp, schema_16_shape  # noqa: F811
):
    """A store that a lexical-16 build migrated and a later build only settled keeps its stamp or its page
    shape, so its lexical readers answer None for good unless either one makes the owner state behind."""
    from duplicate_fixtures import legacy_evidence_filing
    from test_vault_store_migration import build_indexes

    from exomem import duplicates, freshness, lexstore

    original = b"original kept by a store that a lexical-16 build migrated"
    rel = legacy_evidence_filing(vault, original)
    digest = hashlib.sha256(original).hexdigest()
    freshness.clear()
    lexstore.clear_stores()
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    with store_writer(vault) as bound, bound.handle.transaction() as tx:
        if old_stamp:
            tx.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(lexstore.SCHEMA_VERSION - 1),)
            )
        if schema_16_shape:
            # Schema 17 added these two columns; their indexes go first, as SQLite refuses to drop an indexed column.
            for index in ("pages_payload_hash", "pages_source_url"):
                tx.execute(f"DROP INDEX {index}")
            for column in ("payload_hash", "source_url"):
                tx.execute(f"ALTER TABLE pages DROP COLUMN {column}")
    lexstore.clear_stores()

    assert mode.status(vault)["owner_schema"] == "behind"
    assert lexstore.payload_candidates(vault, digest) is None
    with store_writer(vault) as bound:
        settle_to_the_end(vault, bound)

    assert mode.status(vault)["owner_schema"] == "current"
    assert lexstore.payload_candidates(vault, digest) == (rel,)
    lookup = duplicates.find_duplicate(vault, sha256=digest, lane=duplicates.EVIDENCE, protected=False)
    assert lookup.available and lookup.hit is not None and lookup.hit.path == rel


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


def test_a_registry_overlay_that_a_binding_puts_in_the_review_folder_is_an_overlay_change(
    stored, writer  # noqa: F811
):
    """A note-type overlay that the vault's binding places among the relation review records: its
    loader reads it, but its save left the rows of the old registry current, like a review."""
    reviews = "Knowledge Base/_Schema/relation-reviews"
    overlay = f"{reviews}/active-note-types.yaml"
    public = {
        "namespace": "Knowledge Base/_Schema/public",
        "history": "public",
        "overrides": {note_types.SPEC.stem: {"overlay": overlay}},
    }
    vocabulary = {"public": public, "private": {}, "destinations": {}, "selections": {}}
    # An armed boundary protects at least one scope, so the vault gets one before the binding.
    scope = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    scopes = stored / "Knowledge Base/_Governance/scopes"
    scopes.mkdir(parents=True, exist_ok=True)
    (scopes / "capture.yaml").write_text(
        f'governance_version: 1\nid: {scope}\npaths: ["Capture/**"]\n', encoding="utf-8"
    )
    connector_boundary.publish_requirement(
        stored,
        connector_boundary.Snapshot(
            revision="",
            default_denied_scope_ids=frozenset({scope}),
            clients=(),
            capture_paths=("Knowledge Base/Capture",),
            vocabulary=vocabulary,
        ),
    )
    assert instances.match_overlay(stored, note_types.SPEC, overlay) is not None
    review = write.PagePut(f"{reviews}/0a1b2c3d.json", b'{"decision": "reviewed_none"}\n')
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "review", puts=(review,)))
    assert mode.status(stored)["registry"] == rederive.CURRENT, "a review record is no input"

    note_type = b"types:\n  meeting-note:\n    role: compiled\n    folder: Notes/Meetings\n"
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED, "note types", puts=(write.PagePut(overlay, note_type),)
        ),
    )
    assert mode.status(stored)["registry"] == rederive.REBUILDING
    assert rederive.settle(stored, writer) == rederive.WAITING


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
    assert commits_since(stored, during) == 1, "the write committed while the pass was part way"
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


@pytest.mark.parametrize("legacy_cursor", [False, True], ids=["older-generation", "legacy-cursor"])
def test_an_owner_upgrade_restarts_an_older_pass_and_resumes_its_own_generation(
    stored, tmp_path, monkeypatch, legacy_cursor,  # noqa: F811
):
    """An old cursor must not skip pre-cursor pages in a newly empty projection or expose partial exclusions."""
    target_generation = rederive.OWNER_SCHEMA_VERSION
    # Version 2 shipped page admission without unit/title projections; deriving this from the target hid that collision.
    old_generation = 2
    assert target_generation > old_generation
    positive = {"unit.dimension": {"$eq": "attribution"}}
    dependent = [
        positive,
        {"$not": positive},
        {"unit.dimension": {"$ne": "attribution"}},
        {"$or": [positive, {"unit.kind": {"$eq": "decision"}}]},
    ]

    def search(filters, *, query="", result_level="unit", limit=100):
        return commands.op_ask_memory(
            stored, query=query, mode="keyword", scope="kb-only", result_level=result_level,
            filters=filters, graph=False, limit=limit, detail="full",
        )["hits"]

    with store_writer(stored) as upgrading:
        write.commit(upgrading, write.StoreChange(
            write.Origin.GOVERNED, "notes", puts=(
                write.PagePut(ALPHA, b"# Alpha\n\n## Claim\n- asserted_by: Survey team\n\nThe beacon is lit.\n"),
                write.PagePut(BETA, b"# Beta\n\n## Claim\n- asserted_by: Survey team\n\nThe tide is rising.\n"),
                write.PagePut(GAMMA, ("# Gamma\n\n" + "\n".join(
                    f"## Claim\n- id: noise-{n}\n\nThe signal is steady {n}. See [[Alpha]].\n" for n in range(24)
                )).encode()),
            ),
        ))
        project(upgrading, stored)
        expected = [{hit["unit_ref"] for hit in search(expression)} for expression in dependent]
        assert len(expected[0]) == 2
        inbound_before = commands.op_list_inbound_links(stored, target=ALPHA)["count"]
        assert inbound_before > 0
        with upgrading.handle.transaction() as tx:
            tx.execute("UPDATE store_meta SET value = ? WHERE key = ?",
                       (str(old_generation), rederive.OWNER_SCHEMA))
            tx.execute("DELETE FROM store_meta WHERE key = ?", (rederive.REGISTRY_REVISION,))
            prefix = tx.execute("SELECT COUNT(*) FROM store_pages WHERE path <= ?", (ALPHA,)).fetchone()[0]
        monkeypatch.setattr(rederive, "BATCH_PAGES", prefix)
        with monkeypatch.context() as older:
            older.setattr(rederive, "OWNER_SCHEMA_VERSION", old_generation)
            assert rederive.settle(stored, upgrading) == rederive.ADVANCED
        with upgrading.handle.transaction() as tx:
            cursor = json.loads(tx.execute("SELECT value FROM store_meta WHERE key = ?", (rederive.PASS,)).fetchone()[0])
            assert cursor["after"] == ALPHA
            if legacy_cursor:
                cursor.pop("owner_schema", None)
            else:
                cursor["owner_schema"] = old_generation
            tx.execute("UPDATE store_meta SET value = ? WHERE key = ?", (json.dumps(cursor), rederive.PASS))
            tx.execute("DROP TABLE unit_row_keys")
            tx.execute("DROP TABLE title_fts")
            tx.execute("DROP TABLE wikilink_keys")
            tx.execute("ALTER TABLE graph_dependencies DROP COLUMN body_link")
        legacy_write = "Knowledge Base/Notes/Upgrade write.md"
        write.commit(upgrading, write.StoreChange(
            write.Origin.GOVERNED, "note", puts=(write.PagePut(
                legacy_write, b"# Upgrade write\n\n## Claim\n\nA write during background upgrade.\n",
            ),),
        ))
        write.commit(upgrading, write.StoreChange(
            write.Origin.GOVERNED, "note", deletes=(write.PageDelete(legacy_write),),
        ))
        project(upgrading, stored)
        find_module.clear_cache()  # The new build has no hot results from the old process.

        assert rederive.settle(stored, upgrading) == rederive.ADVANCED
        assert rows(stored, "SELECT DISTINCT parent_path FROM unit_row_keys") == [(ALPHA,)]
        assert rederive.owner_schema_behind(upgrading.connection)
        assert commands.op_list_inbound_links(stored, target=ALPHA)["count"] == inbound_before
        for expression in dependent:
            with pytest.raises(find_module.RetrievalIndexWarming):
                search(expression)
        for query in ("", "beacon"):
            with pytest.raises(find_module.RetrievalIndexWarming):
                search(positive, query=query, result_level="page")
        assert search(None)
        assert search({"unit.dimension": {"$eq": "kind"}})
        with pytest.raises(find_module.RetrievalIndexWarming):
            search({"$or": [
                {"unit.dimension": {"$eq": "currency"}}, {"unit.kind": {"$eq": "claim"}},
            ]})
        resumed_from = rows(stored, "SELECT value FROM store_meta WHERE key = ?", rederive.PASS)
        first_revision = rows(stored, "SELECT revision FROM unit_revisions WHERE parent_path = ?", ALPHA)
        original_collection_schema = schema.schema_version(upgrading.connection)

    # Reopening the writer represents an interruption after the batch and its cursor committed.
    with store_writer(stored) as restarted:
        assert rederive.settle(stored, restarted) == rederive.ADVANCED
        resumed = rows(stored, "SELECT value FROM store_meta WHERE key = ?", rederive.PASS)
        assert json.loads(resumed[0][0])["after"] > json.loads(resumed_from[0][0])["after"]
        assert rows(stored, "SELECT revision FROM unit_revisions WHERE parent_path = ?", ALPHA) == first_revision
        monkeypatch.setattr(rederive, "BATCH_PAGES", 32)
        settle_to_the_end(stored, restarted, limit=200)
        assert not rederive.owner_schema_behind(restarted.connection)
        assert schema.schema_version(restarted.connection, ceiling=original_collection_schema) == original_collection_schema
        for expression, wanted in zip(dependent, expected, strict=True):
            assert {hit["unit_ref"] for hit in search(expression)} == wanted
        assert len(search(positive, limit=2)) == len(search({"$not": positive}, limit=2)) == 2
    assert_cold_derivation(stored, tmp_path)


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


@pytest.mark.parametrize("legacy_cursor", [False, True], ids=["older-parser", "legacy-cursor"])
def test_parser_upgrade_rederives_stored_units_before_reporting_current(
    stored, tmp_path, monkeypatch, legacy_cursor,  # noqa: F811
):
    """A future parser upgrade must not turn valid stored units or a cached miss into an exact empty answer."""
    previous = semantic_index.PARSER_VERSION
    future = previous + 1
    before_cursor = "Knowledge Base/Notes/Aardvark.md"
    after_cursor = "Knowledge Base/Notes/Zulu.md"
    text = b"# Alpha\n\n## Claim\n- id: first\n\nThe beacon is lit.\n\n## Claim\n\nThe tide is rising.\n"

    def ask(query=""):
        return commands.op_ask_memory(
            stored, query=query, mode="keyword", scope="kb-only", result_level="unit",
            filters={"unit.kind": {"$eq": "claim"}}, limit=2, graph=False,
        )

    initial = mode.status(stored)
    with store_writer(stored) as upgrading:
        assert rederive.pending(upgrading.connection, stored) == rederive.IDLE
        write.commit(upgrading, write.StoreChange(write.Origin.GOVERNED, "notes", puts=(
            write.PagePut(ALPHA, text), write.PagePut(BETA, b"# Beta\n\nSee [[Alpha#first]].\n"),
        )))
        project(upgrading, stored)
        assert len(ask()["hits"]) == 2
        inbound = commands.op_list_inbound_links(stored, target=ALPHA)["count"]
        assert inbound > 0
        with upgrading.handle.transaction() as tx:
            tx.execute("DELETE FROM store_meta WHERE key = ?", (rederive.REGISTRY_REVISION,))
            prefix = tx.execute("SELECT COUNT(*) FROM store_pages WHERE path <= ?", (ALPHA,)).fetchone()[0]
        monkeypatch.setattr(rederive, "BATCH_PAGES", prefix)
        assert rederive.settle(stored, upgrading) == rederive.ADVANCED
        with upgrading.handle.transaction() as tx:
            cursor = json.loads(tx.execute("SELECT value FROM store_meta WHERE key = ?", (rederive.PASS,)).fetchone()[0])
            if legacy_cursor:
                cursor.pop("parser_version", None)
            else:
                cursor["parser_version"] = previous
            tx.execute("UPDATE store_meta SET value = ? WHERE key = ?", (json.dumps(cursor), rederive.PASS))
        assert ask("no matching words")["hits"] == []  # Cache a legitimate miss before the upgrade.
        monkeypatch.setattr(semantic_index, "PARSER_VERSION", future)

        with pytest.raises(find_module.RetrievalIndexWarming):
            ask("no matching words")
        with pytest.raises(find_module.RetrievalIndexWarming):
            ask()
        assert initial["parser_version"] == initial["required_parser_version"] == previous
        assert rederive.pending(upgrading.connection, stored) == rederive.READY
        assert read.owned_bytes(stored, ALPHA) == text
        assert "Alpha.md" in read.listing(stored, "Knowledge Base/Notes", True).pages
        assert commands.op_list_inbound_links(stored, target=ALPHA)["count"] == inbound
        assert lexstore.get_store(stored).heading_unit_counts(["claim"]) is None
        with read.snapshot(stored) as snapshot:
            view = epistemic_graph.GraphView(stored, snapshot.connection)
            with pytest.raises(OpError) as refused:
                view.indexed_units("missing-parent#missing-unit")
            assert getattr(refused.value, "code", None) == read.DERIVED_BEHIND
        objects = rows(stored, "SELECT name, sql FROM sqlite_master ORDER BY name")
        assert rederive.settle(stored, upgrading) == rederive.ADVANCED
        assert rows(stored, "SELECT name, sql FROM sqlite_master ORDER BY name") == objects
        status = mode.status(stored)
        assert status["owner_schema"] == "current"
        assert status["parser_version"] == previous
        assert status["required_parser_version"] == future
        with pytest.raises(find_module.RetrievalIndexWarming):
            ask()
        first = json.loads(rows(stored, "SELECT value FROM store_meta WHERE key = ?", rederive.PASS)[0][0])
        assert first["after"] == ALPHA
        assert first["parser_version"] == future
        write.commit(upgrading, write.StoreChange(write.Origin.GOVERNED, "notes", puts=(
            write.PagePut(ALPHA, text.replace(b"lit", b"bright")),
            write.PagePut(before_cursor, b"# Aardvark\n\n## Claim\n\nAn earlier note.\n"),
            write.PagePut(after_cursor, b"# Zulu\n\n## Claim\n\nA later note.\n"),
        ), deletes=(write.PageDelete(BETA),)))
        project(upgrading, stored)
        commit_policy(upgrading, stored, READ_ONLY_NOTES)
        project(upgrading, stored)
        first_revision = rows(stored, "SELECT revision FROM unit_revisions WHERE parent_path = ?", ALPHA)

    with store_writer(stored) as restarted:
        assert rederive.settle(stored, restarted) == rederive.ADVANCED
        resumed = json.loads(rows(stored, "SELECT value FROM store_meta WHERE key = ?", rederive.PASS)[0][0])
        assert resumed["after"] > first["after"]
        assert resumed["target"] == first["target"]
        assert rows(stored, "SELECT revision FROM unit_revisions WHERE parent_path = ?", ALPHA) == first_revision
        monkeypatch.setattr(rederive, "BATCH_PAGES", 32)
        settle_to_the_end(stored, restarted, limit=200)
        assert mode.status(stored)["parser_version"] == future
        assert mode.status(stored)["registry"] == rederive.REBUILDING
        assert rederive.pending(restarted.connection, stored) == rederive.READY
        assert ask()["hits"], "ordinary overlay lag stays a notice after parser qualification"
        settle_to_the_end(stored, restarted, limit=200)
    assert mode.status(stored)["registry"] == rederive.CURRENT
    assert_cold_derivation(stored, tmp_path)


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
            migrate.create_owner_schema(tx)
            for key in (rederive.OVERLAY_REVISION, rederive.REGISTRY_REVISION):
                tx.execute("INSERT INTO store_meta(key, value) VALUES (?, '5')", (key,))
            # As this build's migration records it: the store holds this build's owner objects.
            tx.execute(
                "INSERT INTO store_meta(key, value) VALUES (?, ?)",
                (rederive.OWNER_SCHEMA, str(rederive.OWNER_SCHEMA_VERSION)),
            )
            tx.execute(
                "INSERT INTO store_meta(key, value) VALUES (?, ?)",
                (rederive.PARSER_VERSION, str(semantic_index.PARSER_VERSION)),
            )
            lexstore.create_catalogue_schema(tx)
            lexstore.stamp_catalogue_schema(tx)
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


@pytest.mark.parametrize("parser_stamp", [None, "damaged"], ids=["missing", "malformed"])
def test_an_unqualified_parser_stamp_owes_a_full_pass_even_when_no_pages_exist(served_store, parser_stamp):
    """An empty older store that silently declares its semantic projection current."""
    root = served_store.root
    with store_writer(root) as bound:
        with bound.handle.transaction() as tx:
            tx.execute("DELETE FROM store_meta WHERE key = ?", (rederive.PARSER_VERSION,))
            if parser_stamp is not None:
                tx.execute("INSERT INTO store_meta(key, value) VALUES (?, ?)",
                           (rederive.PARSER_VERSION, parser_stamp))
        assert rederive.pending(bound.connection, root) == rederive.READY
        assert rederive.read(bound.connection).parser_version is None
        assert rederive.settle(root, bound) == rederive.DONE
        assert rederive.pending(bound.connection, root) == rederive.IDLE
        assert rederive.read(bound.connection).parser_version == semantic_index.PARSER_VERSION


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
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        before = rederive.read(reader)
    assert before.state != rederive.CURRENT

    assert runtime.StoreServer._registry_tick(served_store) == runtime.DERIVED_RETRY_SECONDS
    # A batch takes no vault boundary, so the registry state, not the commit generation, says one ran.
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert rederive.read(reader) == before
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        with handle.transaction() as tx:
            tx.execute("DELETE FROM store_projection_queue")

    runtime.StoreServer._registry_tick(served_store)

    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert rederive.read(reader).state == rederive.CURRENT
