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

import json
import os
import sqlite3
import threading
from collections import Counter
from contextlib import closing, contextmanager

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, BETA, vault  # noqa: F401
from test_vault_store_readers import EXTERNAL, restrict_external

from exomem import commands, entity_recurrence, semantic_contract
from exomem import vault as vault_module
from exomem.collection_store import connection
from exomem.collection_store import governance as store_governance
from exomem.collection_store.writer import CollectionWriter
from exomem.governance import egress, policy, receipts
from exomem.governance.principal import owner_principal, request_scope
from exomem.vault_store import read, rederive, write

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


def emitted_receipts(root, monkeypatch, who, pages, *, before):
    """The disclosure payloads that a command's boundary emits for ``who``, who decides ``pages`` one at
    a time, then as a set, and builds the corpus context in it. ``before`` is what a write did before its
    sets were decided by scope class and its corpus boundary kept no receipt."""
    sent = []
    with monkeypatch.context() as patch:
        patch.setattr(
            receipts, "append_event",
            lambda _root, **event: sent.append((event["event_type"], json.dumps(event["payload"], sort_keys=True))),
        )
        if before:
            boundary = egress.disclosure_boundary

            @contextmanager
            def recording(vault_root, command_name, **options):
                with boundary(vault_root, command_name, **{**options, "sink": False}) as collector:
                    yield collector

            patch.setattr(egress, "disclosure_boundary", recording)
            patch.setattr(egress, "_bound_operation_decides_as", lambda *args: False)
        with request_scope(who), egress.disclosure_boundary(root, "remember") as collector:
            keep = egress.restricted_release_filter(root)
            assert keep is not None
            keep(pages[0])
            egress.admitted(keep, pages)
            semantic_contract.build_corpus_context_with_census(root)
            egress.emit_boundary_receipt(collector)
    return sent


def assert_receipts_unchanged(root, monkeypatch, who, pages):
    after = emitted_receipts(root, monkeypatch, who, pages, before=False)
    before = emitted_receipts(root, monkeypatch, who, pages, before=True)
    assert before and all(kind == "disclosure" for kind, _payload in before), before
    assert after == before
    return {outcome["decision"] for _kind, payload in before for outcome in json.loads(payload)["outcomes"]}


@pytest.mark.timeout(300)
def test_a_reads_emitted_receipt_is_the_one_it_emitted_before_the_corpus_boundary_kept_none(
    vault, monkeypatch  # noqa: F811
):
    """Defect that this guards: a corpus boundary that records nothing must leave the receipt of the command
    around it as it was, with the same outcomes (hashes, sizes, levels, scopes) in the same order. A restricted
    caller reads in file mode, where a page is released or withheld."""
    secret = "Knowledge Base/Notes/Secret.md"
    (vault / secret).write_text("---\ntitle: Secret\ntags: [secret]\n---\nWithheld from the external audience.\n")
    restrict_external(vault, "tags: [secret]")
    decisions = assert_receipts_unchanged(vault, monkeypatch, EXTERNAL, [ALPHA, BETA, secret])
    assert decisions == {"released", "withheld"}


@pytest.mark.timeout(300)
def test_a_writes_emitted_receipt_is_the_one_it_emitted_before_sets_took_the_class_route(
    served, writer, monkeypatch  # noqa: F811
):
    """Defect that this guards: a set that a store-mode write decides by scope class, and a corpus boundary
    that records nothing, must leave the receipt of the write's own command as it was, outcome for outcome."""
    grow(writer, 1, 8, on_disk=served)
    restrict_external(served, "tags: [secret]")
    assert_receipts_unchanged(
        served, monkeypatch, owner_principal(surface="library"),
        ["Knowledge Base/Notes/Note 2.md", "Knowledge Base/Notes/Note 3.md", "Knowledge Base/Entities/People/Person 4.md", ALPHA],
    )


