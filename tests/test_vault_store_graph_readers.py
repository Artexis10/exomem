"""What a store-mode vault's graph and link readers answer (OpenSpec make-vault-store-canonical,
tasks 3.1 and 3.2, the graph half).

The vault is the migration suite's vault with typed relations, moved into store mode by the real
migrate route. Its file-built graph sidecar is then replaced by a sentinel and every projected page
is poisoned, so a reader that still takes either fails or serves the poison.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import (  # noqa: F401
    build_indexes,
    migrate_apply,
    rollback_apply,
    vault,
)
from test_vault_store_read_write import put, stored, writer  # noqa: F401
from test_vault_store_readers import (
    EXTERNAL,
    SENTINEL,
    damage_every_owned_projection,
    restrict_external,
)
from test_vault_store_rederive import PROFILES, project, settle_to_the_end

from exomem import commands, epistemic_graph, find, graph_read_publication, lexstore
from exomem import vault as vaults
from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.governance.principal import request_scope
from exomem.vault_store import mode, ownership, read, rederive, write

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

INSIGHTS = "Knowledge Base/Notes/Insights"
CLAIM = f"{INSIGHTS}/Lantern Claim.md"
EVIDENCE = f"{INSIGHTS}/Tide Evidence.md"
RIVAL = f"{INSIGHTS}/Rival.md"
TIDE = "Knowledge Base/Notes/Tide.md"
TIDE_TWIN = "Knowledge Base/Entities/Tide.md"
SECRET = "Knowledge Base/Notes/Secret Log.md"
OUTSIDE = "Harbour Log.md"
PAGES = {
    CLAIM: (
        "---\ntitle: Lantern Claim\ntype: insight\n---\n# Lantern Claim\n\n"
        "The lighthouse lantern burns all night.\n\n## Relations\n\n"
        f"- supports [[{EVIDENCE[:-3]}]]\n- contradicts [[Rival Theory]]\n\n"
        "The keeper checks [[Tide]] against [[Notes/Alpha]], and [[Notes/Alpha]] again.\n"
    ),
    EVIDENCE: (
        "---\ntitle: Tide Evidence\ntype: insight\n---\n# Tide Evidence\n\n"
        "The lighthouse tide log that [[Lantern Claim]] rests on.\n"
    ),
    RIVAL: "---\ntitle: Rival Theory\ntype: insight\n---\n# Rival\n\nThe lighthouse is dark.\n",
    TIDE: "---\ntitle: Tide\ntype: note\n---\n# Tide\n\nThe lighthouse tide.\n",
    TIDE_TWIN: "---\ntitle: Tide entity\ntype: note\n---\n# Tide\n\nA homonym.\n",
    SECRET: (
        "---\ntitle: Secret Log\ntype: note\ntags: [secret]\n---\n# Secret Log\n\n"
        "The lighthouse log names [[Lantern Claim]].\n\n## Relations\n\n"
        f"- supports [[{CLAIM[:-3]}]]\n"
    ),
    OUTSIDE: f"# Harbour Log\n\nThe harbour log cites [[{CLAIM[:-3]}]].\n",
}


def write_pages(root):
    for rel, text in PAGES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def inbound(root, target):
    answer = commands.op_list_inbound_links(root, target=target)
    rows = sorted(
        (row["path"], row["line_number"], row["context"], row["raw_target"])
        for row in answer["inbound"]
    )
    return answer["count"], rows


def graph_answers(root):
    """What each graph and link reader answers, as plain data. An inbound list is compared in
    (path, line) order: file mode lists it in the order that the filesystem walks the folders."""
    asked = commands.op_ask_memory(root, query="lighthouse lantern", limit=15)
    packed = commands.op_find(root, query="lighthouse lantern", limit=15, pack=True)
    return {
        "ask": asked["hits"],
        "pack": packed.get("pack"),
        "context": commands.op_graph_context(root, path=CLAIM),
        "context depth 2": commands.op_graph_context(root, path=CLAIM, depth=2),
        "context supports": commands.op_graph_context(
            root, path=CLAIM, relation_types=["supports"]
        ),
        "supports": [
            hit.path for hit in find.find(root, query="", relations=["supports"], limit=15)
        ],
        "relation of": [
            hit.path for hit in find.find(root, query="", relation_of=CLAIM, limit=15)
        ],
        "inbound": {rel: inbound(root, rel) for rel in (*PAGES, "Knowledge Base/Notes/Alpha.md")},
    }


def answers_for_both_callers(root):
    owner = graph_answers(root)
    with request_scope(EXTERNAL):
        external = graph_answers(root)
    return {"owner": owner, "external": external}


@pytest.mark.timeout(500)
def test_each_graph_reader_answers_from_the_store_as_it_answers_in_file_mode(
    vault, monkeypatch  # noqa: F811
):
    """A graph or link reader that still takes the file-built sidecar, the walk of the files or a
    projected page: it refuses, misses a page that only the store holds or serves the poison, where
    file mode answers the same bytes. Or a store branch that re-proves the graph, opens a second
    snapshot, drops the restricted caller's filter, or stats a stored page's file per read."""
    restrict_external(vault, 'tags: ["secret"]')
    write_pages(vault)
    build_indexes(vault)
    find.clear_cache()
    in_file_mode = answers_for_both_callers(vault)
    owner, external = in_file_mode["owner"], in_file_mode["external"]
    assert owner["supports"] and owner["context"]["edges"] and owner["ask"]
    assert {row[0] for row in owner["inbound"][CLAIM][1]} == {EVIDENCE, SECRET, OUTSIDE}
    assert SECRET not in {row[0] for row in external["inbound"][CLAIM][1]}
    assert migrate_apply(vault) == 0

    damage_every_owned_projection(vault, "poisoned")
    sidecar = epistemic_graph.sidecar_path(vault)
    sidecar.unlink()
    sidecar.write_bytes(SENTINEL)  # a read of the file-built graph fails
    vaults.clear_inbound_index()
    find.clear_cache()
    machinery, stats, opened = [], [], []
    for owner_module, name in (
        (epistemic_graph, "graph_lag"),
        (epistemic_graph, "schedule_background_rebuild"),
        (graph_read_publication, "published"),
        (vaults, "_build_inbound_index"),
        (find, "schedule_inbound_rebuild"),
        (lexstore, "_schedule_repair"),
    ):
        monkeypatch.setattr(
            owner_module, name, lambda *a, _name=name, **k: machinery.append(_name)
        )

    def watching(real):
        def watched(path, *arguments, **keywords):
            try:
                rel = Path(path).relative_to(vault).as_posix()
            except (TypeError, ValueError):
                rel = None  # a descriptor, or a path outside the vault
            if rel is not None and ownership.classify(rel) is ownership.Ownership.PAGE:
                stats.append(rel)
            return real(path, *arguments, **keywords)

        return watched

    real_reader = connection.store_reader

    def counting(*arguments, **keywords):
        opened.append(1)
        return real_reader(*arguments, **keywords)

    monkeypatch.setattr(os, "stat", watching(os.stat))
    monkeypatch.setattr(os, "lstat", watching(os.lstat))
    monkeypatch.setattr(connection, "store_reader", counting)
    in_store_mode = answers_for_both_callers(vault)

    assert in_store_mode == in_file_mode
    assert SENTINEL.decode().strip() not in json.dumps(in_store_mode, default=str)
    assert machinery == []
    assert stats == []
    opened.clear()
    commands.op_graph_context(vault, path=CLAIM, depth=2)
    commands.op_list_inbound_links(vault, target=CLAIM)
    assert len(opened) == 2, "one snapshot for each tool call"


def test_a_graph_read_borrows_the_request_snapshot_and_hands_it_back_unchanged(
    stored, writer  # noqa: F811
):
    """A graph reader that opens its own snapshot reads a later revision than the request's search
    rows; one whose ``close`` or ``rollback`` ends the request's snapshot makes the next read of the
    request see another revision; a nested read whose ``close`` clears the outer reader's deadline;
    and a deadline handler or SQL function left on a pooled connection aborts or haunts a later
    request's reads."""
    write.commit(writer, put(CLAIM, PAGES[CLAIM].encode()))
    index = epistemic_graph.EpistemicGraphIndex(stored)
    with read.request_snapshot(stored):
        before = read.current_revision(stored)
        outer = index._open_read_snapshot()
        assert outer.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE path = ?", (CLAIM,)
        ).fetchone()[0] >= 1
        outer.create_function("exomem_probe", 0, lambda: 1)
        outer.set_progress_handler(lambda: 1, 1)  # a deadline that has passed
        inner = index._open_read_snapshot()
        inner.close()
        with pytest.raises(sqlite3.OperationalError):
            outer.execute("SELECT exomem_probe() FROM graph_nodes").fetchall()
        with pytest.raises(sqlite3.ProgrammingError):
            outer.rollback()
        with pytest.raises(sqlite3.ProgrammingError):
            outer.commit()
        outer.close()
        write.commit(writer, put(EVIDENCE, PAGES[EVIDENCE].encode()))
        with read.snapshot(stored) as view:
            assert view.revision == before
            assert view.connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()
            # SQLite keeps the name, but the reader's function and what it holds are gone.
            with pytest.raises(sqlite3.OperationalError, match="user-defined function raised"):
                view.connection.execute("SELECT exomem_probe()")
    assert read.current_revision(stored) == before + 1


