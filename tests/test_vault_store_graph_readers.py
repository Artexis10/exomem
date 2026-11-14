"""What a store-mode vault's graph and link readers answer (OpenSpec make-vault-store-canonical,
tasks 3.1 and 3.2, the graph half).

The vault is the migration suite's vault with typed relations, moved into store mode by the real
migrate route. Its file-built graph sidecar is then replaced by a sentinel and every projected page
is poisoned, so a reader that still takes either fails or serves the poison.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import sqlite3
import sys
import threading
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

from exomem import (
    access,
    commands,
    epistemic_graph,
    find,
    graph_read_publication,
    lexstore,
    semantic_index,
)
from exomem import vault as vaults
from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.governance import egress
from exomem.governance.principal import library_scope, request_scope
from exomem.vault_store import mode, ownership, read, rederive, targets, write
from exomem.vocabulary import instances

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


LAMP = f"{INSIGHTS}/Lamp Analysis.md"
#: Units for the dimension reads, apart from the lantern claim's graph: an attributed claim derived
#: from a survey (`attribution`, whose display reads that origin edge) and a claim that supports it
#: (`argument`). A withheld third `Tide` makes the lantern claim's `[[Tide]]` resolve differently
#: for the external caller, so that caller's reads re-derive the claim's edges in its own view.
UNIT_PAGES = {
    LAMP: (
        "---\ntitle: Lamp Analysis\ntype: insight\n---\n# Lamp Analysis\n\n"
        "## Claim\n- id: oil\n- asserted_by: Keeper team\n"
        "- relations: derived_from: [[Lamp Survey]]\n\nThe lighthouse lamp burns oil.\n\n"
        "## Claim\n- relations: supports: [[#oil]]\n\nThe lamp room smells of oil.\n"
    ),
    f"{INSIGHTS}/Lamp Survey.md": (
        "---\ntitle: Lamp Survey\ntype: insight\n---\n# Lamp Survey\n\n"
        "## Claim\n\nThe keepers counted the oil casks.\n"
    ),
    "Knowledge Base/Notes/Hidden/Tide.md": (
        "---\ntitle: Tide\ntype: note\ntags: [secret]\n---\n# Tide\n\nA withheld tide note.\n"
    ),
}


def write_pages(root, pages=PAGES):
    for rel, text in pages.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def dimension_read(root, dimension):
    """A unit search filtered by `dimension`, with the marks its group's display sets."""
    answer = commands.op_ask_memory(
        root, query="", mode="keyword", scope="kb-only", result_level="unit",
        filters={"unit.dimension": {"$eq": dimension}}, limit=15,
    )
    return {"hits": answer["hits"], "groups": answer.get("dimension_groups")}


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
        # Membership needs the typed edges; the attribution group's origin mark reads them.
        "argument": dimension_read(root, "argument"),
        "attribution": dimension_read(root, "attribution"),
    }


def answers_for_both_callers(root):
    owner = graph_answers(root)
    with request_scope(EXTERNAL):
        external = graph_answers(root)
    return {"owner": owner, "external": external}