class DeriveCounts:
    """How often a save enriches a page, derives a whole corpus and builds a link resolver.

    ``resolver_builds`` counts the resolvers built from the disk or the store (``WikilinkResolver.__init__``);
    ``large_resolvers`` counts those built from more than ``LARGE`` entries (``from_entries``), the
    whole-corpus ones: a resolver over a page or two is as cheap as a dict.
    """

    LARGE = 64
    NAMES = ("enrichments", "whole_derives", "resolver_builds", "large_resolvers")

    def __init__(self, monkeypatch):
        self.seen = Counter()
        for name, owner, attribute in (
            ("enrichments", semantic_contract, "enrich_page_state"),
            ("whole_derives", semantic_contract, "_context_from_state_map"),
            ("resolver_builds", vault_module.WikilinkResolver, "__init__"),
        ):
            function = getattr(owner, attribute)

            def counting(*args, _name=name, _function=function, **kwargs):
                self.seen[_name] += 1
                return _function(*args, **kwargs)

            monkeypatch.setattr(owner, attribute, counting)
        from_entries = vault_module.WikilinkResolver.from_entries

        def counted_from_entries(vault_root, entries):
            entries = tuple(entries)
            self.seen["large_resolvers"] += len(entries) > self.LARGE
            return from_entries(vault_root, entries)

        monkeypatch.setattr(vault_module.WikilinkResolver, "from_entries", counted_from_entries)

    def take(self):
        taken = {name: self.seen[name] for name in self.NAMES}
        self.seen.clear()
        return taken


@pytest.mark.timeout(300)
def test_a_store_writes_corpus_derivation_does_not_grow_with_the_pages(
    served, writer, corpus_cache_on, monkeypatch  # noqa: F811
):
    """Defect that this guards: a save that enriches every page of the vault, or derives the whole corpus
    again for the page it adds, costs seconds at 20,000 pages. A create and an edit by the owner are
    measured at N and 2N pages, each page also a file, with a read by a restricted caller before each
    save, under a policy whose selector reads frontmatter so that the caller cannot admit the vault's
    type overlay as the owner can. Enrichments stay with the pages the save changes, and no whole
    corpus is derived, whatever the page count and whoever read in between. A restricted caller that
    overwrote the owner's enrichment would make every save after its read enrich every page. The corpus
    cache is on, as in a served vault: the suite defaults it off.

    The restricted caller's own read and a restricted caller's save are not measured. The resolvers that a
    save builds over the whole vault are held at zero by the test after this one.
    """
    grow(writer, 1, 70, on_disk=served)
    restrict_external(served, "tags: [secret]")

    def restricted_read():
        with request_scope(EXTERNAL):
            semantic_contract.build_corpus_context_with_census(served)

    edit(served, create(served, "Warm-up"))
    counts = DeriveCounts(monkeypatch)
    measured = []
    for title, more in (("First probe", 0), ("Second probe", 70)):
        if more:
            grow(writer, 1000, more, on_disk=served)
            # The pages just committed are new to the corpus: the next save enriches them, and no more.
            edit(served, create(served, "Catch-up"))
        pages = page_count(served)
        restricted_read()
        counts.take()
        path = create(served, title)
        created = counts.take()
        restricted_read()
        counts.take()
        edit(served, path)
        edited = counts.take()
        measured.append((pages, created, edited))
    (pages_n, create_n, edit_n), (pages_2n, create_2n, edit_2n) = measured
    grown = pages_2n - pages_n
    report = json.dumps(measured)
    assert pages_n > counts.LARGE and grown >= 70, report
    for at_n, at_2n in ((create_n, create_2n), (edit_n, edit_2n)):
        # The pages a save changes are enriched; a page of the vault is not.
        assert at_2n["enrichments"] - at_n["enrichments"] <= 2, report
        # The page that a save adds or changes joins the corpus by the page delta, never a whole derivation.
        assert at_n["whole_derives"] == at_2n["whole_derives"] == 0, report
        # No resolver is built over the corpus's entries for a judged page, nor by the store commit.
        assert at_n["large_resolvers"] == at_2n["large_resolvers"] == 0, report