@pytest.mark.timeout(300)
def test_a_graph_and_link_read_serves_and_says_so_while_a_schema_change_is_applied(
    stored, writer  # noqa: F811
):
    """A graph reader that refuses as warming for a pass that ends in minutes, or one that serves
    rows derived under the previous schema without saying so; and a restricted caller that is
    shown the revisions, which count the vault's writes."""
    for rel in (CLAIM, EVIDENCE, RIVAL):
        write.commit(writer, put(rel, PAGES[rel].encode()))
    write.commit(writer, put(PROFILES, b"profiles: {}\n"))
    find.clear_cache()

    context = commands.op_graph_context(stored, path=CLAIM)
    links = commands.op_list_inbound_links(stored, target=EVIDENCE)
    asked = commands.op_ask_memory(stored, query="lighthouse", limit=15)
    with request_scope(EXTERNAL):
        restricted = commands.op_graph_context(stored, path=CLAIM)

    assert context["available"] and context["edges"] and links["count"] == 1
    assert context["lag"]["registry"] == links["lag"]["registry"] == "rebuilding"
    assert context["notice"] == links["notice"] == asked["notice"]
    assert restricted["notice"] == context["notice"] and "lag" not in restricted

    project(writer, stored)
    settle_to_the_end(stored, writer)

    settled = commands.op_graph_context(stored, path=CLAIM)
    assert "notice" not in settled and "lag" not in settled


