"""Stored selector facts must preserve the scalar release rule without corpus-sized reads."""

from __future__ import annotations

import json
from contextlib import closing

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_graph_readers import CLAIM, EVIDENCE, PAGES, commit_linked
from test_vault_store_migration import rollback_apply, vault  # noqa: F401
from test_vault_store_read_write import put, stored, writer  # noqa: F401
from test_vault_store_readers import EXTERNAL, restrict_external
from test_vault_store_rederive import settle_to_the_end

from exomem import epistemic_graph, find, find_corpus, published_admission
from exomem.collection_store import connection
from exomem.governance import egress
from exomem.governance.principal import request_scope
from exomem.vault_store import ownership, projector, read, rederive, verify, write


def participants(root):
    with request_scope(EXTERNAL), read.request_snapshot(root):
        return epistemic_graph.EpistemicGraphIndex(root).relation_participants(
            [], anchor=CLAIM, keep=egress.edge_view(root, find.caller_admission(root)),
            resolver=lambda: find.recall_resolver_snapshot(root),
        )


def test_admission_default_batch_keeps_the_scalar_contract():
    """File predicates without a batch implementation keep their verdicts and publication proof."""
    admission = published_admission.Admission(lambda: None, lambda path, publish: (path == "open", publish))
    assert admission.decide_many(["open", "closed"], True) == {
        "open": (True, True), "closed": (False, True),
    }


def test_ordinary_hub_uses_facts_after_a_commit_and_during_a_registry_pass(stored, writer, monkeypatch):  # noqa: F811
    """A revision bump must not send ordinary endpoints back through the per-page release chain."""
    restrict_external(stored, 'tags: ["secret"]')
    write.commit(writer, put(CLAIM, PAGES[CLAIM].encode()))
    commit_linked(writer, 40)
    counts = {"scalar": [], "parse": []}
    real_decide, real_parse = egress._decide_path, find_corpus._parse_page_after_checkpoint

    def decide(root, path, **kwargs):
        counts["scalar"].append(path)
        return real_decide(root, path, **kwargs)

    def parse(path, *args, **kwargs):
        counts["parse"].append(str(path))
        return real_parse(path, *args, **kwargs)

    monkeypatch.setattr(egress, "_decide_path", decide)
    monkeypatch.setattr(find_corpus, "_parse_page_after_checkpoint", parse)
    for state in ("cold", "commit", "registry"):
        if state == "commit":
            write.commit(writer, put(EVIDENCE, PAGES[EVIDENCE].encode()))
        elif state == "registry":
            with writer.handle.transaction() as tx:
                rederive._set(tx, rederive.OVERLAY_REVISION, str(read.current_revision(stored)))
            assert rederive.settle(stored, writer) == rederive.ADVANCED
        find.clear_cache()
        counts["scalar"].clear()
        counts["parse"].clear()
        result = participants(stored)
        assert result.status == "available" and len(result.paths) >= 40
        ordinary_scalar = [path for path in counts["scalar"] if "/Notes/" in path]
        ordinary_parses = [path for path in counts["parse"] if "/Notes/" in path]
        assert not ordinary_scalar and not ordinary_parses, (state, counts)


def test_migration_commit_verification_owner_pass_and_rollback_share_facts(stored, writer, tmp_path):  # noqa: F811
    """Facts must track page bytes atomically, detect corruption, rebuild old stores and leave on rollback."""
    with read.snapshot(stored) as snapshot:
        assert snapshot.connection.execute("SELECT COUNT(*) FROM page_admission").fetchone()[0] > 0
    write.commit(writer, put(CLAIM, PAGES[CLAIM].encode()))
    moved = CLAIM.replace("Lantern Claim", "Moved")
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "move", moves=(write.PageMove(CLAIM, moved),)))
    with read.snapshot(stored) as snapshot:
        assert snapshot.connection.execute("SELECT 1 FROM page_admission WHERE path = ?", (CLAIM,)).fetchone() is None
        row = snapshot.connection.execute(
            "SELECT f.sha256, p.sha256, f.subject FROM page_admission f JOIN store_pages p USING(path) WHERE path = ?", (moved,)
        ).fetchone()
        assert row[0] == row[1] and json.loads(row[2])["path"] == moved
    assert projector.Projector(stored, run=lambda work: work(writer)).run_pass().held == 0
    outside = "Outside.md"
    (stored / outside).write_bytes(b"---\ntitle: Outside\n---\nA file the store does not own.\n")
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "outside refresh", refresh=(outside,)))
    assert writer.connection.execute("SELECT 1 FROM page_admission WHERE path = ?", (outside,)).fetchone() is None
    expected = tmp_path / "expected.sqlite"
    verify.derive_expected(stored, expected)
    assert "page_admission" in verify.compare_tables(writer.connection, expected).tables
    assert not [difference for difference in verify.compare_tables(writer.connection, expected).differences
                if difference.table == "page_admission"]
    with writer.handle.transaction() as tx:
        tx.execute("UPDATE page_admission SET subject = '{}' WHERE path = ?", (moved,))
    assert "page_admission" in {difference.table for difference in verify.compare_tables(writer.connection, expected).differences}
    with writer.handle.transaction() as tx:
        tx.execute("DROP TABLE page_admission")
        tx.execute("DELETE FROM store_meta WHERE key = ?", (rederive.OWNER_SCHEMA,))
        rederive._set(tx, rederive.PASS, json.dumps({"target": read.current_revision(stored), "after": "zzzz"}))
    settle_to_the_end(stored, writer)
    assert not [difference for difference in verify.compare_tables(writer.connection, expected).differences
                if difference.table == "page_admission"]
    # Release this fixture's writer before the offline rollback acquires its own writer.
    writer.handle.close()
    assert rollback_apply(stored, tmp_path / "export") == 0
    with closing(connection.open_reader(connection.store_path(stored))) as reader:
        assert reader.execute("SELECT 1 FROM sqlite_master WHERE name = 'page_admission'").fetchone() is None