@contextlib.contextmanager
def record_store_reads(root, monkeypatch, *, page_paths=None):
    """Record page filesystem reads, sidecar connections, repair and store snapshots.

    Keep the graph test and the registry surface test on the same instrument. Repair seams
    record intent without starting a background repair against deliberately damaged files.
    """
    events = {name: [] for name in (
        "page_io", "sidecars", "snapshots", "snapshot_threads", "request_threads", "machinery",
    )}

    def watching(real, operation):
        def watched(path, *arguments, **keywords):
            try:
                rel = Path(path).relative_to(root).as_posix()
            except (TypeError, ValueError):
                rel = None  # A descriptor, or a path outside the vault.
            page_owned = (
                rel in page_paths if page_paths is not None
                else rel is not None and ownership.classify(rel) is ownership.Ownership.PAGE
            )
            if page_owned:
                events["page_io"].append((operation, rel))
            return real(path, *arguments, **keywords)

        return watched

    real_reader = connection.store_reader
    real_connect = sqlite3.connect
    real_request = read.request_snapshot

    @contextlib.contextmanager
    def requesting(*arguments, **keywords):
        with real_request(*arguments, **keywords):
            scope = read._REQUEST.get()
            if scope is not None and scope.thread not in events["request_threads"]:
                events["request_threads"].append(scope.thread)
            yield

    def counting(*arguments, **keywords):
        events["snapshots"].append(1)
        events["snapshot_threads"].append(threading.get_ident())
        return real_reader(*arguments, **keywords)

    def connecting(database, *arguments, **keywords):
        # These are the two retired sidecar filename suffixes, not a semantic vocabulary.
        if any(suffix in str(database) for suffix in (".graph.sqlite", ".lexical.sqlite")):
            events["sidecars"].append(str(database))
        return real_connect(database, *arguments, **keywords)

    with monkeypatch.context() as patch:
        for owner_module, name in (
            (epistemic_graph, "graph_lag"),
            (epistemic_graph, "schedule_background_rebuild"),
            (graph_read_publication, "published"),
            (vaults, "_build_inbound_index"),
            (find, "schedule_inbound_rebuild"),
            (lexstore, "_schedule_repair"),
            (epistemic_graph, "_sqlite_connect_owned"),
        ):
            patch.setattr(
                owner_module, name,
                lambda *a, _name=name, **k: events["machinery"].append(_name),
            )
        for owner_module, name in (
            (os, "stat"), (os, "lstat"), (os, "open"), (os, "scandir"),
            (builtins, "open"), (io, "open"),
        ):
            patch.setattr(owner_module, name, watching(getattr(owner_module, name), name))
        patch.setattr(connection, "store_reader", counting)
        patch.setattr(read, "request_snapshot", requesting)
        patch.setattr(sqlite3, "connect", connecting)
        yield events


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
    write_pages(vault, UNIT_PAGES)
    build_indexes(vault)
    find.clear_cache()
    in_file_mode = answers_for_both_callers(vault)
    owner, external = in_file_mode["owner"], in_file_mode["external"]
    assert owner["supports"] and owner["context"]["edges"] and owner["ask"]
    for caller in (owner, external):
        assert "Lamp Analysis" in {hit["parent_title"] for hit in caller["argument"]["hits"]}
        assert any(hit.get("display") for hit in caller["attribution"]["hits"])
    assert {row[0] for row in owner["inbound"][CLAIM][1]} == {EVIDENCE, SECRET, OUTSIDE}
    assert SECRET not in {row[0] for row in external["inbound"][CLAIM][1]}
    assert migrate_apply(vault) == 0

    damage_every_owned_projection(vault, "poisoned")
    sidecar = epistemic_graph.sidecar_path(vault)
    sidecar.unlink()
    sidecar.write_bytes(SENTINEL)  # a read of the file-built graph fails
    vaults.clear_inbound_index()
    find.clear_cache()
    with record_store_reads(vault, monkeypatch) as events:
        in_store_mode = answers_for_both_callers(vault)
        assert in_store_mode == in_file_mode
        assert SENTINEL.decode().strip() not in json.dumps(in_store_mode, default=str)
        assert events["machinery"] == []
        # Retain this regression's original stat contract; the surface test also checks opens.
        assert not [event for event in events["page_io"] if event[0] in ("stat", "lstat")]
        assert events["sidecars"] == []
        events["snapshots"].clear()
        commands.op_graph_context(vault, path=CLAIM, depth=2)
        commands.op_list_inbound_links(vault, target=CLAIM)
        assert len(events["snapshots"]) == 2, "one snapshot for each tool call"