def drop_link_tables(root):
    """Return the store to what an older build left: no link tables and no owner version."""
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        with handle.transaction() as tx:
            for table in ("inbound_links", "inbound_sources"):
                tx.execute(f"DROP TABLE {table}")
            tx.execute(
                "DELETE FROM store_mode_objects WHERE name LIKE 'inbound%'"
            )
            tx.execute("DELETE FROM store_meta WHERE key = ?", (rederive.OWNER_SCHEMA,))


def link_rows(root):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return {
            table: sorted(reader.execute(f"SELECT * FROM {table}").fetchall())
            for table in ("inbound_links", "inbound_sources")
        }, sorted(
            name for name, in reader.execute(
                "SELECT name FROM store_mode_objects WHERE name LIKE 'inbound%'"
            )
        )


@pytest.mark.timeout(500)
def test_a_store_from_an_older_build_refuses_link_reads_until_its_pass_derives_them(
    vault, tmp_path  # noqa: F811
):
    """A store that an older build set up has no link tables: a reader that answers "no inbound
    links" from it tells an agent that a page is safe to delete; an upgrade that leaves its tables
    out of the rollback record keeps them after a rollback; one that never fills them serves an
    empty list as complete."""
    write_pages(vault)
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    derived = link_rows(vault)
    drop_link_tables(vault)

    figures = mode.status(vault)
    assert (figures["registry"], figures["owner_schema"]) == ("unknown", "behind")
    with pytest.raises(OpError) as refused:
        commands.op_list_inbound_links(vault, target=CLAIM)
    assert refused.value.code == read.DERIVED_BEHIND

    path = connection.store_path(vault)
    with connection.open_writer(path, lease_check=lambda: True) as handle:
        from exomem.collection_store.writer import CollectionWriter

        upgrading = CollectionWriter(vault, handle)
        write.commit(upgrading, put(RIVAL, PAGES[RIVAL].encode()))  # a commit before the pass
        project(upgrading, vault)
        settle_to_the_end(vault, upgrading)

    assert link_rows(vault) == derived
    figures = mode.status(vault)
    assert (figures["registry"], figures["owner_schema"]) == ("current", "current")
    assert inbound(vault, CLAIM)[0] == 4  # the secret log links twice

    assert rollback_apply(vault, tmp_path / "export") == 0
    with closing(connection.open_reader(path)) as reader:
        assert reader.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'inbound%'"
        ).fetchall() == []
        assert reader.execute(
            "SELECT 1 FROM store_meta WHERE key = ?", (rederive.OWNER_SCHEMA,)
        ).fetchall() == []


