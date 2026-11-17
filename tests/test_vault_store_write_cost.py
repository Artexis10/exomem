"""What a store-mode write pays per page (OpenSpec make-vault-store-canonical, task 2.2).

A store-mode write runs with the store thread's writer bound, so every caller's release filter
decides each page, the owner's included. These tests hold the write to work that does not grow with
the vault: decisions that do not grow with the pages (a set is decided by scope class, each page that
the class route cannot prove stays per page); one policy load and one store snapshot per set of
decisions, whose vault store readers, path resolutions and SQL statements do not grow with the pages
decided; the corpus admission boundary records no receipt; and no registry re-derivation for a write
that changes no registry input.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections import Counter
from contextlib import closing, contextmanager

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401

from exomem import commands, entity_recurrence
from exomem.collection_store import connection
from exomem.collection_store import governance as store_governance
from exomem.collection_store.writer import CollectionWriter
from exomem.governance import egress, policy
from exomem.vault_store import rederive, write

stored, writer, served = read_write.stored, read_write.writer, write_path.served


def grow(writer, start, count, *, on_disk=None):
    """Commit ``count`` pages: insight notes that link each other, and People entities.

    ``on_disk`` is a vault root that also holds each page as a file, as a vault that was a file vault does.
    """
    puts = []
    for number in range(start, start + count):
        if number % 4 == 0:
            rel = f"Knowledge Base/Entities/People/Person {number}.md"
            body = (
                f"---\ntitle: Person {number}\ntype: entity\nentity_type: person\n"
                f"status: active\n---\n# Person {number}\n\nA keeper of the tide ledger.\n"
            )
        else:
            rel = f"Knowledge Base/Notes/Note {number}.md"
            body = (
                f"---\ntitle: Note {number}\ntype: insight\nstatus: active\n---\n"
                f"# Note {number}\n\nThe tide rose, as [[Note {max(number - 1, 1)}]] records.\n"
            )
        puts.append(write.PagePut(rel, body.encode()))
        if on_disk is not None:
            (on_disk / rel).parent.mkdir(parents=True, exist_ok=True)
            (on_disk / rel).write_bytes(body.encode())
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "growth", puts=tuple(puts)))


class Counts:
    """How often a write decides a page, loads the policy, builds a store authorization, opens a
    store snapshot and builds the registry index; how often its decisions open a vault store reader
    and resolve a path; how many SQL statements its decision sweeps issue; and how many receipts its
    corpus admission boundary records."""

    NAMES = (
        "decisions", "loads", "authorizations", "snapshots", "readers", "realpaths", "indexes",
        "sweep_statements", "corpus_receipts",
    )
    #: The command name the corpus build gives the boundary that decides its pages.
    CORPUS_BOUNDARY = "semantic_corpus_admission"

    def __init__(self, monkeypatch, writer):
        self.seen = Counter()
        for name, owner, attribute in (
            ("loads", policy, "load"),
            ("authorizations", store_governance.OperationAuthorization, "__init__"),
            ("indexes", entity_recurrence, "registry_index"),
        ):
            monkeypatch.setattr(owner, attribute, self.counted(name, getattr(owner, attribute)))
        snapshot = CollectionWriter.read_snapshot

        def opened(writer, *args, **kwargs):
            # A snapshot nested in an open transaction joins it; only an outer one opens one.
            self.seen["snapshots"] += not writer.connection.in_transaction
            return snapshot(writer, *args, **kwargs)

        monkeypatch.setattr(CollectionWriter, "read_snapshot", opened)
        # Lease, freshness and warm-up work resolves paths and opens readers on its own schedule,
        # so only what a decision does counts.
        deciding, sweeping, decide = threading.local(), threading.local(), egress._decide_path

        def decided(*args, **kwargs):
            self.seen["decisions"] += 1
            deciding.depth = getattr(deciding, "depth", 0) + 1
            sweeping.depth = getattr(sweeping, "depth", 0) + 1
            try:
                return decide(*args, **kwargs)
            finally:
                deciding.depth -= 1
                sweeping.depth -= 1

        monkeypatch.setattr(egress, "_decide_path", decided)
        context = egress.release_decision_context

        @contextmanager
        def held(vault_root):
            sweeping.depth = getattr(sweeping, "depth", 0) + 1
            try:
                with context(vault_root):
                    yield
            finally:
                sweeping.depth -= 1

        monkeypatch.setattr(egress, "release_decision_context", held)

        def traced(_statement):
            # Every statement that a page or a set of pages is decided with, on any connection: the
            # page-body and the single-key metadata reads of a per-page decision are among them.
            self.seen["sweep_statements"] += getattr(sweeping, "depth", 0) > 0

        writer.connection.set_trace_callback(traced)
        connect = sqlite3.connect

        def connect_traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(traced)
            return conn

        monkeypatch.setattr(sqlite3, "connect", connect_traced)
        for name, owner, attribute in (
            # A vault store snapshot that joins none opens a reader: a pooled lend when served.
            ("readers", connection, "store_reader"),
            ("realpaths", os.path, "realpath"),
        ):
            monkeypatch.setattr(
                owner, attribute, self.counted(name, getattr(owner, attribute), deciding)
            )
        boundary = egress.disclosure_boundary

        @contextmanager
        def watched(vault_root, command_name, **options):
            with boundary(vault_root, command_name, **options) as collector:
                try:
                    yield collector
                finally:
                    if command_name == self.CORPUS_BOUNDARY:
                        self.seen["corpus_receipts"] += len(collector.outcomes) + len(collector.path_outcomes)

        monkeypatch.setattr(egress, "disclosure_boundary", watched)

    def counted(self, name, function, inside=None):
        def counting(*args, **kwargs):
            self.seen[name] += inside is None or getattr(inside, "depth", 0) > 0
            return function(*args, **kwargs)

        return counting

    def take(self):
        taken = {name: self.seen[name] for name in self.NAMES}
        self.seen.clear()
        return taken


def registry_state(root):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return rederive.read(reader)


def page_count(root):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute("SELECT count(*) FROM pages").fetchone()[0]


def create(root, title):
    """A create that records a reviewed-none relation decision; the new page's path."""
    return commands.op_remember(
        root, title=title, summary="A page of the write-cost test.",
        content=f"## Observations\n\n- [finding] Revision 0 of {title} reads [[Note 2]].\n",
        relation_disposition="reviewed_none",
        relation_review_reason="a page of the write-cost test",
    )["path"]