@pytest.mark.parametrize("layout", ["stem", "title", "stem-title", "root", "all withheld", "unicode", "title suffix"])
def test_name_group_edges_equal_the_corpus_algorithm(stored, writer, monkeypatch, layout):  # noqa: F811
    """A hidden homonym must re-resolve exactly the edges the original whole-corpus algorithm re-resolves."""
    restrict_external(stored, 'tags: ["secret"]')
    target = "Beacon" if layout != "unicode" else "Cafe\u0301"
    visible = "Knowledge Base/Notes/Beacon.md"
    hidden = "Knowledge Base/Notes/Hidden/Beacon.md"
    title = target
    if layout == "title suffix":
        title, target = "Report.md", "Report.md.md"
    if layout in {"title", "unicode", "title suffix"}:
        visible, hidden = "Knowledge Base/Notes/Visible.md", "Knowledge Base/Notes/Hidden.md"
    elif layout == "stem-title":
        hidden = "Knowledge Base/Notes/Hidden.md"
    elif layout == "root":
        visible, hidden = "Knowledge Base/Notes/Beacon.md", "Notes/Beacon.md"
        target = "Notes/Beacon"
    if layout == "unicode":
        import unicodedata

        target = unicodedata.normalize("NFC", target)
    bodies = {
        CLAIM: f"---\ntitle: Claim\n---\nSee [[{target}]].\n".encode(),
        visible: f"---\ntitle: {title}\ntags: [{'secret' if layout == 'all withheld' else 'public'}]\n---\nVisible.\n".encode(),
        hidden: f"---\ntitle: {title}\ntags: [secret]\n---\nHidden.\n".encode(),
    }
    for rel, body in bodies.items():
        if ownership.classify(rel) is not ownership.Ownership.PAGE:
            (stored / rel).parent.mkdir(parents=True, exist_ok=True)
            (stored / rel).write_bytes(body)
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "homonyms", puts=tuple(
        write.PagePut(rel, body) for rel, body in bodies.items() if rel.startswith("Knowledge Base/")
    ), refresh=tuple(rel for rel in bodies if not rel.startswith("Knowledge Base/"))))
    find.clear_cache()
    actual = participants(stored)
    assert (visible in actual.paths) == (layout != "all withheld")

    def corpus_keys(view):
        resolver = view._shared_resolver()
        return epistemic_graph._dependency_changed_keys({
            path + ".md" for path in resolver.full_paths if not view.keep(path + ".md")
        }, resolver)

    monkeypatch.setattr(epistemic_graph.GraphView, "_withheld_name_keys", corpus_keys)
    find.clear_cache()
    assert participants(stored) == actual


