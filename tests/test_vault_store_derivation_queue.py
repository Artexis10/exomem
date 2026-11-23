"""A store-mode write holds the vault mutation boundary only for its canonical rows.

OpenSpec make-vault-store-canonical, the derivation queue. The commit stores a page and one
derivation intent for it; the store thread derives the search, identity and graph rows from the
intent after the writing request releases the boundary. The vault is the migration suite's
generated vault in store mode, with the store thread's writer bound.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from itertools import zip_longest

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401
from test_vault_store_write_path import bm25_paths, planned, served, store_rows  # noqa: F401

from exomem import delete_file, lexstore, media_capacity, memory_refs, mutation_lock, writer_lease
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.vault_store import derivation, derive, projection, read, verify, write

SMALL = "Knowledge Base/Notes/Small.md"
LARGE = "Knowledge Base/Notes/Large.md"
SMALL_TEXT = "---\ntitle: Small\ntype: note\n---\n# Small\n\nThe ferry leaves at dawn.\n"
#: 400 units, each with its own link: the page whose rows cost 400 times a small page's.
LARGE_TEXT = "---\ntitle: Large\ntype: note\n---\n# Large\n\n" + "".join(
    f"- [finding] The ferry calls at [[Harbour {number}]] on day {number}.\n"
    for number in range(400)
)
#: A derivation this slow, under the boundary, would show in any hold.
SLOW_DERIVATION_SECONDS = 2.0
#: A hold that grows by less than half the slow derivation did not derive under the boundary.
HOLD_GROWTH_LIMIT_MS = SLOW_DERIVATION_SECONDS * 1000 / 2


@pytest.fixture(autouse=True)
def _reset_managers():
    yield
    writer_lease.reset_managers_for_tests()


def save(root, rel, text):
    """A governed save as the store thread runs it: under the vault mutation boundary."""
    with writer_lease.get_manager().mutation_guard(root, operation="note_save"):
        vault_module.batch_atomic_write(
            [planned(root, rel, text, create_only=True)], vault_root=root, post_commit_fanout=False
        )


def holds(monkeypatch):
    """Each vault boundary hold in milliseconds, as the boundary itself times it."""
    held = []
    real = mutation_lock._record_timing

    def record(coordinator, timing):  # noqa: ANN001, ANN202 - the boundary's own private hook
        if coordinator.kind == mutation_lock.COORDINATOR_VAULT_BOUNDARY:
            held.append(timing["hold_ms"])
        real(coordinator, timing)

    monkeypatch.setattr(mutation_lock, "_record_timing", record)
    return held


def assert_rows_equal_a_cold_derivation(root, tmp_path):
    """The store's derived tables equal a derivation from the pages' files, made from scratch."""
    # The projector's step, run by hand: each stored page becomes its file, with its stored mtime.
    with read.snapshot(root) as view:
        for meta in view.pages():
            target = projection.write_file(root, meta.path, view.page(meta.path).body, durable=False)
            os.utime(target, ns=(meta.mtime_ns, meta.mtime_ns))
    expected = tmp_path / "expected.sqlite"
    verify.derive_expected(root, expected)
    with closing(sqlite3.connect(connection.store_path(root))) as store:
        comparison = verify.compare_tables(store, expected)
    assert comparison.tables, "the comparison must cover tables for this check to mean anything"
    assert comparison.differences == ()


def test_a_governed_write_holds_the_boundary_no_longer_when_its_derivation_is_slow(
    served, monkeypatch  # noqa: F811
):
    """A save whose search and graph rows take seconds to derive: the boundary is free in the
    meantime, and the save still returns with its rows derived."""
    held = holds(monkeypatch)
    delay = [0.0]
    real = lexstore.write_page_rows

    def write_page_rows(*arguments, **keywords):
        time.sleep(delay[0])
        return real(*arguments, **keywords)

    monkeypatch.setattr(lexstore, "write_page_rows", write_page_rows)
    save(served, SMALL, SMALL_TEXT)
    quick = held[-1]
    delay[0] = SLOW_DERIVATION_SECONDS
    save(served, "Knowledge Base/Notes/Slow.md", SMALL_TEXT.replace("Small", "Slow"))
    slow = held[-1]

    assert slow - quick < HOLD_GROWTH_LIMIT_MS, (
        f"the save held the boundary {slow:.0f} ms with a {SLOW_DERIVATION_SECONDS:.0f} s "
        f"derivation, against {quick:.0f} ms with an instant one"
    )
    assert "Knowledge Base/Notes/Slow.md" in bm25_paths(served, "Slow")


def test_a_large_page_runs_as_many_statements_under_the_boundary_as_a_small_one(
    served, writer, monkeypatch  # noqa: F811
):
    """The statements that a save runs while it holds the boundary, for a page of one unit and a
    page of 400 units and 400 links: the canonical rows cost the same for both."""
    coordinator = writer_lease.get_manager()._mutation_coordinator_for(served)
    statements = []

    def trace(statement):
        if coordinator.current_thread_holds_boundary():
            statements.append(statement)

    # The first save after a start also checks the writer's cached schema; this one pays for it.
    save(served, "Knowledge Base/Notes/Warm.md", SMALL_TEXT.replace("Small", "Warm"))
    writer.handle.connection.set_trace_callback(trace)
    try:
        save(served, SMALL, SMALL_TEXT)
        small = list(statements)
        statements.clear()
        save(served, LARGE, LARGE_TEXT)
        large = list(statements)
    finally:
        writer.handle.connection.set_trace_callback(None)
    print(f"statements under the boundary: {len(small)} for one unit, {len(large)} for 400 units and links")

    assert len(large) == len(small), "\n".join(
        f"{one[:70]!r:75} {other[:70]!r}" for one, other in zip_longest(small, large, fillvalue="")
    )
    assert LARGE in bm25_paths(served, "ferry")


def test_a_commit_whose_drain_never_ran_is_derived_at_the_next_start(
    served, writer, monkeypatch, tmp_path  # noqa: F811
):
    """A process that stopped after the commit and before its drain: the intents wait in the
    store, and the next start derives the rows that a cold derivation gives."""
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        save(served, SMALL, SMALL_TEXT.replace("dawn", "[[Alpha]] at dawn"))
    assert store_rows(served, "SELECT path FROM store_derivation_queue") == [(SMALL,)]
    assert bm25_paths(served, "ferry") == []

    derivation.started(served.resolve(), writer)
    derivation.drain(served.resolve(), writer)

    assert store_rows(served, "SELECT path FROM store_derivation_queue") == []
    assert bm25_paths(served, "ferry") == [SMALL]
    assert_rows_equal_a_cold_derivation(served, tmp_path)


class Killed(BaseException):
    """The process stopping mid-transaction: no handler of ordinary errors catches it."""


def test_a_drain_stopped_inside_its_transaction_leaves_its_intents_for_the_next_start(
    served, writer, monkeypatch, tmp_path  # noqa: F811
):
    """A process that stopped while it derived a batch of two pages: the batch rolls back whole,
    and the next start derives the rows that a cold derivation gives."""
    real = lexstore.write_page_rows
    calls = []

    def stop_at_the_second_page(*arguments, **keywords):
        calls.append(1)
        if len(calls) == 2:
            raise Killed
        return real(*arguments, **keywords)

    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        with writer_lease.get_manager().mutation_guard(served, operation="note_save"):
            vault_module.batch_atomic_write(
                [
                    planned(served, SMALL, SMALL_TEXT, create_only=True),
                    planned(served, LARGE, SMALL_TEXT.replace("Small", "Large"), create_only=True),
                ],
                vault_root=served,
                post_commit_fanout=False,
            )
    with monkeypatch.context() as patch:
        patch.setattr(lexstore, "write_page_rows", stop_at_the_second_page)
        with pytest.raises(Killed):
            derivation.drain(served.resolve(), writer)
    assert sorted(store_rows(served, "SELECT path FROM store_derivation_queue")) == [
        (LARGE,),
        (SMALL,),
    ]
    assert bm25_paths(served, "ferry") == []

    derivation.started(served.resolve(), writer)
    derivation.drain(served.resolve(), writer)

    assert sorted(bm25_paths(served, "ferry")) == sorted([SMALL, LARGE])
    assert_rows_equal_a_cold_derivation(served, tmp_path)


def test_a_write_can_cite_the_source_that_the_write_before_it_created(served, source_schema):  # noqa: F811
    """Two saves back to back, the second citing the first's Source by its reference: the citation
    resolves through identity rows, which the first save derived before it returned."""
    from exomem import add, commands

    with writer_lease.get_manager().mutation_guard(served):
        source = add.add(
            served,
            source_schema,
            title="Ferry timetable",
            content="The ferry leaves at dawn and returns at dusk.\n",
            source_type="session",
        )
    cited = commands.op_remember(
        served,
        title="Ferry finding",
        status="draft",
        content="## Observations\n\n- [finding] The ferry returns at dusk.\n",
        summary="A finding that cites the timetable.",
        sources=[source.ref],
    )

    assert cited["path"] in bm25_paths(served, "dusk")
    assert store_rows(served, "SELECT path FROM store_derivation_queue") == []


def test_a_reader_whose_snapshot_holds_underived_pages_is_told_so(served, writer, monkeypatch):  # noqa: F811
    """A search that runs while the store thread still derives a save's rows: it serves what the
    rows hold and says that recent pages lag, with counts and no path."""
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        save(served, SMALL, SMALL_TEXT)

    behind = read.request_registry_notice(served)
    assert behind is not None and "not yet in the search" in behind["notice"]
    assert (behind["lag"]["derived_pending"], behind["lag"]["derived_failed"]) == (1, 0)
    assert SMALL not in str(behind)

    derivation.drain(served.resolve(), writer)
    assert read.request_registry_notice(served) is None


HARBOUR = "Knowledge Base/Notes/Harbour.md"
LINKER = "Knowledge Base/Notes/Linker.md"


def fail_derivations(served, writer, monkeypatch, error, attempts):  # noqa: F811
    """Run the drain ``attempts`` times while every derivation raises ``error``."""

    def refuse(*_arguments, **_keywords):
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(derive, "derive_paths", refuse)
        for _ in range(attempts):
            derivation.started(served.resolve(), writer)
            derivation.drain(served.resolve(), writer)


@pytest.mark.parametrize(
    ("error", "attempts", "code"),
    [
        (RuntimeError("the derivation broke"), derivation.ATTEMPTS, derivation.FAILED),
        (OpError(media_capacity.UPLOAD_SPACE_BUSY, "no room"), 1, derivation.NO_ROOM),
    ],
    ids=["failed", "no_room"],
)
def test_a_delete_counts_the_link_of_a_page_whose_derivation_did_not_land(
    served, writer, monkeypatch, error, attempts, code  # noqa: F811
):
    """A page that links to the target and whose rows failed, or wait for room: its link has no row,
    so a check that trusts the rows alone would trash the target and orphan the link."""
    save(served, HARBOUR, SMALL_TEXT.replace("Small", "Harbour"))
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        save(served, LINKER, SMALL_TEXT.replace("dawn", "dawn from [[Harbour]]"))
    fail_derivations(served, writer, monkeypatch, error, attempts)
    assert store_rows(served, "SELECT path, failed_code FROM store_derivation_queue") == [(LINKER, code)]

    with pytest.raises(delete_file.DeleteFileError) as refused:
        delete_file.delete_file(served, path=HARBOUR, confirm=True)

    assert refused.value.code == "INBOUND_LINKS"
    assert f"{LINKER}:" in refused.value.reason
    assert read.page_exists(served, HARBOUR)


def test_a_reference_names_where_a_moved_page_is_when_its_rows_failed_to_follow(
    served, writer, monkeypatch  # noqa: F811
):
    """A page moved by a commit whose derivation failed: its identity row still names the old path,
    so a citation that trusts it would resolve to a path where the store holds no page."""
    identifier = "6f1c8e9a-2b4d-4c3e-9a7f-1d2e3f4a5b6c"
    before, after = "Knowledge Base/Notes/Tide table.md", "Knowledge Base/Notes/Archive/Tide table.md"
    body = f"---\ntitle: Tide table\nexomem_id: {identifier}\n---\n# Tide table\n".encode()
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "file note", puts=(write.PagePut(before, body),)))
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "move note",
                                               moves=(write.PageMove(before, after),)))
    fail_derivations(served, writer, monkeypatch, RuntimeError("the derivation broke"), derivation.ATTEMPTS)
    assert store_rows(served, "SELECT path FROM identities WHERE exomem_id = ?", identifier) == [(before,)]

    assert memory_refs.resolve_identifier_read_only(served, memory_refs.memory_ref(identifier)) == after