def edit(root, path):
    commands.op_edit_memory(
        root, path=path, why="the cost of an edit",
        operation={
            "kind": "replace_string", "old_string": "Revision 0", "new_string": "Revision 1",
        },
    )


@pytest.mark.timeout(300)
def test_a_store_writes_decisions_do_not_grow_with_the_pages_under_one_policy_load(
    served, writer, monkeypatch  # noqa: F811
):
    """A write that decides each page of the vault one at a time, in the sweeps of its corpus and
    registry advisories, costs 14 times a file-mode write at 20,000 pages. The owner's decision of
    a page that only its scopes determine is one class verdict, so a create and an edit are measured
    at N and 2N pages, with each page also a file: the decisions and the sweeps' statements stay
    flat, and the corpus admission boundary, which no receipt is emitted from, records none.
    """
    grow(writer, 1, 24, on_disk=served)
    # The first write of a process loads what the later writes reuse.
    edit(served, create(served, "Warm-up"))
    counts = Counts(monkeypatch, writer)
    measured = []
    for title, more in (("First probe", 0), ("Second probe", 24)):
        if more:
            grow(writer, 1000, more, on_disk=served)
        pages = page_count(served)
        before = registry_state(served)
        counts.take()
        path = create(served, title)
        created = counts.take()
        edit(served, path)
        edited = counts.take()
        measured.append((pages, created, edited))
        assert registry_state(served) == before, "a note and its relation review move no target"
    (pages_n, create_n, edit_n), (pages_2n, create_2n, edit_2n) = measured
    grown = pages_2n - pages_n
    assert grown >= 24, measured
    for at_n, at_2n in ((create_n, create_2n), (edit_n, edit_2n)):
        # The page that a write itself changes may stay per page; a page of the vault may not.
        assert at_2n["decisions"] - at_n["decisions"] <= 2, measured
        # A per-page decision issues at least one statement (its snapshot, facts and page reads).
        assert at_2n["sweep_statements"] - at_n["sweep_statements"] < grown, measured
        assert at_n["corpus_receipts"] == at_2n["corpus_receipts"] == 0, measured
        for shared in ("loads", "authorizations", "snapshots"):
            assert at_2n[shared] <= at_n[shared], (shared, measured)
        # Under load a decision now and then resolves a path more; one per page is the defect.
        for per_set in ("readers", "realpaths"):
            assert at_2n[per_set] - at_n[per_set] < grown, (per_set, measured)
        assert at_n["indexes"] == at_2n["indexes"] == 1, measured