@pytest.mark.timeout(900)
def test_small_anchor_decides_name_groups_independently_of_unrelated_corpus(stored, writer, monkeypatch):  # noqa: F811
    """A real five-link anchor must not decide thousands of uniquely named, unrelated pages."""
    from test_vault_store_graph_readers import linked_page

    restrict_external(stored, 'tags: ["secret"]')
    write.commit(writer, put(CLAIM, PAGES[CLAIM].encode()))
    scalar = egress._decide_path
    called = []

    def counted(root, path, **kwargs):
        called.append(path)
        return scalar(root, path, **kwargs)

    monkeypatch.setattr(egress, "_decide_path", counted)
    counts = []
    previous = 0
    for size in (2000, 8000):
        for start in range(previous, size, 500):
            puts = []
            for index in range(start, min(start + 500, size)):
                path, body = linked_page(index)
                if index >= 5:
                    body = body.replace(b"[[Lantern Claim]]", b"the keeper")
                puts.append(write.PagePut(path, body))
            write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "five-link drill", puts=tuple(puts)))
        previous = size
        find.clear_cache()
        called.clear()
        result = participants(stored)
        assert result.status == "available" and len(result.paths) >= 5
        counts.append(len(called))
    import os

    if report := os.environ.get("EXOMEM_ADMISSION_GROWTH_REPORT"):
        from pathlib import Path

        Path(report).write_text(json.dumps({"pages": [2000, 8000], "links": 5, "scalar_decisions": counts}))
    assert counts[0] == counts[1], counts


@pytest.mark.parametrize("damage", ["missing", "version", "hash", "exception", "subject"])
def test_unproven_facts_take_the_scalar_chain(stored, writer, monkeypatch, damage):  # noqa: F811
    """An incomplete upgrade or damaged facts row must preserve the scalar withholding decision."""
    restrict_external(stored, 'tags: ["secret"]')
    write.commit(writer, put(CLAIM, PAGES[CLAIM].encode().replace(b"type: insight", b"type: insight\ntags: [secret]")))
    with writer.handle.transaction() as tx:
        if damage == "missing":
            tx.execute("DELETE FROM page_admission WHERE path = ?", (CLAIM,))
        else:
            column, value = {
                "version": ("facts_version", -1), "hash": ("sha256", "old"),
                "exception": ("simple", 0), "subject": ("subject", "{}"),
            }[damage]
            tx.execute(f"UPDATE page_admission SET {column} = ? WHERE path = ?", (value, CLAIM))
    scalar = egress._decide_path
    decided = []

    def counted(root, path, **kwargs):
        decided.append(path)
        return scalar(root, path, **kwargs)

    monkeypatch.setattr(egress, "_decide_path", counted)
    with request_scope(EXTERNAL), read.request_snapshot(stored):
        keep = egress.edge_view(stored, egress.content_admission(stored, EXTERNAL))
        offered = published_admission.admission_of(keep)
        assert offered.decide_many([CLAIM])[CLAIM][0] is False
        assert CLAIM in decided


def test_a_request_never_uses_facts_from_a_newer_commit(stored, writer):  # noqa: F811
    """A concurrent retag must neither widen the next request nor mix newer facts into an older snapshot."""
    restrict_external(stored, 'tags: ["secret"]')
    write.commit(writer, put(CLAIM, PAGES[CLAIM].encode()))
    with request_scope(EXTERNAL):
        with read.request_snapshot(stored):
            keep = egress.edge_view(stored, egress.content_admission(stored, EXTERNAL))
            offered = published_admission.admission_of(keep)
            assert offered.decide_many([CLAIM])[CLAIM][0]
            write.commit(writer, put(CLAIM, PAGES[CLAIM].encode().replace(b"type: insight", b"type: insight\ntags: [secret]")))
            assert offered.decide_many([CLAIM])[CLAIM][0]
        with read.request_snapshot(stored):
            keep = egress.edge_view(stored, egress.content_admission(stored, EXTERNAL))
            assert not published_admission.admission_of(keep).decide_many([CLAIM])[CLAIM][0]


def test_normalized_subject_has_a_versioned_stable_format(tmp_path):
    """Changing facts without changing their version must not let old stores supply a new kernel's inputs."""
    from dataclasses import asdict

    from exomem.find_types import ParsedPage
    from exomem.governance import membership, page_admission

    page = ParsedPage(
        path=tmp_path / "Example.md", rel_path="Knowledge Base/Notes/Example.md", body="", title="", mtime=0,
        frontmatter={"exomem_id": "12345678-1234-4abc-8def-123456789abc", "type": "Note",
                     "project": "Coast", "projects": ["Harbour"], "tags": ["Sea"], "classes": ["Internal"]},
    )
    assert page_admission.FACTS_VERSION == 1
    assert asdict(membership.markdown_subject(page)) == {
        "path": "Knowledge Base/Notes/Example.md",
        "refs": ("exomem://memory/12345678-1234-4abc-8def-123456789abc",),
        "projects": ("coast", "harbour"), "tags": ("sea",), "types": ("note",), "classes": ("internal",),
        "frontmatter_valid": True, "identity_valid": True,
    }


