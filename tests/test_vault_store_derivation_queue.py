"""A store-mode write holds the vault mutation boundary only for its canonical rows.

OpenSpec make-vault-store-canonical, the derivation queue. The commit stores a page and one
derivation intent for it; the store thread derives the search, identity and graph rows from the
intent after the writing request releases the boundary. The vault is the migration suite's
generated vault in store mode, with the store thread's writer bound.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import timedelta
from itertools import zip_longest

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401
from test_vault_store_write_path import bm25_paths, planned, served, store_rows  # noqa: F401

from exomem import (
    delete_directory,
    delete_file,
    lexstore,
    media_capacity,
    memory_refs,
    mutation_lock,
    writer_lease,
)
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.collection_store import connection, runtime
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


def test_a_hub_edit_resumes_both_fragment_projections_after_an_interrupted_batch(
    served, writer, monkeypatch, tmp_path,  # noqa: F811
):
    hub = "Knowledge Base/Notes/Signal Hub.md"
    hub_text = "---\ntitle: Signal Hub\ntype: note\n---\n# Signal Hub\n\n- [rule] A signal. ^old\n"
    for index in range(5):
        path = f"Knowledge Base/Notes/Signal Reader {index}.md"
        save(served, path, f"# Signal Reader {index}\n\n[[Signal Hub#^old]]\n\n"
             "## Claim\n- relations: supports: [[Signal Hub#^old]]\n\nThe light is visible.\n")
    save(served, hub, hub_text)
    derivation.drain(served, writer)
    landed = """
        SELECT COUNT(*) FROM graph_unit_links
        WHERE target_path = ? AND resolution = 'unit'
    """
    typed = """
        SELECT COUNT(*) FROM graph_edges
        WHERE dst_page_key = ? AND json_extract(metadata, '$.fragment_resolution') = 'unit'
    """
    assert store_rows(served, landed, hub) == [(10,)]
    assert store_rows(served, typed, f"file:{hub}") == [(5,)]

    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_args, **_kwargs: None)
        with writer_lease.get_manager().mutation_guard(served):
            vault_module.batch_atomic_write(
                [planned(served, hub, hub_text.replace("^old", "^new"), create_only=False)],
                vault_root=served, post_commit_fanout=False,
            )
    real = derive.resume_fragments

    def interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise Killed

    monkeypatch.setattr(derivation, "BATCH_PAGES", 2)
    with monkeypatch.context() as patch:
        patch.setattr(derive, "resume_fragments", interrupted)
        with pytest.raises(Killed):
            derivation.drain(served, writer)

    assert store_rows(served, "SELECT path FROM store_derivation_queue") == [(hub,)]
    assert store_rows(served, "SELECT path FROM graph_fragment_pass") == [(hub,)]
    assert store_rows(served, landed, hub) == [(6,)]
    assert store_rows(served, typed, f"file:{hub}") == [(3,)]
    derivation.started(served, writer)
    derivation.drain(served, writer)
    assert store_rows(served, "SELECT path FROM graph_fragment_pass") == []
    assert store_rows(served, "SELECT path FROM store_derivation_queue") == []
    assert store_rows(served, landed, hub) == [(0,)]
    assert store_rows(served, typed, f"file:{hub}") == [(0,)]
    assert_rows_equal_a_cold_derivation(served, tmp_path)


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


def commit_pending_pages(served, writer, monkeypatch, count, name="Backlog", *, projected=False):  # noqa: F811
    """Commit ``count`` pages whose derivation does not run: a backlog, as a bulk save leaves one.

    ``projected`` also writes each page's file, as the projector does after a commit."""
    puts = tuple(
        write.PagePut(
            f"Knowledge Base/Notes/{name} {number}.md",
            f"---\ntitle: {name} {number}\ntype: note\n---\n# {name} {number}\n\nThe ferry waits.\n".encode(),
        )
        for number in range(count)
    )
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "bulk save", puts=puts))
    if projected:
        for put in puts:
            (served / put.rel).write_bytes(put.body)