#: When set, the plan test writes each graph and link statement's ``EXPLAIN QUERY PLAN`` here: the
#: plans that tasks.md 3.2 records, run with ``EXOMEM_READERS_DRILL_PAGES`` at scale.
PLAN_REPORT = os.environ.get("EXOMEM_GRAPH_PLAN_REPORT")
GENERATED = int(os.environ.get("EXOMEM_READERS_DRILL_PAGES", "40"))


def linked_page(index):
    """A generated note that links to the claim and to its neighbour, as a vault's notes link."""
    return (
        f"Knowledge Base/Notes/Gen/Page-{index:04d}.md",
        (
            f"---\ntitle: Page {index}\ntype: note\n---\n# Page {index}\n\n"
            f"The lantern number {index} burns at the lighthouse, as [[Lantern Claim]] says; "
            f"see [[Page-{(index + 1) % max(GENERATED, 1):04d}]].\n"
        ).encode(),
    )


def commit_linked(writer, pages):  # noqa: F811
    for start in range(0, pages, 200):
        puts = tuple(
            write.PagePut(*linked_page(index)) for index in range(start, min(pages, start + 200))
        )
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "drill", puts=puts))


#: The graph and link calls that the plan test reads through and the drill times, at the tool entry.
GRAPH_CALLS = {
    "ask_memory": lambda root: commands.op_ask_memory(root, query="lighthouse lantern", limit=15),
    "graph_context depth 2": lambda root: commands.op_graph_context(root, path=CLAIM, depth=2),
    "find supports": lambda root: find.find(root, query="", relations=["supports"], limit=15),
    "find relation_of": lambda root: find.find(
        root, query="lantern", relation_of=CLAIM, limit=15
    ),
    "list_inbound_links, every page": lambda root: commands.op_list_inbound_links(
        root, target=CLAIM
    ),
    "list_inbound_links, one link": lambda root: commands.op_list_inbound_links(
        root, target=linked_page(1)[0]
    ),
}
#: When set, the plan test writes each call's median cold and warm milliseconds here.
DRILL_REPORT = os.environ.get("EXOMEM_GRAPH_DRILL_REPORT")


def graph_and_link_reads(root):
    answers = {name: call(root) for name, call in GRAPH_CALLS.items()}
    assert answers["ask_memory"]["hits"] and answers["graph_context depth 2"]["edges"]
    assert answers["find supports"] and answers["find relation_of"]
    assert answers["list_inbound_links, every page"]["count"] >= GENERATED
    assert answers["list_inbound_links, one link"]["count"] == 1