def test_bridges_tombstones_and_unnameable_callers_keep_scalar_answers(stored, writer, monkeypatch):  # noqa: F811
    """Neither a bridge-shaped page, an erased path nor a live authorization session can borrow simple-page authority."""
    from dataclasses import replace

    from exomem.governance import lifecycle

    restrict_external(stored, 'tags: ["secret"]')
    bridge = "Knowledge Base/Notes/Bridge.md"
    flow_bridge = "Knowledge Base/Notes/Flow.md"
    invalid = "Knowledge Base/Notes/Invalid.md"
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "exception parity", puts=(
        write.PagePut(CLAIM, PAGES[CLAIM].encode() + b"\nSee [[Flow]].\n"),
        write.PagePut(bridge, b"---\ntitle: Bridge\nbridge_of: []\nbridge_scope: navigation\n---\nDraft abstraction.\n"),
        write.PagePut(flow_bridge, b"---\n{title: Flow, type: insight, bridge_of: []}\n---\nDraft abstraction.\n"),
        write.PagePut(invalid, b"---\nexomem_id: invalid\ntags: [secret]\n---\nMalformed identity.\n"),
    )))
    for who in (EXTERNAL, replace(EXTERNAL, authorization_session_id="unnameable")):
        with request_scope(who), read.request_snapshot(stored):
            keep = egress.edge_view(stored, egress.content_admission(stored, who))
            offered = published_admission.admission_of(keep)
            if who.authorization_session_id:
                assert offered.identity() is None
            expected = {path: keep(path) for path in (CLAIM, bridge, flow_bridge, invalid)}
            actual = offered.decide_many(expected)
            assert {path: verdict[0] for path, verdict in actual.items()} == expected
            assert not actual[bridge][0] and not actual[flow_bridge][0] and not actual[invalid][0]
    assert flow_bridge not in participants(stored).paths
    monkeypatch.setattr(lifecycle, "tombstoned_paths", lambda root: frozenset({CLAIM}))
    with request_scope(EXTERNAL), read.request_snapshot(stored):
        keep = egress.edge_view(stored, egress.content_admission(stored, EXTERNAL))
        assert not keep(CLAIM)
        assert not published_admission.admission_of(keep).decide_many([CLAIM])[CLAIM][0]


def test_one_batch_does_not_query_each_ordinary_page(stored, writer, monkeypatch):  # noqa: F811
    """Growing a candidate batch within one SQL key chunk must not add one lookup per page."""
    from contextlib import contextmanager

    restrict_external(stored, 'tags: ["secret"]')
    paths = [f"Knowledge Base/Notes/Batch-{index}.md" for index in range(16)]
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "query count", puts=tuple(
        write.PagePut(path, b"---\ntitle: Batch\ntype: note\n---\nOrdinary page.\n") for path in paths
    )))
    reader = connection.store_reader
    statements = []

    @contextmanager
    def traced(*args, **kwargs):
        with reader(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    with request_scope(EXTERNAL), read.request_snapshot(stored):
        keep = egress.edge_view(stored, egress.content_admission(stored, EXTERNAL))
        assert published_admission.admission_of(keep).decide_many(paths)
    monkeypatch.setattr(connection, "store_reader", traced)
    counts = []
    for size in (4, 16):
        statements.clear()
        with request_scope(EXTERNAL), read.request_snapshot(stored):
            keep = egress.edge_view(stored, egress.content_admission(stored, EXTERNAL))
            assert all(verdict[0] for verdict in published_admission.admission_of(keep).decide_many(paths[:size]).values())
        counts.append(sum(sql.lstrip().upper().startswith(("SELECT", "WITH")) for sql in statements))
    assert counts[0] > 0 and counts[0] == counts[1], counts


def test_a_facts_only_upgrade_keeps_complete_inbound_rows_available(stored, writer, monkeypatch):  # noqa: F811
    """Backfilling a new owner must not disable ready link reads or the inbound checks of moves and deletes."""
    from test_vault_store_graph_readers import inbound

    from exomem.vault_store import targets

    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "ready links", puts=(
        write.PagePut(CLAIM, PAGES[CLAIM].encode()), write.PagePut(EVIDENCE, PAGES[EVIDENCE].encode()),
    )))
    expected = inbound(stored, CLAIM), targets.inbound_links(stored, [CLAIM])
    assert expected[0][0] >= 1
    with writer.handle.transaction() as tx:
        tx.execute("DROP TABLE page_admission")
        rederive._set(tx, rederive.OWNER_SCHEMA, "1")
    assert rederive.read(writer.connection).owner_schema_behind
    assert (inbound(stored, CLAIM), targets.inbound_links(stored, [CLAIM])) == expected
    monkeypatch.setattr(rederive, "BATCH_PAGES", 1)
    assert rederive.settle(stored, writer) == rederive.ADVANCED
    assert rederive.read(writer.connection).owner_schema_behind
    assert (inbound(stored, CLAIM), targets.inbound_links(stored, [CLAIM])) == expected