def drain_all(served, writer):  # noqa: F811
    while store_rows(served, "SELECT 1 FROM store_derivation_queue LIMIT 1"):
        derivation.drain(served.resolve(), writer)


def test_a_link_guarded_write_refuses_while_more_pages_wait_than_a_check_reads(
    served, writer, monkeypatch  # noqa: F811
):
    """A delete that checks every link, with more pages waiting to derive than one check reads: it
    refuses, retryably, before it parses a waiting page, and commits nothing. Once the store thread
    has derived them the same delete runs."""
    save(served, HARBOUR, SMALL_TEXT.replace("Small", "Harbour"))
    commit_pending_pages(served, writer, monkeypatch, derivation.UNDERIVED_CAP + 1)
    parsed = []
    real = vault_module.inbound_rows
    monkeypatch.setattr(vault_module, "inbound_rows", lambda *arguments: parsed.append(arguments[0]) or real(*arguments))
    revision = store_rows(served, "SELECT revision FROM store_mode")

    with pytest.raises(OpError) as refused:
        delete_file.delete_file(served, path=HARBOUR, confirm=True)

    assert refused.value.code == read.DERIVED_BEHIND
    assert "Retry in a few seconds" in refused.value.remediation
    assert parsed == [], "the refused check parsed pages while it could not answer"
    assert read.page_exists(served, HARBOUR)
    assert store_rows(served, "SELECT revision FROM store_mode") == revision
    assert store_rows(served, "SELECT 1 FROM store_derivation_queue WHERE path = ?", HARBOUR) == []

    # The commit's own guard on the linking pages meets the same backlog inside its transaction.
    with pytest.raises(OpError) as guarded:
        write.commit(writer, write.StoreChange(
            write.Origin.GOVERNED, "delete", deletes=(write.PageDelete(HARBOUR),),
            expect={HARBOUR: write.Expect(linked_from=frozenset())},
        ))

    assert guarded.value.code == read.DERIVED_BEHIND
    assert read.page_exists(served, HARBOUR)
    assert store_rows(served, "SELECT revision FROM store_mode") == revision

    drain_all(served, writer)
    delete_file.delete_file(served, path=HARBOUR, confirm=True)

    assert not read.page_exists(served, HARBOUR)


def test_a_page_read_right_after_a_save_keeps_its_ref_beside_a_backlog_past_the_cap(
    served, writer, monkeypatch  # noqa: F811
):
    """A saved page whose rows wait, among more waiting pages than one check reads: the ref of the
    page comes from its own stored bytes, and the check parses no other waiting page. A write asks
    for this ref after it commits, so a refusal here would answer an error for a write that stands."""
    identifier = "6f1c8e9a-2b4d-4c3e-9a7f-1d2e3f4a5b6c"
    body = f"---\ntitle: Tide table\nexomem_id: {identifier}\n---\n# Tide table\n".encode()
    tide = "Knowledge Base/Notes/Tide table.md"
    commit_pending_pages(served, writer, monkeypatch, derivation.UNDERIVED_CAP + 1)
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "save", puts=(write.PagePut(tide, body),)))
    parsed = []
    real = memory_refs.identity_row
    monkeypatch.setattr(memory_refs, "identity_row", lambda root, rel, raw: parsed.append(rel) or real(root, rel, raw))

    ref = memory_refs.ReferenceIndex(served).ref_for_path(tide)

    assert ref == memory_refs.memory_ref(identifier)
    assert set(parsed) == {tide}, "the check parsed pages besides the one it was asked about"


