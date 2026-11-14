"""What a store-mode write pays per page (OpenSpec make-vault-store-canonical, task 2.2).

A store-mode write runs with the store thread's writer bound, so every caller's release filter
decides each page, the owner's included. These tests hold the write to work that does not grow with
the vault: a bounded number of decisions per page, one policy load and one store snapshot per set of
decisions, and no registry re-derivation for a write that changes no registry input.
"""

from __future__ import annotations

import sys
from collections import Counter
from contextlib import closing

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

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

stored, writer, served = read_write.stored, read_write.writer, write_path.served


def grow(writer, start, count):
    """Commit ``count`` pages: insight notes that link each other, and People entities."""
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
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "growth", puts=tuple(puts)))


class Counts:
    """How often a write decides a page, loads the policy, builds a store authorization, opens a
    store snapshot and builds the entity registry index."""

    NAMES = ("decisions", "loads", "authorizations", "snapshots", "indexes")

    def __init__(self, monkeypatch):
        self.seen = Counter()
        for name, owner, attribute in (
            ("decisions", egress, "_decide_path"),
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

    def counted(self, name, function):
        def counting(*args, **kwargs):
            self.seen[name] += 1
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
def test_a_store_write_decides_each_page_a_bounded_number_of_times_under_one_policy_load(
    served, writer, monkeypatch  # noqa: F811
):
    """A write that opens a store snapshot and loads the policy for each page it decides, decides
    the corpus in every advisory and builds the entity registry in each: at 1,000 pages it costs
    20 times a file-mode write. A create and an edit are measured at N pages and at 2N."""
    grow(writer, 1, 24)
    # The first write of a process loads what the later writes reuse.
    edit(served, create(served, "Warm-up"))
    counts = Counts(monkeypatch)
    measured = []
    for title, more in (("First probe", 0), ("Second probe", 24)):
        if more:
            grow(writer, 1000, more)
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
    # A create sweeps the corpus before and after its commit, and an edit once.
    assert create_2n["decisions"] - create_n["decisions"] <= 3 * grown, measured
    assert edit_2n["decisions"] - edit_n["decisions"] <= 2 * grown, measured
    for at_n, at_2n in ((create_n, create_2n), (edit_n, edit_2n)):
        for shared in ("loads", "authorizations", "snapshots"):
            assert at_2n[shared] <= at_n[shared], (shared, measured)
        assert at_n["indexes"] == at_2n["indexes"] == 1, measured