def drill_figures(root):
    import statistics
    import time

    figures = {}
    for name, call in GRAPH_CALLS.items():
        cold, warm = [], []
        for _ in range(3):
            find.clear_cache()
            for times in (cold, warm):
                started = time.perf_counter()
                call(root)
                times.append((time.perf_counter() - started) * 1000)
        figures[name] = {
            "cold_ms": round(statistics.median(cold)),
            "warm_ms": round(statistics.median(warm)),
        }
    return figures


@pytest.mark.timeout(1800)
def test_each_graph_and_link_statement_finds_its_rows_through_an_index(
    stored, writer, monkeypatch  # noqa: F811
):
    """A dropped index, or a graph or link reader that adds a predicate no index covers, makes a
    10,000-page vault answer in seconds and fails no other test: each answer is still correct."""
    for rel in (CLAIM, EVIDENCE, RIVAL):
        write.commit(writer, put(rel, PAGES[rel].encode()))
    commit_linked(writer, GENERATED)
    find.clear_cache()
    graph_and_link_reads(stored)  # the admitted sets are read once per revision, as file mode does
    if DRILL_REPORT:
        Path(DRILL_REPORT).write_text(json.dumps({"pages": GENERATED, **drill_figures(stored)}))
    find.reset_page_and_result_caches()
    statements = []
    real = connection.store_reader

    @contextlib.contextmanager
    def tracing(*arguments, **keywords):
        with real(*arguments, **keywords) as reader:
            reader.set_trace_callback(statements.append)
            try:
                yield reader
            finally:
                reader.set_trace_callback(None)

    with monkeypatch.context() as patch:
        patch.setattr(connection, "store_reader", tracing)
        graph_and_link_reads(stored)

    owned = ("graph_", "inbound_")
    selects = sorted({
        sql for sql in statements
        if sql.lstrip().upper().startswith(("SELECT", "WITH")) and any(t in sql for t in owned)
    })
    assert any("inbound_links" in sql for sql in selects) and any("graph_edges" in sql for sql in selects)
    with closing(connection.open_reader(connection.store_path(stored))) as reader:
        from test_vault_store_readers import full_reads

        if PLAN_REPORT:
            plans = {
                sql: [row[-1] for row in reader.execute(f"EXPLAIN QUERY PLAN {sql}")]
                for sql in selects
            }
            Path(PLAN_REPORT).write_text(json.dumps({"pages": GENERATED, "plans": plans}, indent=1))
        scans = {
            sql: found for sql in selects
            if (found := {t for t in full_reads(reader, sql) if t and t.startswith(owned)})
        }
    assert scans == {}


@pytest.mark.timeout(300)
def test_a_commit_writes_the_link_rows_of_its_own_pages_and_no_others(
    stored, writer, monkeypatch  # noqa: F811
):
    """A commit that rebuilds the link rows of the whole vault, or of every page that links to the
    one it wrote, costs O(pages) on every write (``log.md`` is rewritten by almost every write);
    one that forgets a moved or deleted page's rows keeps listing links that no page holds."""
    for rel in (CLAIM, EVIDENCE, RIVAL):
        write.commit(writer, put(rel, PAGES[rel].encode()))
    commit_linked(writer, GENERATED)
    statements = []
    writer.connection.set_trace_callback(statements.append)
    try:
        moved = linked_page(3)[0].replace("Page-0003", "Moved")
        write.commit(writer, write.StoreChange(
            write.Origin.GOVERNED, "test",
            puts=(write.PagePut(EVIDENCE, PAGES[EVIDENCE].encode() + b"\nMore [[Rival]].\n"),),
            moves=(write.PageMove(linked_page(3)[0], moved),),
            deletes=(write.PageDelete(linked_page(4)[0]),),
        ))
    finally:
        writer.connection.set_trace_callback(None)
    touched = [sql for sql in statements if "inbound_" in sql and "SELECT" not in sql]
    pages = (EVIDENCE, linked_page(3)[0], moved, linked_page(4)[0])

    assert touched and all(any(f"'{rel}'" in sql for rel in pages) for sql in touched)
    assert all(any(f"'{rel}'" in sql for sql in touched) for rel in pages)

    def link(path, body):
        return (path, 7, body.decode().splitlines()[6].strip(), "Lantern Claim")

    kept = [link(*linked_page(index)) for index in range(GENERATED) if index not in (3, 4)]
    assert inbound(stored, CLAIM)[1] == sorted(
        [*kept, link(moved, linked_page(3)[1]), link(EVIDENCE, PAGES[EVIDENCE].encode())]
    )