class SaveCounts:
    """The per-page work that a save may not do: a walk of the vault for the corpus census, a resolver built
    over the whole vault, a frontmatter parse or a stored body read for a page that the save did not change."""

    LARGE = 64
    NAMES = (
        "census_walks", "stored_page_censuses", "resolver_builds", "large_resolvers",
        "frontmatter_parses", "body_reads",
    )

    def __init__(self, monkeypatch):
        self.seen = Counter()
        for name, owner, attribute in (
            ("census_walks", semantic_contract, "_corpus_census"),
            ("stored_page_censuses", semantic_contract, "_stored_page_census"),
            ("resolver_builds", vault_module.WikilinkResolver, "__init__"),
            ("frontmatter_parses", vault_module, "parse_frontmatter"),
        ):
            function = getattr(owner, attribute)

            def counting(*args, _name=name, _function=function, **kwargs):
                self.seen[_name] += 1
                return _function(*args, **kwargs)

            monkeypatch.setattr(owner, attribute, counting)
        page, bodies, sources = read.StoreSnapshot.page, read.StoreSnapshot.bodies, read.StoreSnapshot.page_sources

        def counted_page(view, rel):
            found = page(view, rel)
            # A page that the store holds is a body read; a miss reads none.
            self.seen["body_reads"] += found is not None
            return found

        def counted_bodies(view, rels):
            found = bodies(view, rels)
            self.seen["body_reads"] += len(found)
            return found

        def counted_sources(view):
            for found in sources(view):
                self.seen["body_reads"] += 1
                yield found

        monkeypatch.setattr(read.StoreSnapshot, "page", counted_page)
        monkeypatch.setattr(read.StoreSnapshot, "bodies", counted_bodies)
        monkeypatch.setattr(read.StoreSnapshot, "page_sources", counted_sources)
        from_entries = vault_module.WikilinkResolver.from_entries

        def counted_from_entries(vault_root, entries):
            entries = tuple(entries)
            self.seen["large_resolvers"] += len(entries) > self.LARGE
            return from_entries(vault_root, entries)

        monkeypatch.setattr(vault_module.WikilinkResolver, "from_entries", staticmethod(counted_from_entries))

    def take(self):
        taken = {name: self.seen[name] for name in self.NAMES}
        self.seen.clear()
        return taken


@pytest.mark.timeout(300)
def test_a_store_save_after_a_save_walks_no_page_and_builds_no_resolver_over_the_vault(
    served, writer, corpus_cache_on, monkeypatch  # noqa: F811
):
    """Defect that this guards: each store commit evicts the structural corpus, so the next save walks and
    reconciles every page again and builds a resolver over every page, twice. A create and an edit by the
    owner, after a save that warmed the corpus, are measured at N and 2N pages: the next save is an event
    hit that walks no vault and builds no resolver, and the pages that it parses or reads from the store are
    the ones that it changes, whatever the page count."""
    grow(writer, 1, 70, on_disk=served)
    edit(served, create(served, "Warm-up"))
    counts = SaveCounts(monkeypatch)
    measured = []
    for title, more in (("First probe", 0), ("Second probe", 70)):
        if more:
            grow(writer, 1000, more, on_disk=served)
            # A bulk commit leaves the corpus to the next save's walk; that save is not measured.
            edit(served, create(served, "Catch-up"))
        pages = page_count(served)
        counts.take()
        path = create(served, title)
        created = counts.take()
        edit(served, path)
        edited = counts.take()
        measured.append((pages, created, edited))
    (pages_n, create_n, edit_n), (pages_2n, create_2n, edit_2n) = measured
    assert pages_n > SaveCounts.LARGE and pages_2n - pages_n >= 70, json.dumps(measured)
    broken = []
    for write_name, at_n, at_2n in (("create", create_n, create_2n), ("edit", edit_n, edit_2n)):
        for name in ("census_walks", "stored_page_censuses", "resolver_builds", "large_resolvers"):
            if at_n[name] or at_2n[name]:
                broken.append((write_name, name, at_n[name], at_2n[name]))
        # The pages that a save changes are parsed and read; a page of the vault is not.
        for name in ("frontmatter_parses", "body_reads"):
            if at_2n[name] - at_n[name] > 2:
                broken.append((write_name, name, at_n[name], at_2n[name]))
    assert not broken, f"(save, count, at N={pages_n} pages, at 2N={pages_2n}): {broken}"