def test_a_moved_page_keeps_its_ref_beside_a_backlog_past_the_cap(
    served, writer, monkeypatch  # noqa: F811
):
    """A move whose rows wait, among more waiting pages than one check reads: the identity row still
    names the old path, a page outside the question. Counted as an owner, that row would make the id
    of the moved page ambiguous and its ref empty."""
    identifier = "6f1c8e9a-2b4d-4c3e-9a7f-1d2e3f4a5b6c"
    before, after = "Knowledge Base/Notes/Tide table.md", "Knowledge Base/Notes/Archive/Tide table.md"
    body = f"---\ntitle: Tide table\nexomem_id: {identifier}\n---\n# Tide table\n".encode()
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "file note", puts=(write.PagePut(before, body),)))
    drain_all(served, writer)
    commit_pending_pages(served, writer, monkeypatch, derivation.UNDERIVED_CAP + 1)
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "move note",
                                               moves=(write.PageMove(before, after),)))

    assert memory_refs.ReferenceIndex(served).ref_for_path(after) == memory_refs.memory_ref(identifier)


def record_failures(writer, attempts):  # noqa: F811
    """Give every intent in the queue ``attempts`` recorded failures and a retry far ahead, as the
    drain leaves it, without running that many failing derivations."""
    later = derivation._stamp(derivation._now() + timedelta(seconds=derivation.MAX_RETRY_SECONDS))
    with writer.handle.transaction() as tx:
        tx.execute(
            "UPDATE store_derivation_queue SET attempts = ?, failed_code = ?, retry_at = ?",
            (attempts, derivation.FAILED, later),
        )


def pending_bytes(conn, paths=None):
    """What ``derivation.underived_parsed`` gives a check that keeps each page's stored bytes."""
    return derivation.underived_parsed(conn, paths, "stored bytes", lambda _rel, raw: raw)


def test_derivations_that_used_up_their_attempts_never_make_a_link_guarded_write_refuse(
    served, writer, monkeypatch  # noqa: F811
):
    """More pages whose derivation used up its attempts than one check reads, and a few that still
    wait: such an intent keeps retrying on a growing backoff and never leaves the queue while it
    fails, so counting it toward the cap would refuse every delete for good, the delete that frees room for those rows
    included. The delete runs, and the check still reads the failed pages' bytes, not their rows."""
    save(served, HARBOUR, SMALL_TEXT.replace("Small", "Harbour"))
    commit_pending_pages(served, writer, monkeypatch, derivation.UNDERIVED_CAP + 1, "Failed")
    record_failures(writer, derivation.ATTEMPTS)
    commit_pending_pages(served, writer, monkeypatch, 3, "Waiting")
    with read.snapshot(served) as view:
        found = pending_bytes(view.connection)
    assert all(found[f"Knowledge Base/Notes/Failed {number}.md"] for number in range(derivation.UNDERIVED_CAP + 1))

    delete_file.delete_file(served, path=HARBOUR, confirm=True)

    assert not read.page_exists(served, HARBOUR)


def test_derivations_that_failed_once_still_count_toward_the_cap(served, writer, monkeypatch):  # noqa: F811
    """More pages than one check reads, each failed once: they retry within seconds and leave the
    queue when a retry lands, so they are a backlog still, and a check that reads every one of them
    refuses. Exempting a page at its first failure let such a backlog be parsed in full."""
    commit_pending_pages(served, writer, monkeypatch, derivation.UNDERIVED_CAP + 1, "Retrying")
    record_failures(writer, 1)

    with read.snapshot(served) as view, pytest.raises(OpError) as refused:
        pending_bytes(view.connection)

    assert refused.value.code == read.DERIVED_BEHIND