def test_a_store_commit_expires_vocabulary_continuations_and_advances_census(vault):  # noqa: F811
    """A binary-record commit advances the store revision without rewriting graph rows."""
    from test_vault_store_migration import DIAGRAM
    from test_vocabulary_projection import fixture_many_edges

    from exomem import relation_census, vocabulary_projection
    from exomem.collection_store.writer import CollectionWriter

    path = fixture_many_edges(vault)
    build_indexes(vault)
    assert migrate_apply(vault) == 0
    with library_scope(), read.request_snapshot(vault):
        first = vocabulary_projection.for_write(vault, path=path)
        generation = relation_census.census(vault)["graph_generation"]
        with read.snapshot(vault) as view:
            record = view.binary_records((DIAGRAM,))[DIAGRAM]
    assert first["continuation"]
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True) as handle:
        write.commit(CollectionWriter(vault, handle), write.StoreChange(
            write.Origin.GOVERNED, "test",
            binaries=(write.BinaryRecord(record.path, record.sha256, record.size),),
        ))
    with library_scope(), read.request_snapshot(vault):
        with pytest.raises(ValueError, match="VOCABULARY_CONTINUATION_STALE"):
            vocabulary_projection.for_write(vault, path=path, continuation=first["continuation"])
        assert relation_census.census(vault)["graph_generation"] == generation + 1


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
    links" from it tells an agent that a page is safe to delete; one that answers once the pass's
    first batch has created the tables counts only the pages that the batch reached; an upgrade
    that leaves its tables out of the rollback record keeps them after a rollback; one that never
    fills them serves an empty list as complete."""
    write_pages(vault)
    # More stored pages than one batch derives, which sort before every page that links the claim.
    write_pages(vault, {
        f"Knowledge Base/Almanac/Entry {index:02d}.md": f"# Entry {index:02d}\n"
        for index in range(rederive.BATCH_PAGES + 8)
    })
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
        assert rederive.settle(vault, upgrading) == rederive.ADVANCED
        assert mode.status(vault)["owner_schema"] == "behind"
        with pytest.raises(OpError) as refused:
            commands.op_list_inbound_links(vault, target=CLAIM)
        assert refused.value.code == read.DERIVED_BEHIND
        with pytest.raises(OpError) as refused:
            targets.inbound_links(vault, [CLAIM])  # a move's or a delete's check
        assert refused.value.code == read.DERIVED_BEHIND
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
    units = "".join(
        f"- [constraint] The keeper logs observation {unit} of lantern {index}. ^observation-{unit}\n"
        for unit in range(int(os.environ.get("EXOMEM_ADMISSION_DRILL_UNITS", "0")))
    )
    return (
        f"Knowledge Base/Notes/Gen/Page-{index:04d}.md",
        (
            f"---\nexomem_id: 00000000-0000-4000-8000-{index:012d}\n"
            f"title: Page {index}\ntype: note\n"
            f"created: 2026-01-{index % 28 + 1:02d}\n"
            f"updated: 2026-06-{index % 28 + 1:02d}\nproject: harbour\n"
            f"tags: [navigation, fieldwork, cohort-{index % 10}]\n"
            "aliases: []\nstatus: active\n"
            "summary: Observations from the lighthouse survey\n"
            "sources: []\nrelated: []\n---\n"
            f"# Page {index}\n\n"
            f"The lantern number {index} burns at the lighthouse, as [[Lantern Claim]] says; "
            f"see [[Page-{(index + 1) % max(GENERATED, 1):04d}]].\n"
            + (f"\n## Observations\n{units}" if units else "")
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
            "cold_samples_ms": cold,
            "warm_samples_ms": warm,
        }
    with request_scope(EXTERNAL):
        cold, warm = [], []
        for _ in range(3):
            find.clear_cache()
            for times in (cold, warm):
                started = time.perf_counter()
                assert GRAPH_CALLS["find relation_of"](root)
                times.append((time.perf_counter() - started) * 1000)
        figures["find relation_of external"] = {
            "cold_ms": round(statistics.median(cold)),
            "warm_ms": round(statistics.median(warm)),
            "cold_samples_ms": cold,
            "warm_samples_ms": warm,
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
    restrict_external(stored, 'tags: ["secret"]')
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

    owned = ("graph_", "inbound_", "page_admission")
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
        number, line = next(
            (number, line) for number, line in enumerate(body.decode().splitlines(), 1)
            if "[[Lantern Claim]]" in line
        )
        return (path, number, line.strip(), "Lantern Claim")

    kept = [link(*linked_page(index)) for index in range(GENERATED) if index not in (3, 4)]
    assert inbound(stored, CLAIM)[1] == sorted(
        [*kept, link(moved, linked_page(3)[1]), link(EVIDENCE, PAGES[EVIDENCE].encode())]
    )


BIN_CITE = f"{INSIGHTS}/Bin Cite.md"
BIN_HIDDEN = "Knowledge Base/Notes/Secret Bin.md"
BIN_TWIN = "Knowledge Base/Notes/diagram.bin.md"
BIN_TARGET = "Knowledge Base/Notes/diagram.bin"

BIN_CITE_BODY = (
    "---\ntitle: Bin Cite\ntype: note\n---\n# Bin Cite\n\n"
    "Bare [[diagram.bin]] here.\n"
    "Embed ![[diagram.bin]] too.\n"
    "Alias [[diagram.bin|the map]].\n"
    "Path [[Notes/diagram.bin]] form.\n"
    "Full [[Knowledge Base/Notes/diagram.bin]] form.\n"
)
BIN_HIDDEN_BODY = (
    "---\ntitle: Secret Bin\ntype: note\ntags: [secret]\n---\n# Secret Bin\n\n"
    "Cites [[diagram.bin]] quietly.\n"
)
BIN_TWIN_BODY = (
    "---\ntitle: Diagram Bin\ntype: note\n---\n# Diagram Bin\n\n"
    "Self-cites [[diagram.bin]].\n"
)


def test_an_extensioned_target_counts_every_exact_filename_link(stored, writer):  # noqa: F811
    """`[[diagram.bin]]` names the file: a store-mode read counts every raw target that is exactly
    the filename (bare, embedded or aliased), like the graph's recorded link targets did, while a
    page's stem rule keeps the path forms. A caller that may not see a source never sees its link,
    and a page that shares the filename links to itself without counting."""
    restrict_external(stored, 'tags: ["secret"]')
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "test",
        puts=(
            write.PagePut(BIN_CITE, BIN_CITE_BODY.encode()),
            write.PagePut(BIN_HIDDEN, BIN_HIDDEN_BODY.encode()),
        ),
    ))
    expected = [
        (BIN_CITE, 7, "Bare [[diagram.bin]] here.", "diagram.bin"),
        (BIN_CITE, 8, "Embed ![[diagram.bin]] too.", "diagram.bin"),
        (BIN_CITE, 9, "Alias [[diagram.bin|the map]].", "diagram.bin"),
        (BIN_CITE, 10, "Path [[Notes/diagram.bin]] form.", "Notes/diagram.bin"),
        (BIN_CITE, 11, "Full [[Knowledge Base/Notes/diagram.bin]] form.",
         "Knowledge Base/Notes/diagram.bin"),
        (BIN_HIDDEN, 8, "Cites [[diagram.bin]] quietly.", "diagram.bin"),
    ]
    assert inbound(stored, BIN_TARGET) == (len(expected), sorted(expected))
    # The release gate asks which pages the target names: a binary names none, so a
    # restricted caller gets the absence shape rather than the binary's links.
    with request_scope(EXTERNAL):
        assert inbound(stored, BIN_TARGET) == (0, [])

    # A page whose stem is the filename makes the stem count one, so the page rule also opens the
    # basename bucket: the extensioned rule must add no duplicate of what it already counted, and
    # the page's self-link stays out. The gate then decides the page the target names, and the
    # hidden source's row still leaves the answer.
    write.commit(writer, put(BIN_TWIN, BIN_TWIN_BODY.encode()))
    assert inbound(stored, BIN_TARGET) == (len(expected), sorted(expected))
    with request_scope(EXTERNAL):
        answer = commands.op_list_inbound_links(stored, target=BIN_TARGET)
    assert sorted(
        (row["path"], row["line_number"]) for row in answer["inbound"]
    ) == sorted((row[0], row[1]) for row in expected[:5])


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


def test_a_link_hub_reads_no_author_summary_and_binds_policy_once(
    stored, writer, monkeypatch,  # noqa: F811
):
    """Core links must not parse each author or stat the access policy once per endpoint."""
    write.commit(writer, put(CLAIM, PAGES[CLAIM].encode()))
    commit_linked(writer, 100)
    interpreted, summaries, policies = [], [], []
    parent = semantic_index.Interpretations.parent
    load = epistemic_graph.GraphView._summaries
    policy = access._policy_state

    def interpret(self, path, metadata):
        interpreted.append(path)
        return parent(self, path, metadata)

    def loading(self, paths):
        summaries.extend(paths)
        return load(self, paths)

    def reading_policy(*args, **kwargs):
        policies.append(1)
        return policy(*args, **kwargs)

    monkeypatch.setattr(semantic_index.Interpretations, "parent", interpret)
    monkeypatch.setattr(epistemic_graph.GraphView, "_summaries", loading)
    monkeypatch.setattr(access, "_policy_state", reading_policy)
    result = epistemic_graph.EpistemicGraphIndex(stored).relation_participants([], anchor=CLAIM)

    assert result.status == "available"
    assert {path for path in result.paths if "/Gen/" in path} == {
        linked_page(index)[0] for index in range(100)
    }
    assert len(policies) < 12, f"policy reads grew with hub degree: {len(policies)}"
    assert not any("/Gen/" in path for path in interpreted + summaries)


def test_an_interpretation_reads_the_binding_configuration_once(tmp_path, monkeypatch):
    """Interpreting another author must not re-read connector configuration for every page."""
    reads = []
    real = instances.configuration

    def configuration(root):
        reads.append(1)
        return real(root)

    monkeypatch.setattr(instances, "configuration", configuration)
    interpretation = semantic_index.Interpretations(tmp_path)
    assert interpretation.definitions("first.md", {}) is not None
    assert interpretation.definitions("second.md", {}) is not None
    interpretation.admitted_instances()
    interpretation.dependency()
    interpretation.public_dependency()
    assert len(reads) == 1


@pytest.mark.parametrize("relations", [None, ["supports"]])
def test_a_repeated_store_find_reads_no_graph_statement(stored, writer, monkeypatch, relations):  # noqa: F811
    """A result-cache hit must skip graph resolution, including anchored key validation."""
    for rel in (CLAIM, EVIDENCE, RIVAL):
        write.commit(writer, put(rel, PAGES[rel].encode()))
    find.clear_cache()
    kwargs = dict(query="", relation_of=CLAIM, relations=relations, mode="keyword", graph=False)
    first = find.find(stored, **kwargs)
    assert first
    statements = []
    real = connection.store_reader

    @contextlib.contextmanager
    def tracing(*args, **kwargs):
        with real(*args, **kwargs) as reader:
            reader.set_trace_callback(statements.append)
            try:
                yield reader
            finally:
                reader.set_trace_callback(None)

    monkeypatch.setattr(connection, "store_reader", tracing)
    timings = find.FindTimings()
    second = find.find(stored, **kwargs, timings=timings)
    assert second == first and timings.cache["hit"]
    # The graph tables share the `graph_` name prefix that their schema fixes; a statement naming one reads them.
    assert not [sql for sql in statements if "graph_" in sql.lower()]


def test_relation_matching_keeps_units_symmetry_unavailable_instances_and_withheld_names(vault):  # noqa: F811
    """Slim core matching must preserve file-mode answers at the vocabulary and privacy boundaries."""
    unavailable = f"{INSIGHTS}/Unavailable.md"
    pages = {
        **PAGES, **UNIT_PAGES,
        LAMP: UNIT_PAGES[LAMP] + "\n## Claim\n- relations: supports: [[Tide Evidence]]\n\nOil is evidence.\n",
        unavailable: (
            "---\ntitle: Unavailable\ntype: note\nregistry_scope: missing\n---\n"
            "A core link to [[Tide Evidence]] survives unavailable extension definitions.\n"
        ),
    }
    del pages[TIDE_TWIN]  # Leave one visible homonym and one withheld homonym.
    write_pages(vault, pages)
    restrict_external(vault, 'tags: ["secret"]')
    build_indexes(vault)

    def answers():
        index = epistemic_graph.EpistemicGraphIndex(vault)
        keep = egress.edge_view(vault, None)
        def resolver():
            return find.recall_resolver_snapshot(vault)
        results = {
            "unit": index.relation_participants(["supports"], keep=keep, resolver=resolver),
            "symmetric inbound": index.relation_participants(
                ["contradicts"], direction="inbound", keep=keep, resolver=resolver,
            ),
            "symmetric outbound": index.relation_participants(
                ["contradicts"], direction="outbound", keep=keep, resolver=resolver,
            ),
            "homonym": index.relation_participants([], anchor=TIDE, keep=keep, resolver=resolver),
            "unavailable": index.relation_participants([], anchor=unavailable, keep=keep),
        }
        assert LAMP in results["unit"].paths
        assert results["symmetric inbound"].paths == results["symmetric outbound"].paths
        assert {CLAIM, RIVAL} <= results["symmetric inbound"].paths
        assert results["unavailable"].status == "temporarily_unavailable"
        return results

    before = {"owner": answers()}
    with request_scope(EXTERNAL):
        before["external"] = answers()
        assert CLAIM in before["external"]["homonym"].paths
        assert SECRET not in before["external"]["unit"].paths
    migrate_apply(vault)
    find.clear_cache()
    assert answers() == before["owner"]
    with request_scope(EXTERNAL):
        assert answers() == before["external"]


@pytest.mark.parametrize("change", ["store commit", "policy", "caller"])
def test_withheld_names_follow_the_store_policy_and_caller(stored, writer, change):  # noqa: F811
    """A stale visibility cache reveals a withheld homonym by removing a visible link or endpoint."""
    from dataclasses import replace

    source = f"{INSIGHTS}/Survey.md"
    visible = f"{INSIGHTS}/Signal.md"
    hidden = f"{INSIGHTS}/Other.md"
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "homonyms", puts=(
        write.PagePut(source, b"---\ntitle: Survey\ntype: note\n---\nSee [[Beacon]].\n"),
        write.PagePut(visible, b"---\ntitle: Beacon\ntype: note\ntags: [blocked]\n---\nVisible beacon.\n"),
        write.PagePut(hidden, (
            "---\ntitle: " + ("Other" if change == "store commit" else "Beacon")
            + "\ntype: note\ntags: [secret]\n---\nPrivate beacon.\n"
        ).encode()),
    )))
    project(writer, stored)
    restrict_external(stored, 'tags: ["secret"]')
    governance = stored / "Knowledge Base" / "_Governance"
    if change == "caller":
        (governance / "scopes" / "blocked.yaml").write_text(
            'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAW\nname: Blocked\ntags: [blocked]\n'
        )
        (governance / "rules" / "partner.yaml").write_text(
            'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB1\n'
            'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAW"]\naudience: partner\nceiling: 0\n'
        )

    def answer(who, anchor, withheld):
        with request_scope(who):
            find.clear_cache()  # Clear result/resolver caches; the revision's name cache must stay valid.
            hits = find.find(stored, query="", relation_of=anchor, mode="keyword", graph=False)
            assert source in {hit.path for hit in hits}
            assert withheld not in json.dumps([(hit.path, hit.title, hit.excerpt) for hit in hits])
            return [(hit.path, hit.title, hit.excerpt) for hit in hits]

    before = answer(EXTERNAL, visible, hidden)
    assert answer(EXTERNAL, visible, hidden) == before
    if change == "store commit":
        write.commit(writer, put(hidden, (
            b"---\ntitle: Beacon\ntype: note\ntags: [secret]\n---\nPrivate beacon.\n"
        )))
        assert answer(EXTERNAL, visible, hidden) == before
    elif change == "policy":
        scope = governance / "scopes" / "secret.yaml"
        scope.write_text(scope.read_text().replace('["secret"]', '["blocked"]'))
        assert answer(EXTERNAL, hidden, visible) == before
    else:
        assert answer(replace(EXTERNAL, audience_id="partner"), hidden, visible) == before


def test_a_repeated_restricted_hub_does_not_recheck_unchanged_pages(
    stored, writer, monkeypatch,  # noqa: F811
):
    """A new graph operation must not repeat visibility decisions on unchanged linking pages."""
    write.commit(writer, put(CLAIM, PAGES[CLAIM].encode()))
    commit_linked(writer, 40)
    restrict_external(stored, 'tags: ["secret"]')
    def participants():
        with read.request_snapshot(stored):
            return epistemic_graph.EpistemicGraphIndex(stored).relation_participants(
                [], anchor=CLAIM, keep=egress.edge_view(stored, find.caller_admission(stored)),
                resolver=lambda: find.recall_resolver_snapshot(stored),
            )

    with request_scope(EXTERNAL):
        assert participants().paths
        checked = []
        permits = egress.content_permits

        def checking(root, path, *args, **kwargs):
            checked.append(path)
            return permits(root, path, *args, **kwargs)

        monkeypatch.setattr(egress, "content_permits", checking)
        find.clear_cache()
        assert participants().paths
        # Every endpoint still passes recall admission; its stable release verdict needs no new read.
        assert not any("/Gen/" in path for path in checked)


def admission_sample(root, caller, operation):
    """One graph-reader sample, including the scalar work hidden by an elapsed time."""
    import time

    import yaml

    from exomem import find_corpus
    from exomem.governance.principal import library_scope

    counters = {"scalar_decisions": 0, "page_parses": 0, "yaml_decodes": 0,
                "ordinary_scalar_decisions": 0, "ordinary_page_parses": 0}

    def counted(name, real):
        def call(*args, **kwargs):
            counters[name] += 1
            if name != "yaml_decodes":
                path = str(args[1] if name == "scalar_decisions" else args[0])
                if path.startswith(str(root) + "/"):
                    path = path[len(str(root)) + 1:]
                if path.startswith("Knowledge Base/Notes/"):
                    counters["ordinary_" + name] += 1
            return real(*args, **kwargs)
        return call

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(egress, "_decide_path", counted("scalar_decisions", egress._decide_path))
        patch.setattr(find_corpus, "_parse_page_after_checkpoint",
                      counted("page_parses", find_corpus._parse_page_after_checkpoint))
        # Both safe_load and the vault's libyaml loader call this decoder entry.
        patch.setattr(yaml, "load", counted("yaml_decodes", yaml.load))
        with library_scope(), request_scope(EXTERNAL) if caller != "owner" else contextlib.nullcontext():
            started = time.perf_counter()
            if operation == "participants":
                with read.request_snapshot(root):
                    answer = epistemic_graph.EpistemicGraphIndex(root).relation_participants(
                        [], anchor=CLAIM, keep=egress.edge_view(root, find.caller_admission(root)),
                        resolver=lambda: find.recall_resolver_snapshot(root),
                    )
                size = len(answer.paths)
            else:
                size = len(GRAPH_CALLS["find relation_of"](root))
            elapsed = (time.perf_counter() - started) * 1000
    return {"ms": elapsed, "result_count": size, **counters, "load": os.getloadavg()}


@pytest.mark.skipif(not os.environ.get("EXOMEM_ADMISSION_DRILL_REPORT"), reason="Explicit load drill")
@pytest.mark.timeout(1800)
def test_restricted_admission_drill(stored, writer):  # noqa: F811
    """Measure a realistic hub before and after commits and owner-pass revision bumps."""
    import subprocess

    from exomem.governance import membership

    links = int(os.environ.get("EXOMEM_ADMISSION_DRILL_LINKS", str(GENERATED)))
    for rel in (CLAIM, EVIDENCE, RIVAL):
        write.commit(writer, put(rel, PAGES[rel].encode()))
    for start in range(0, GENERATED, 200):
        puts = []
        for index in range(start, min(GENERATED, start + 200)):
            rel, body = linked_page(index)
            if index >= links:
                body = body.replace(b"[[Lantern Claim]]", b"the keeper")
            puts.append(write.PagePut(rel, body))
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "drill", puts=tuple(puts)))
    restrict_external(stored, 'tags: ["cohort-0"]')
    report = {"pages": GENERATED, "hub_links": links,
              "units_per_page": int(os.environ.get("EXOMEM_ADMISSION_DRILL_UNITS", "0")), "samples": {}}
    report_path = Path(os.environ["EXOMEM_ADMISSION_DRILL_REPORT"])
    repeats = int(os.environ.get("EXOMEM_ADMISSION_DRILL_REPEATS", "3"))
    callers = os.environ.get("EXOMEM_ADMISSION_DRILL_CALLERS", "owner,few withheld,most withheld,homonym").split(",")
    operations = os.environ.get("EXOMEM_ADMISSION_DRILL_OPERATIONS", "participants,find").split(",")
    states = os.environ.get("EXOMEM_ADMISSION_DRILL_STATES", "cold process,first after commit,registry pass,warm").split(",")
    for caller in callers:
        scope = stored / "Knowledge Base/_Governance/scopes/secret.yaml"
        selector = '["cohort-0"]' if caller != "most withheld" else (
            '[' + ', '.join(f'"cohort-{i}"' for i in range(9)) + ']'
        )
        scope.write_text(scope.read_text().split("tags:")[0] + f"tags: {selector}\n")
        if caller == "homonym":
            write.commit(writer, put("Knowledge Base/Notes/Hidden/Page-0001.md", (
                b"---\ntitle: Page 1\ntype: note\ntags: [cohort-0]\n---\n# Hidden twin\n"
            )))
        for operation in operations:
            for state in states:
                samples = []
                for repeat in range(repeats):
                    if state == "cold process":
                        result = subprocess.run([
                            sys.executable, "-c",
                            "import json,sys; from pathlib import Path; "
                            "from test_vault_store_graph_readers import admission_sample; "
                            "print(json.dumps(admission_sample(Path(sys.argv[1]),sys.argv[2],sys.argv[3])))",
                            str(stored), caller, operation,
                        ], check=True, capture_output=True, text=True, timeout=180)
                        samples.append(json.loads(result.stdout.splitlines()[-1]))
                        continue
                    if state == "first after commit":
                        write.commit(writer, put(EVIDENCE, PAGES[EVIDENCE].encode() + f"\n{repeat}\n".encode()))
                    elif state == "registry pass":
                        if repeat == 0:
                            with writer.handle.transaction() as tx:
                                rederive._set(tx, rederive.OVERLAY_REVISION, str(read.current_revision(stored)))
                        rederive.settle(stored, writer)
                    else:
                        admission_sample(stored, caller, operation)
                    membership.clear_memo()
                    samples.append(admission_sample(stored, caller, operation))
                report["samples"][f"{caller}/{operation}/{state}"] = samples
                report_path.write_text(json.dumps(report, indent=2))
    with read.snapshot(stored) as snapshot:
        report["rows"] = {table: snapshot.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                          for table in ("store_pages", "graph_nodes", "graph_edges")}
        if snapshot.connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'page_admission'").fetchone():
            import time

            from exomem.governance import page_admission, policy

            facts = snapshot.connection.execute("SELECT path, simple, subject FROM page_admission").fetchall()
            subjects = [page_admission._subject(raw, path) for path, simple, raw in facts if simple]
            compiled = policy.load(stored)
            started = time.perf_counter()
            sets = {membership.evaluate_metadata_selectors(subject, compiled) for subject in subjects}
            report["census"] = {
                "facts": len(facts), "simple": len(subjects), "exceptions": len(facts) - len(subjects),
                "distinct_selector_sets": len(sets),
                "selector_us_per_page": (time.perf_counter() - started) * 1e6 / len(subjects),
            }
    if profile_path := os.environ.get("EXOMEM_ADMISSION_DRILL_PROFILE"):
        import cProfile
        import pstats

        write.commit(writer, put(EVIDENCE, PAGES[EVIDENCE].encode() + b"\nProfile revision.\n"))
        find.clear_cache()
        with cProfile.Profile() as profiler:
            report["profile_sample"] = admission_sample(stored, "few withheld", "find")
        profiler.dump_stats(profile_path + ".pstats")
        with Path(profile_path).open("w") as output:
            pstats.Stats(profiler, stream=output).sort_stats("cumulative").print_stats(65)
    report_path.write_text(json.dumps(report, indent=2))
