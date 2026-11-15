"""A store-mode commit is a delta of the semantic corpus context, as a file-mode write is.

The census holds one entry for each stored page that a build reads, from the store's own rows, so
the cached context re-derives the pages that a commit changed, not the corpus. Each test commits one
kind of change to a warm context, then compares the context that the cache serves with a cold build
of the same store state. The projector is off, so a page's file trails the store: a step that read
the files would see the old page.
"""

from __future__ import annotations

import sys

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_corpus_context_cache import _resolver_maps
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, BETA, vault  # noqa: F401
from test_vault_store_readers import EXTERNAL, restrict_external
from test_vault_store_rederive import (
    READ_ONLY_NOTES,
    commit_policy,
    project,
    settle_to_the_end,
)
from test_vault_store_write_cost import create, edit, grow, page_count

from exomem import delete_file, recover_from_trash
from exomem import semantic_contract as sc
from exomem.governance import egress
from exomem.governance.principal import request_scope
from exomem.vault_store import rederive, write

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

stored, writer, served = read_write.stored, read_write.writer, write_path.served

GAMMA = "Knowledge Base/Notes/Gamma.md"
GAMMA_TEXT = b"---\ntitle: Gamma\ntype: note\n---\n# Gamma\n\nThe harbour master reads [[Alpha]].\n"
HIDDEN = "Knowledge Base/Notes/Ledger Keeper.md"
HIDDEN_TEXT = b"---\ntitle: Ledger Keeper\ntype: note\ntags: [secret]\n---\n# Ledger Keeper\n\nPrivate.\n"
LINKER = "Knowledge Base/Notes/Harbour Log.md"
LINKER_TEXT = b"---\ntitle: Harbour Log\ntype: note\n---\n# Harbour Log\n\nSee [[Ledger Keeper]].\n"
OVERLAY = "Knowledge Base/_Schema/note-types.yaml"
HARBOUR = "Knowledge Base/Notes/Harbour/Tide.md"
OWNER = "Knowledge Base/trash/Old Owner.md"
OWNER_ID = "5f3c2b1a-9d4e-4f6a-8b7c-1e2d3c4b5a69"


def commit(writer, *, puts=(), moves=(), deletes=()):
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "corpus census test",
        puts=tuple(write.PagePut(rel, body) for rel, body in puts),
        moves=tuple(write.PageMove(src, dst) for src, dst in moves),
        deletes=tuple(write.PageDelete(rel) for rel in deletes),
    ))


class Builds:
    """How many times the identity census is built, and how many pages those builds parse: the
    corpus-sized work that a delta spares."""

    def __init__(self, monkeypatch):
        self.builds = self.pages = 0
        real = sc._build_identity_census

        def counted(*args, **kwargs):
            census = real(*args, **kwargs)
            self.builds += 1
            self.pages += len(census[0].entries)
            return census

        monkeypatch.setattr(sc, "_build_identity_census", counted)

    def take(self):
        taken = (self.builds, self.pages)
        self.builds = self.pages = 0
        return taken


def cold(root, monkeypatch):
    """A cold build of the store state, as a process with the cache off reads it."""
    sc.reset_corpus_context_cache()
    with monkeypatch.context() as off:
        off.setenv("EXOMEM_DISABLE_CORPUS_CACHE", "1")
        return sc.build_corpus_context(root)


def assert_equal(cached, rebuilt):
    assert cached.as_dict() == rebuilt.as_dict()
    assert _resolver_maps(cached) == _resolver_maps(rebuilt)
    assert (
        cached.identity_census.reference_only_entries
        == rebuilt.identity_census.reference_only_entries
    )


def test_a_create_and_an_edit_are_deltas_that_equal_a_cold_build(
    stored, writer, monkeypatch, corpus_cache_on  # noqa: F811
):
    """A commit that moves the store revision makes the cache rebuild the corpus, which strict-parses
    every stored page's frontmatter for each write."""
    sc.build_corpus_context(stored)
    builds = Builds(monkeypatch)

    commit(writer, puts=[(GAMMA, GAMMA_TEXT)])
    created = sc.build_corpus_context(stored)
    assert builds.take() == (0, 0)
    assert GAMMA in created.pages
    assert_equal(created, cold(stored, monkeypatch))

    sc.build_corpus_context(stored)
    commit(writer, puts=[(GAMMA, GAMMA_TEXT.replace(b"[[Alpha]]", b"[[Beta]]"))])
    builds.take()
    edited = sc.build_corpus_context(stored)
    assert builds.take() == (0, 0)
    assert edited.pages[GAMMA].source_hash != created.pages[GAMMA].source_hash
    assert_equal(edited, cold(stored, monkeypatch))


def test_a_move_drops_the_old_path_and_equals_a_cold_build(
    stored, writer, monkeypatch, corpus_cache_on  # noqa: F811
):
    moved = "Knowledge Base/Notes/Archive/Beta.md"
    sc.build_corpus_context(stored)
    builds = Builds(monkeypatch)

    commit(writer, moves=[(BETA, moved)])
    cached = sc.build_corpus_context(stored)

    assert builds.take() == (0, 0)
    assert BETA not in cached.pages and moved in cached.pages
    assert BETA not in {entry.path for entry in cached.identity_census.entries}
    assert_equal(cached, cold(stored, monkeypatch))