@pytest.mark.timeout(500)
def test_the_correction_context_of_a_unit_reads_the_store_snapshot(vault):  # noqa: F811
    """The correction context of ``activate_context`` and ``context_pack`` asks the file-mode
    freshness registry for its checkpoint: in store mode it has none, so every call answers
    ``unavailable`` and a claim that another unit corrects is served without its correction."""
    from test_relation_fragment_targets import (
        DUP_ID,
        DUP_TARGET,
        SOURCE,
        TARGET,
        _source_text,
        _target_text,
    )

    from exomem import freshness, semantic_index, semantic_language_registry, unit_corrections

    registry = semantic_language_registry.registry_path(vault)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        "schema_version: 1\ncategories:\n  config:\n    description: Configuration facts\n"
        "    aliases: [configuration]\nkinds: {}\n"
    )
    for rel, text in (
        (TARGET, _target_text()),
        (DUP_TARGET, f"---\ntype: insight\ntitle: Frag dup\nexomem_id: {DUP_ID}\n---\n# Frag dup\n\n"
         "## Observations\n- [configuration] First ^dup\n- [configuration] Second ^dup\n"),
        (SOURCE, _source_text()),
    ):
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(text)
    build_indexes(vault)
    reference = semantic_index.current_parent_index_state(
        vault, TARGET
    ).document.resolve_fragment("rich-1").unit.unit_ref
    # File mode serves the context from a live checkpoint, as a service's watcher keeps it.
    freshness.seed(vault, "vault", (
        (str(path), freshness.stat_signature(path)) for path in vaults.walk_vault_md(vault)
    ))
    find.evict_resolver_caches(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    find.recall_resolver_snapshot(
        vault, expected_checkpoint=freshness.live_recall_checkpoint(vault, "vault")
    )
    in_file_mode = unit_corrections.discover(vault, [reference], accept=lambda _row: True)
    assert in_file_mode["status"] == "available" and in_file_mode["units"]
    freshness.invalidate(vault)
    assert migrate_apply(vault) == 0
    damage_every_owned_projection(vault, "poisoned")
    sidecar = epistemic_graph.sidecar_path(vault)
    sidecar.unlink()
    sidecar.write_bytes(SENTINEL)
    find.evict_resolver_caches(vault)

    with read.request_snapshot(vault):
        find.recall_resolver_snapshot(vault)  # the request's resolver, as its recall lanes build it
        in_store_mode = unit_corrections.discover(vault, [reference], accept=lambda _row: True)

    assert in_store_mode == in_file_mode


def test_a_relation_filter_answers_from_the_store_when_the_sidecar_file_is_gone(
    stored, writer  # noqa: F811
):
    """A graph reader that asks whether the file-built sidecar exists before it reads the store: in
    a store-mode vault whose state holds no sidecar, every relation filter reports warming."""
    for rel in (CLAIM, EVIDENCE, RIVAL):
        write.commit(writer, put(rel, PAGES[rel].encode()))
    epistemic_graph.sidecar_path(stored).unlink(missing_ok=True)
    find.clear_cache()

    found = find.find(stored, query="", relations=["supports"], limit=15)
    participants = epistemic_graph.EpistemicGraphIndex(stored).relation_participants(["supports"])

    assert {hit.path for hit in found} == {CLAIM, EVIDENCE}
    assert (participants.status, participants.paths) == ("available", frozenset({CLAIM, EVIDENCE}))