def test_a_page_with_a_duplicated_id_has_no_ref_while_both_pages_wait_for_their_rows(
    served, writer, monkeypatch  # noqa: F811
):
    """Two pages that hold one id, both edited with their derivation held back. Each page's row
    names the id, so the id has two owners: the ref of either page is ambiguous, as it is once the
    rows arrive. Dropping a waiting owner's row without reading its bytes made one page the only
    owner."""
    identifier = "6f1c8e9a-2b4d-4c3e-9a7f-1d2e3f4a5b6c"
    first, second = "Knowledge Base/Notes/Tide table.md", "Knowledge Base/Notes/Tide copy.md"

    def page(title, extra=""):
        return f"---\ntitle: {title}\nexomem_id: {identifier}\n---\n# {title}\n{extra}".encode()

    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "file notes", puts=(
        write.PagePut(first, page("Tide table")), write.PagePut(second, page("Tide copy")),
    )))
    drain_all(served, writer)
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "edit notes", puts=(
            write.PagePut(first, page("Tide table", "\nHigh at noon.\n")),
            write.PagePut(second, page("Tide copy", "\nHigh at noon.\n")),
        )))

    assert memory_refs.ReferenceIndex(served).ref_for_path(first) is None


def test_a_folder_delete_with_no_page_parses_no_waiting_page(served, writer, monkeypatch):  # noqa: F811
    """An empty folder is deleted while many pages wait, each with its file on disk. The links into
    the folder's pages are none, so the check has nothing to read: it must not parse every waiting
    page that the walk of the vault reaches."""
    (served / "Knowledge Base/Projects/Empty").mkdir(parents=True)
    commit_pending_pages(served, writer, monkeypatch, 400, projected=True)
    parsed = []
    real = vault_module.inbound_rows
    monkeypatch.setattr(vault_module, "inbound_rows", lambda *arguments: parsed.append(arguments[0]) or real(*arguments))

    with monkeypatch.context() as patch:  # the commit's own drain would parse them as well
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        delete_directory.delete_directory(served, path="Knowledge Base/Projects/Empty", confirm=True)

    assert parsed == [], "the delete parsed waiting pages that no link of the folder could come from"


def test_a_keyed_read_of_more_pages_than_the_cap_is_capped_like_a_read_of_every_page(
    served, writer, monkeypatch  # noqa: F811
):
    """A keyed read of the waiting pages among a caller's paths, when the caller names more paths
    than a check reads, and more pages wait than it reads: the paths would reach every waiting page,
    so the read takes the capped form and refuses instead of reading them all. Fewer paths read
    their own pages alone."""
    commit_pending_pages(served, writer, monkeypatch, 400)
    waiting = [f"Knowledge Base/Notes/Backlog {number}.md" for number in range(400)]

    with read.snapshot(served) as view:
        few = pending_bytes(view.connection, waiting[:10])
        with pytest.raises(OpError) as refused:
            pending_bytes(view.connection, waiting)

    assert sorted(few) == sorted(waiting[:10]) and all(body is not None for body in few.values())
    assert refused.value.code == read.DERIVED_BEHIND


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


def test_a_page_whose_derivation_failed_runs_apart_from_the_pages_of_a_later_save(
    served, writer, monkeypatch  # noqa: F811
):
    """A page whose rows failed once and fall due again beside a later save of two pages: the save's
    pages derive together, in one transaction. Taken into their batch, the failing page rolled the
    batch back, and the save's pages then ran one transaction each."""
    failing = "Knowledge Base/Notes/Failing.md"
    real = derive.derive_paths
    batches = []

    def derive_paths(tx, root, *, written, **keywords):
        batches.append(sorted(written))
        if failing in written:
            raise RuntimeError("the derivation broke")
        return real(tx, root, written=written, **keywords)

    monkeypatch.setattr(derive, "derive_paths", derive_paths)
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        save(served, failing, SMALL_TEXT.replace("Small", "Failing"))
    derivation.drain(served.resolve(), writer)
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
    batches.clear()
    # Later than the first retry falls due.
    later = derivation._now() + timedelta(seconds=derivation.RETRY_SECONDS * 4)
    monkeypatch.setattr(derivation, "_now", lambda: later)

    derivation.drain(served.resolve(), writer)

    assert batches == [[failing], sorted([LARGE, SMALL])]