def test_a_deleted_withheld_page_leaves_the_context_that_a_restricted_caller_reads(
    stored, writer, monkeypatch, corpus_cache_on  # noqa: F811
):
    """A stale entry for a deleted page reaches a restricted caller: the release filter keeps a path
    that names no page, so only the context can withhold a page that is gone."""
    restrict_external(stored, 'tags: ["secret"]')
    commit(writer, puts=[(HIDDEN, HIDDEN_TEXT), (LINKER, LINKER_TEXT)])
    with request_scope(EXTERNAL):
        assert not egress.visible_page_filter(stored)(HIDDEN)
        assert HIDDEN in sc.build_corpus_context(stored).pages
    builds = Builds(monkeypatch)

    commit(writer, deletes=[HIDDEN])
    with request_scope(EXTERNAL):
        assert egress.visible_page_filter(stored)(HIDDEN)
        cached = sc.build_corpus_context(stored)

    assert builds.take() == (0, 0)
    assert HIDDEN not in repr(cached.as_dict())
    assert HIDDEN not in repr(_resolver_maps(cached))
    assert_equal(cached, cold(stored, monkeypatch))


def test_a_trash_and_a_recovery_remove_the_page_and_bring_it_back(
    served, monkeypatch, corpus_cache_on  # noqa: F811
):
    """The trash folder holds no page that a build reads, so a trashed page leaves the context."""
    sc.build_corpus_context(served)
    trash_path = delete_file.delete_file(
        served, path=BETA, confirm=True, force_orphan=True
    ).trash_path
    trashed = sc.build_corpus_context(served)
    assert BETA not in trashed.pages and trash_path not in trashed.pages
    assert not {BETA, trash_path} & {entry.path for entry in trashed.identity_census.entries}
    assert_equal(trashed, cold(served, monkeypatch))

    sc.build_corpus_context(served)
    recover_from_trash.recover_from_trash(served, trash_path=trash_path)
    recovered = sc.build_corpus_context(served)
    assert BETA in recovered.pages
    assert_equal(recovered, cold(served, monkeypatch))


def test_a_page_in_the_reference_only_folder_owns_a_reference_and_holds_no_state(
    stored, writer, monkeypatch, corpus_cache_on  # noqa: F811
):
    """The build reads a page in this folder for its owner only, from the store: its file trails."""
    sc.build_corpus_context(stored)
    body = f"---\ntitle: Old Owner\ntype: note\nexomem_id: {OWNER_ID}\n---\n# Old Owner\n"
    commit(writer, puts=[(OWNER, body.encode())])
    owning = sc.build_corpus_context(stored)
    assert {
        entry.path: entry.page_identity
        for entry in owning.identity_census.reference_only_entries
    } == {OWNER: OWNER_ID}
    assert OWNER not in owning.pages
    assert_equal(owning, cold(stored, monkeypatch))

    sc.build_corpus_context(stored)
    commit(writer, deletes=[OWNER])
    removed = sc.build_corpus_context(stored)
    assert not removed.identity_census.reference_only_entries
    assert_equal(removed, cold(stored, monkeypatch))


def test_an_overlay_edit_rebuilds_the_context(stored, writer, monkeypatch, corpus_cache_on):  # noqa: F811
    """A stored note-type overlay shapes every page's eligibility, so its commit is no page delta."""
    commit(writer, puts=[(HARBOUR, b"---\ntitle: Tide\ntype: harbour-log\n---\n# Tide\n")])
    before = sc.build_corpus_context(stored)
    builds = Builds(monkeypatch)

    commit(writer, puts=[(OVERLAY, (
        b"schema_version: 1\nentries:\n  harbour-log:\n    label: Harbour log\n"
        b"    description: A log of the harbour.\n"
        b"    attributes:\n      role: compiled\n      folder: Notes/Harbour\n"
    ))])
    cached = sc.build_corpus_context(stored)

    assert builds.take()[0] == 1
    assert cached.pages[HARBOUR].structurally_compiled != before.pages[HARBOUR].structurally_compiled
    assert_equal(cached, cold(stored, monkeypatch))


def test_a_registry_pass_rebuilds_the_context_once_when_it_finishes(
    stored, writer, monkeypatch, corpus_cache_on  # noqa: F811
):
    """Each batch of a pass moves the store revision and changes no page that a build reads; the
    last batch records the overlay that the derived rows now reflect."""
    monkeypatch.setattr(rederive, "BATCH_PAGES", 4)
    commit_policy(writer, stored, READ_ONLY_NOTES)
    project(writer, stored)
    sc.build_corpus_context(stored)
    builds = Builds(monkeypatch)

    assert rederive.settle(stored, writer) == rederive.ADVANCED
    between = sc.build_corpus_context(stored)
    assert builds.take() == (0, 0)
    assert_equal(between, cold(stored, monkeypatch))

    sc.build_corpus_context(stored)
    builds.take()
    assert settle_to_the_end(stored, writer)[-1] == rederive.DONE
    finished = sc.build_corpus_context(stored)
    assert builds.take()[0] == 1
    assert_equal(finished, cold(stored, monkeypatch))


@pytest.mark.timeout(300)
def test_an_edit_parses_no_more_pages_in_a_vault_twice_the_size(
    served, writer, monkeypatch, corpus_cache_on  # noqa: F811
):
    """Each store-mode write rebuilt the corpus context, so the pages that the identity census
    parses for an edit grew with the vault. Measured at N pages and at 2N."""
    grow(writer, 1, 24)
    edit(served, create(served, "Warm-up"))
    builds = Builds(monkeypatch)
    parsed = {}
    for title, more in (("First probe", 0), ("Second probe", 24)):
        if more:
            grow(writer, 1000, more)
        path = create(served, title)
        builds.take()
        edit(served, path)
        parsed[page_count(served)] = builds.take()[1]

    small, large = sorted(parsed)
    assert parsed[large] == parsed[small], parsed