def test_a_failed_batch_that_outlasts_a_slice_still_settles_one_page_of_its_revision(
    served, writer, monkeypatch  # noqa: F811
):
    """A save of a good and a bad page whose batch fails only after more than a slice. The drain
    took the deadline before it derived either page alone, so both intents stayed as they were and
    the next drain took the same batch again: the good page never derived, the bad one never
    failed, and every read waited for them."""
    good, bad = "Knowledge Base/Notes/Aaa Good.md", "Knowledge Base/Notes/Zzz Bad.md"
    real = derive.derive_paths

    def derive_paths(tx, root, *, written, **keywords):
        if bad in written:
            if len(written) > 1:
                time.sleep(derivation.SLICE_SECONDS * 1.3)
            raise RuntimeError("the derivation broke")
        return real(tx, root, written=written, **keywords)

    monkeypatch.setattr(derive, "derive_paths", derive_paths)
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        with writer_lease.get_manager().mutation_guard(served, operation="note_save"):
            vault_module.batch_atomic_write(
                [
                    planned(served, good, SMALL_TEXT.replace("Small", "Good"), create_only=True),
                    planned(served, bad, SMALL_TEXT.replace("Small", "Bad"), create_only=True),
                ],
                vault_root=served,
                post_commit_fanout=False,
            )

    derivation.drain(served.resolve(), writer, deadline=time.monotonic() + derivation.SLICE_SECONDS)
    after_one = store_rows(served, "SELECT path FROM store_derivation_queue")
    derivation.drain(served.resolve(), writer, deadline=time.monotonic() + derivation.SLICE_SECONDS)

    assert after_one == [(bad,)], "the first drain derived the good page"
    assert store_rows(served, "SELECT path, attempts, failed_code FROM store_derivation_queue") == [
        (bad, 1, derivation.FAILED)
    ]
    assert good in bm25_paths(served, "ferry")


def test_a_failed_batch_is_not_taken_whole_again_on_each_turn_of_the_store_thread(
    served, writer, monkeypatch  # noqa: F811
):
    """A save of six pages, the last one bad, whose batch fails only after more than a slice. Each
    turn of the store thread then derived one page alone and took the rest as a batch again, which
    failed again: five failing batches for one bad page, with every read waiting meanwhile."""
    bad = "Knowledge Base/Notes/Zzz Bad.md"
    good = [f"Knowledge Base/Notes/Good {number}.md" for number in range(5)]
    monkeypatch.setattr(derivation, "SLICE_SECONDS", 0.05)
    real = derive.derive_paths
    batches = []

    def derive_paths(tx, root, *, written, **keywords):
        batches.append(sorted(written))
        if bad in written:
            if len(written) > 1:
                time.sleep(derivation.SLICE_SECONDS * 2)
            raise RuntimeError("the derivation broke")
        return real(tx, root, written=written, **keywords)

    monkeypatch.setattr(derive, "derive_paths", derive_paths)
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        with writer_lease.get_manager().mutation_guard(served, operation="note_save"):
            vault_module.batch_atomic_write(
                [planned(served, rel, SMALL_TEXT.replace("Small", rel.split("/")[-1][:-3]), create_only=True)
                 for rel in (*good, bad)],
                vault_root=served,
                post_commit_fanout=False,
            )

    turns = 0
    # Drain until every good page is derived and the bad page has failed on its own too: a slice can
    # end after the last good page, before the bad page's own attempt records its code.
    while turns < 2 * (len(good) + 1) and store_rows(
        served, "SELECT 1 FROM store_derivation_queue WHERE path != ? OR failed_code IS NULL", bad
    ):
        derivation.drain(served.resolve(), writer, deadline=time.monotonic() + derivation.SLICE_SECONDS)
        turns += 1

    whole = [batch for batch in batches if len(batch) > 1]
    assert len(whole) == 1, f"the failed batch was taken {len(whole)} times"
    assert store_rows(served, "SELECT path FROM store_derivation_queue") == [(bad,)], "a good page waits"
    assert set(good) <= set(bm25_paths(served, "ferry"))
    attempts, code, retry_at = store_rows(
        served, "SELECT attempts, failed_code, retry_at FROM store_derivation_queue"
    )[0]
    assert attempts == 1, "the batch's failure counted as one of the page's own"
    assert code == derivation.FAILED and retry_at is not None


def test_a_check_beside_many_failed_pages_parses_each_of_them_once(served, writer, monkeypatch):  # noqa: F811
    """More pages whose derivation used up its attempts than one check reads, which stay queued for
    good while they fail: each check that reads every pending page parsed all of them again. Each
    parses at most once for as long as the page is unchanged, and a page that changed is read anew."""
    save(served, HARBOUR, SMALL_TEXT.replace("Small", "Harbour"))
    failed = [f"Knowledge Base/Notes/Stuck {number}.md" for number in range(derivation.UNDERIVED_CAP + 44)]
    commit_pending_pages(served, writer, monkeypatch, len(failed), "Stuck")
    record_failures(writer, derivation.ATTEMPTS)
    links, identities = [], []
    real_links, real_identity = vault_module.inbound_rows, memory_refs._identity_from_bytes
    monkeypatch.setattr(vault_module, "inbound_rows", lambda *arguments: links.append(arguments[0]) or real_links(*arguments))
    monkeypatch.setattr(memory_refs, "_identity_from_bytes", lambda *arguments: identities.append(arguments[0]) or real_identity(*arguments))

    for _check in range(2):
        vault_module.store_pages_named(served, "Stuck 0")
        memory_refs.store_owners_by_prefix(served, "a")

    assert sorted(links) == sorted(failed), "a check parsed a failed page's links more than once"
    assert sorted(identities) == sorted(failed), "a check parsed a failed page's identity more than once"

    # A save to one of them changes its bytes: a check that served the old parse would miss its link.
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "relink", puts=(
            write.PagePut(failed[0], SMALL_TEXT.replace("dawn", "dawn from [[Harbour]]").encode()),
        )))
    links.clear()

    with pytest.raises(delete_file.DeleteFileError) as refused:
        delete_file.delete_file(served, path=HARBOUR, confirm=True)

    assert refused.value.code == "INBOUND_LINKS" and f"{failed[0]}:" in refused.value.reason
    assert links == [failed[0]], "the check parsed pages that had not changed"


def test_an_upload_behind_a_backlog_derives_its_own_rows_while_it_holds_its_credit(
    served, writer, monkeypatch  # noqa: F811
):
    """An upload commits its page under the credit that reserves the room for its rows, behind a
    backlog that takes the whole slice. Its rows were derived from the oldest intent on, so the
    backlog took the slice and the upload's rows waited until its credit had ended: a retry for room
    that its reservation had covered."""
    upload = "Knowledge Base/Notes/Upload.md"
    for number in range(6):
        commit_pending_pages(served, writer, monkeypatch, 1, f"Older {number}")
    monkeypatch.setattr(derivation, "SLICE_SECONDS", 0.2)
    monkeypatch.setattr(derivation, "BATCH_PAGES", 1)  # a batch a revision, each as long as a slice
    real = derive.derive_paths

    def derive_paths(*arguments, **keywords):
        time.sleep(0.15)
        return real(*arguments, **keywords)

    monkeypatch.setattr(derive, "derive_paths", derive_paths)

    with media_capacity.commit_credit("upload-session"):
        write.commit(writer, write.StoreChange(
            write.Origin.GOVERNED, "upload",
            puts=(write.PagePut(upload, SMALL_TEXT.encode() + b"\nFollow [[Beta#^signal]].\n"),),
        ))
        waiting = store_rows(served, "SELECT path FROM store_derivation_queue")

    assert upload not in {path for (path,) in waiting}, "the upload's rows waited for the backlog"
    assert waiting, "the older intents must still be queued for this check to mean anything"
    assert store_rows(
        served, "SELECT fragment FROM graph_unit_links WHERE source_path = ?", upload,
    ) == [("^signal",)]


def test_a_page_whose_derivation_failed_holds_no_reader_back(served, writer, monkeypatch):  # noqa: F811
    """A save whose derivation failed and waits for its retry, which can be minutes away. A reader
    that waited for it would wait out its whole bound on every request until then."""
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        save(served, SMALL, SMALL_TEXT)
    fail_derivations(served, writer, monkeypatch, RuntimeError("the derivation broke"), 1)
    monkeypatch.setattr(derivation, "READ_WAIT_SECONDS", 30.0)
    monkeypatch.setattr(runtime, "drains", lambda _root: True)  # a reader waits only for a drainer

    began = time.monotonic()
    with read.request_snapshot(served, catch_up=True):
        behind = read.request_registry_notice(served)
    waited = time.monotonic() - began

    assert waited < 5, f"the read waited {waited:.1f} s for a derivation that had failed"
    assert behind is not None and behind["lag"]["derived_pending"] == 1


def test_a_reader_with_no_drainer_answers_at_once_and_says_what_lags(served, writer, monkeypatch):  # noqa: F811
    """A save whose rows nothing in this process derives: the service is down after a crash, and
    a command-line read runs. Waiting for rows that would not arrive, every such read paid the
    whole wait bound."""
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        save(served, SMALL, SMALL_TEXT)
    monkeypatch.setattr(derivation, "READ_WAIT_SECONDS", 30.0)

    began = time.monotonic()
    with read.request_snapshot(served, catch_up=True):
        behind = read.request_registry_notice(served)
    waited = time.monotonic() - began

    assert waited < 5, f"the read waited {waited:.1f} s for rows that no process derives"
    assert behind is not None and behind["lag"]["derived_pending"] == 1


def test_a_reader_does_not_wait_for_a_write_that_committed_after_it_began(
    served, writer, monkeypatch  # noqa: F811
):
    """A read that began while one save's rows were pending, and during whose wait a second save
    committed. Waiting for the later save too, a reader under a steady stream of saves would wait
    out its whole bound on every request."""
    later = "Knowledge Base/Notes/Later.md"
    monkeypatch.setattr(derivation, "READ_WAIT_SECONDS", 30.0)
    monkeypatch.setattr(derivation, "BATCH_PAGES", 1)  # the drain derives the saves one at a time
    monkeypatch.setattr(runtime, "drains", lambda _root: True)  # this test drains, as the store thread
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        save(served, SMALL, SMALL_TEXT)
    waiting, answered = threading.Event(), threading.Event()
    real_caught_up = derivation.caught_up

    def caught_up(still_held_back, seconds):
        waiting.set()
        return real_caught_up(still_held_back, seconds)

    monkeypatch.setattr(derivation, "caught_up", caught_up)
    seen = []

    def search():
        with read.request_snapshot(served, catch_up=True), read.snapshot(served) as view:
            seen.extend(path for (path,) in view.connection.execute(
                "SELECT path FROM pages WHERE path IN (?, ?)", (SMALL, later)
            ))
        answered.set()

    reader = threading.Thread(target=search, daemon=True)
    reader.start()
    assert waiting.wait(30), "the reader must wait for the first save for this check to mean anything"
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        save(served, later, SMALL_TEXT.replace("Small", "Later"))
    real = derive.derive_paths
    answered_first = []

    def derive_paths(tx, root, *, written, **keywords):
        if later in written:
            answered_first.append(answered.wait(10))
        return real(tx, root, written=written, **keywords)

    monkeypatch.setattr(derive, "derive_paths", derive_paths)
    derivation.drain(served.resolve(), writer)
    reader.join(30)

    assert answered_first == [True], "the reader waited for the save that committed after it began"
    assert seen == [SMALL]
