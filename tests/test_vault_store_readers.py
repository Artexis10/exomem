"""What a store-mode vault's readers answer when the projected Markdown cannot be trusted.

OpenSpec make-vault-store-canonical, design D3, task 3.1 (first half). The vault is the migration
suite's generated vault, moved into store mode; pages that only the store holds are committed
through ``write.commit``, as the store thread commits them.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import (
    ALPHA,
    BETA,
    build_indexes,
    migrate_apply,
    vault,  # noqa: F401
)
from test_vault_store_read_write import EDITED, NEW, put, stored, writer  # noqa: F401
from test_vault_store_rederive import PROFILES, project, settle_to_the_end
from test_vault_store_write_path import served  # noqa: F401

from exomem import __main__ as cli
from exomem import (
    activation_conventions,
    bm25,
    commands,
    context_roles,
    document_containers,
    epistemic_graph,
    get_page,
    lexstore,
    list_directory,
    memory_refs,
    project_keys,
    reconcile,
    reserved_paths,
    warmup,
    working_set,
    working_set_runtime,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.collection_store import connection
from exomem.collection_store.writer import CollectionWriter
from exomem.find_types import FindTimings, Hit
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, request_scope
from exomem.vault_store import importer, ownership, read, write
from exomem.vault_store import mode as vault_store_mode
from exomem.vocabulary import registry

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

SENTINEL = b"POISONED PROJECTION: a reader served this file instead of the store\n"
#: How many pages only the store holds; the drill at scale sets it from the environment.
GENERATED = int(os.environ.get("EXOMEM_READERS_DRILL_PAGES", "40"))


def generated_page(index):
    return (
        f"Knowledge Base/Notes/Gen/Page-{index:04d}.md",
        f"---\ntitle: Page {index}\ntype: note\n---\n# Page {index}\n\n"
        f"The lantern number {index} burns at the lighthouse harbour.\n".encode(),
    )


def corpus_pages(root):
    return {path.relative_to(root).as_posix() for path in find_module._corpus_walk(root, "kb")}


def answers(root, generated):
    """What find, get_page, a listing and the corpus say about the vault, as plain data."""
    hits = find_module.find(root, query="lighthouse", limit=15, graph=False, rerank=False)
    # The full recall tool: its default graph and hybrid ranking read each page via the page cache.
    asked = commands.op_ask_memory(root, query="lighthouse", limit=15)
    pages = [
        get_page.get_page(root, path=rel)
        for rel in (ALPHA, *(generated_page(index)[0] for index in generated))
    ]
    listing = list_directory.list_directory(root, path="Knowledge Base/Notes/Gen")
    notes = {
        limit: sorted(
            (
                entry.as_dict()
                for entry in list_directory.list_directory(
                    root, path="Knowledge Base/Notes", limit=limit
                ).entries
            ),
            key=lambda entry: entry["name"],
        )
        for limit in (None, 50)
    }
    return {
        "find": [(hit.path, hit.title, hit.excerpt) for hit in hits],
        "ask": [(hit["path"], hit.get("title"), hit.get("excerpt")) for hit in asked["hits"]],
        "notice": asked.get("notice"),
        "pages": [(page.content, page.content_hash) for page in pages],
        "listing": sorted(
            (entry.as_dict() for entry in listing.entries), key=lambda entry: entry["name"]
        ),
        "notes": notes,
        "corpus": sorted(corpus_pages(root)),
    }


#: The states a projected file takes when the store is the page: wrong bytes, none, or a link.
DAMAGE = ("poisoned", "deleted", "a link into the vault", "a link out of the vault")


def damage_every_owned_projection(root, damage):
    owned = [
        item.rel
        for item in ownership.iter_owned(root)
        if item.ownership is ownership.Ownership.PAGE
    ]
    inside = root / "Decoy.md"
    inside.write_bytes(SENTINEL)
    outside = root.parent / "outside.md"
    outside.write_bytes(SENTINEL)
    for rel in owned:
        path = root / rel
        if damage == "poisoned":
            path.write_bytes(SENTINEL)
            continue
        path.unlink()
        if damage != "deleted":
            path.symlink_to(inside if damage == "a link into the vault" else outside)


# Each case commits GENERATED pages and answers twice; a busy runner can exceed the 60 s default.
@pytest.mark.timeout(300)
@pytest.mark.parametrize("damage", DAMAGE)
def test_find_get_page_and_listing_answer_from_the_store_when_every_projected_file_is_damaged(
    stored, writer, damage, monkeypatch  # noqa: F811
):
    """A reader that still takes the file for the page: a stale projection, one that a failed
    projector left half-written, a missing one or a link at the page's name reaches the person who
    asks (or drops the page from them), and the store's page does not."""
    for index in range(GENERATED):
        rel, body = generated_page(index)
        write.commit(writer, put(rel, body))
    sampled = range(0, GENERATED, max(1, GENERATED // 10))
    find_module.clear_cache()
    scheduled = []
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda *args, **kwargs: scheduled.append(1))
    before = answers(stored, sampled)
    stored_only = f"{generated_page(0)[0].rsplit('/', 1)[0]}/"
    for answered in (before["find"], before["ask"]):
        assert len(answered) == 15, "a full page of answers is what this check needs"
        assert any(hit[0].startswith(stored_only) for hit in answered), (
            "pages that only the store holds must be among the answers"
        )
    # Every lexical lane read the store corpus, so the answer is whole: the reader gets no notice,
    # and the owner's diagnostics name the routing.
    assert before["notice"] is None
    find_module.clear_cache()  # a cached answer would skip the routing that this reads
    diagnosed = commands.op_ask_memory(stored, query="lighthouse", limit=15, include_timings=True)
    assert not (diagnosed.get("warming") or {}).get("components")
    assert diagnosed["timings"]["profile"]["lexical_lanes"] == {
        "corpus": "store",
        "catalogue": "store",
        "registry": "current",
        "unowned_rows_as_of": None,
    }
    assert len(before["listing"]) == GENERATED
    assert len(before["corpus"]) >= GENERATED + 2
    for listed in before["notes"].values():
        assert {"Alpha.md", "Beta.md", "diagram.bin", "Gen"} <= {entry["name"] for entry in listed}

    damage_every_owned_projection(stored, damage)
    find_module.clear_cache()
    after = answers(stored, sampled)

    assert after == before
    assert SENTINEL.decode().strip() not in json.dumps(after)
    # That repair rebuilds the catalogue from files, so in store mode it would run on every find.
    assert scheduled == []


def restrict_external(root, selector):
    """An active policy: pages that `selector` (a scope selector line) names are withheld from the
    external audience."""
    governance = root / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True)
    (governance / "rules").mkdir()
    (governance / "scopes" / "secret.yaml").write_text(
        f"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Secret\n{selector}\n"
    )
    (governance / "rules" / "secret-external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 0\n'
    )


EXTERNAL = RequestPrincipal(audience_id="external", surface="mcp")


UNIT = "\n- [constraint] The lantern stays lit until dawn. ^lit\n"
#: A unit whose category the shipped role registry projects as a method (`recall_context`).
METHOD = "\n- [technique] Trim the lantern wick before dusk. ^wick\n"


def derived_row_answers(root):
    """What each catalogue reader of find answers, as plain data: keyword, browse, a category
    filter, the unit lane, the vault scope, the hybrid ranks and the BM25 scores, in order; and
    what recall_context's text and method lanes answer, with their scores."""

    def found(**keywords):
        hits = find_module.find(
            root, limit=50, graph=False, rerank=False, mode="keyword", **keywords
        )
        return [(getattr(hit, "path", None) or hit.parent_path) for hit in hits]

    hybrid = find_module.find(root, query="lighthouse lantern", limit=50, graph=False, rerank=False)
    anchors = [
        SimpleNamespace(path=generated_page(index)[0], title=f"Page {index}")
        for index in range(GENERATED)
    ]
    text, text_state = working_set_runtime.lexical_evidence(
        root, "the lantern at the lighthouse", anchors, limit=50
    )
    catalogue = lexstore.get_store(root)
    published = catalogue.published_recall_checkpoint("kb")
    methods = working_set.search_method_units(
        root,
        bm25.token_units("trim the wick", query=True),
        50,
        categories=working_set.method_unit_categories(root),
        allowed=set(catalogue.recall_admitted_paths("kb", published)),
        fresh=published.triple,
        checkpoint=published,
    )
    return {
        "recall text": (text_state, [(hit.path, hit.bm25_rank) for hit in text]),
        "recall methods": (
            methods.readiness.status,
            [(hit.parent_path, hit.unit_ref, hit.lexical_score) for hit in methods.value or ()],
        ),
        "hybrid": [(hit.path, hit.bm25_rank, hit.keyword_rank, hit.vector_rank) for hit in hybrid],
        "bm25": lexstore.search_bm25(root, "lighthouse lantern", 50),
        "keyword": found(query="lantern"),
        "keyword and type": found(query="lighthouse", types=["note"]),
        "browse": found(query="", types=["note"]),
        "category": found(query="", categories=["constraint"]),
        "units": found(query="lantern dawn", result_level="unit"),
        "mixed": found(query="lantern dawn", result_level="mixed"),
        "vault scope": found(query="lantern", scope="vault"),
    }


@pytest.mark.timeout(300)
@pytest.mark.parametrize("damage", DAMAGE)
def test_each_catalogue_reader_answers_from_the_store_as_it_answers_in_file_mode(
    vault, damage, monkeypatch  # noqa: F811
):
    """A reader that still takes the sidecar that files built, or the projected file, for the
    store's rows: it refuses, answers empty or serves the poison, where file mode answers the same
    bytes; or one that ranks the store with another scorer, which moves the order and the scores."""
    for index in range(GENERATED):
        rel, body = generated_page(index)
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        extra = {0: UNIT, 1: METHOD}.get(index % 5, "")
        (vault / rel).write_bytes(body + extra.encode())
    build_indexes(vault)
    find_module.clear_cache()
    in_file_mode = derived_row_answers(vault)
    assert all(in_file_mode.values()), "each reader must have something to answer"
    assert in_file_mode["recall text"][1] and in_file_mode["recall methods"][1]
    assert migrate_apply(vault) == 0

    damage_every_owned_projection(vault, damage)
    sidecar = lexstore.lexical_path(vault)
    sidecar.unlink()
    sidecar.write_bytes(SENTINEL)  # a read of the file-built catalogue fails
    find_module.clear_cache()
    scheduled = []
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda *args, **kwargs: scheduled.append(1))

    assert derived_row_answers(vault) == in_file_mode
    assert scheduled == []


@pytest.mark.timeout(300)
@pytest.mark.parametrize("caller", ("the owner", "an external caller under an active policy"))
@pytest.mark.parametrize("damage", ("poisoned", "deleted"))
def test_a_store_mode_read_stats_no_stored_page_and_schedules_no_repair(
    stored, writer, damage, caller, monkeypatch  # noqa: F811
):
    """A read path that still checks a stored page's file (a freshness witness, an mtime key, a
    first-use sync, a governance decision) pays a stat for every page on every request, and keys
    its answer on a file that the store does not hold."""
    external = caller != "the owner"
    if external:
        restrict_external(stored, 'tags: ["secret"]')
    for index in range(GENERATED):
        write.commit(writer, put(*generated_page(index)))
    # The migration above ran in this process; a served process starts after it, with no memo of
    # notes that file-mode reads kept.
    document_containers.clear_unclaimed_notes()
    damage_every_owned_projection(stored, damage)
    find_module.clear_cache()
    repairs, stats = [], []

    def watching(real):
        def watched(path, *arguments, **keywords):
            try:
                rel = Path(path).relative_to(stored).as_posix()
            except (TypeError, ValueError):
                rel = None  # a descriptor, or a path outside the vault
            if rel is not None and ownership.classify(rel) is ownership.Ownership.PAGE:
                stats.append(rel)
            return real(path, *arguments, **keywords)

        return watched

    monkeypatch.setattr(os, "stat", watching(os.stat))
    monkeypatch.setattr(os, "lstat", watching(os.lstat))
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda *args, **kwargs: repairs.append(1))
    monkeypatch.setattr(lexstore, "request_repair", lambda *args, **kwargs: repairs.append(1))

    with request_scope(EXTERNAL) if external else contextlib.nullcontext():
        derived_row_answers(stored)
        commands.op_find(stored, query="lighthouse", limit=15, graph=False, rerank=False)
        commands.op_ask_memory(stored, query="lighthouse", limit=15)

    assert stats == []
    assert repairs == []


@pytest.mark.parametrize("catalogue_off", ("the python backend", "a SQLite build without FTS5"))
def test_a_store_vault_reads_the_store_corpus_for_its_lexical_lanes_with_the_catalogue_off(
    stored, writer, catalogue_off, monkeypatch  # noqa: F811
):
    """A routing that waits for the file catalogue to be usable: with the catalogue off, the lexical
    lanes of a store-mode vault fall back to a build from files that never covers the store."""
    for index in range(GENERATED):
        write.commit(writer, put(*generated_page(index)))
    if catalogue_off == "the python backend":
        monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    else:
        monkeypatch.setattr(lexstore, "fts5_available", lambda: False)
    find_module.clear_cache()

    asked = commands.op_ask_memory(stored, query="lighthouse", limit=15, include_timings=True)

    assert any(hit["path"].startswith("Knowledge Base/Notes/Gen/") for hit in asked["hits"])
    assert asked["timings"]["profile"]["lexical_lanes"] == {
        "corpus": "store",
        "catalogue": "bypassed",
        "registry": "current",
        "unowned_rows_as_of": None,
    }


def test_a_commit_that_changes_no_file_reaches_the_corpus_page_cache_and_resolver(
    stored, writer  # noqa: F811
):
    """A cache that keys on the file's stat never sees a commit that changes no file, so the corpus,
    the parsed page and a link keep missing the page that was just written."""
    assert NEW not in corpus_pages(stored)
    assert find_module._CACHE.get(stored / NEW, stored) is None
    assert find_module.shared_resolver(stored).title_key_for_path(NEW) is None

    write.commit(writer, put(NEW, EDITED.replace(b"title: Alpha", b"title: Gamma")))

    assert not (stored / NEW).exists()
    assert NEW in corpus_pages(stored)
    assert find_module._CACHE.get(stored / NEW, stored).title == "Gamma"
    assert find_module.shared_resolver(stored).title_key_for_path(NEW) == "gamma"

    write.commit(writer, put(NEW, EDITED.replace(b"title: Alpha", b"title: Delta")))

    assert find_module._CACHE.get(stored / NEW, stored).title == "Delta"
    assert find_module.shared_resolver(stored).title_key_for_path(NEW) == "delta"


@pytest.mark.timeout(300)
@pytest.mark.parametrize("caller", ("the owner", "an external caller under an active policy"))
@pytest.mark.parametrize("tool", ("op_find", "op_ask_memory"))
def test_a_recall_tool_call_reads_one_store_snapshot_however_many_pages_it_reads(
    stored, writer, tool, caller, monkeypatch  # noqa: F811
):
    """A reader that opens a snapshot per page costs 7 to 14 ms a page and may mix revisions in
    one answer; the call's reads, release decisions included, must not grow with its pages."""
    external = caller != "the owner"
    if external:
        restrict_external(stored, 'tags: ["secret"]')
    opened = []
    real = connection.store_reader

    def counting(*arguments, **keywords):
        opened.append(1)
        return real(*arguments, **keywords)

    monkeypatch.setattr(connection, "store_reader", counting)

    def snapshots_for_one_call(pages):
        for index in range(pages):
            rel, body = generated_page(index)
            write.commit(writer, put(rel, body))
        find_module.clear_cache()
        opened.clear()
        with request_scope(EXTERNAL) if external else contextlib.nullcontext():
            answer = getattr(commands, tool)(
                stored, query="lighthouse", limit=15, graph=False, rerank=False
            )
        assert answer
        return len(opened)

    few = snapshots_for_one_call(20)
    many = snapshots_for_one_call(60)

    assert (few, many) == (1, 1)


#: When set, the no-scan test writes each recall reader's median cold and warm milliseconds to this
#: file: the drill that tasks.md 3.2 records, run with ``EXOMEM_READERS_DRILL_PAGES`` at scale.
DRILL_REPORT = os.environ.get("EXOMEM_READERS_DRILL_REPORT")
DRILL_REPEATS = int(os.environ.get("EXOMEM_READERS_DRILL_REPEATS", "5"))

#: The recall calls that the drill times, at the tool entry, as an agent makes them.
RECALL_CALLS = {
    "find hybrid": {"tool": "op_find", "query": "lighthouse", "graph": False, "rerank": False},
    "find keyword": {
        "tool": "op_find", "query": "lighthouse", "mode": "keyword", "graph": False, "rerank": False
    },
    "find browse": {"tool": "op_find", "query": "", "types": ["note"], "graph": False},
    "ask_memory": {"tool": "op_ask_memory", "query": "lighthouse"},
}


def commit_generated(writer, pages):  # noqa: F811
    """Commit the generated pages, 200 to a commit, as an import batch commits them."""
    for start in range(0, pages, 200):
        puts = tuple(
            write.PagePut(*generated_page(index)) for index in range(start, min(pages, start + 200))
        )
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "drill", puts=puts))


def recall(root, name):
    call = dict(RECALL_CALLS[name])
    answer = getattr(commands, call.pop("tool"))(root, limit=15, **call)
    return answer["hits"] if isinstance(answer, dict) else answer


def drill_figures(root):
    """The median cold and warm milliseconds of each recall call: cold after ``clear_cache``."""
    import statistics
    import time

    figures = {}
    for name in RECALL_CALLS:
        cold, warm = [], []
        for _ in range(DRILL_REPEATS):
            find_module.clear_cache()
            for times in (cold, warm):
                started = time.perf_counter()
                recall(root, name)
                times.append((time.perf_counter() - started) * 1000)
        figures[name] = {
            "cold_ms": round(statistics.median(cold)),
            "warm_ms": round(statistics.median(warm)),
        }
    return figures


@pytest.mark.timeout(1800)
def test_a_store_mode_recall_call_parses_its_hits_and_lists_no_corpus(
    stored, writer, monkeypatch  # noqa: F811
):
    """The scan rung: a recall call that lists or parses every stored page costs about a
    millisecond a page, so a 10,000-page vault answers in seconds; and a cache that keys on the
    lexical rows that the migration copied keeps serving a page after a commit deletes it."""
    commit_generated(writer, GENERATED)
    if DRILL_REPORT:
        figures = drill_figures(stored)
        Path(DRILL_REPORT).write_text(json.dumps({"pages": GENERATED, **figures}, indent=2))
    parsed, listed = [], []
    real_parse = find_module.find_corpus.parse_page
    real_pages = read.StoreSnapshot.pages

    def parsing(*arguments, **keywords):
        parsed.append(1)
        return real_parse(*arguments, **keywords)

    def listing(view, prefix=""):
        if not prefix:
            listed.append(1)
        return real_pages(view, prefix)

    monkeypatch.setattr(find_module.find_corpus, "parse_page", parsing)
    monkeypatch.setattr(find_module, "_parse_page", parsing)
    monkeypatch.setattr(read.StoreSnapshot, "pages", listing)

    for name in ("find hybrid", "find keyword", "find browse"):
        find_module.clear_cache()
        parsed.clear()
        hits = recall(stored, name)
        assert hits, name
        # The release gate fetches a pool of candidates and keeps ``limit`` of them; the pool
        # depends on the limit, never on how many pages the store holds.
        assert len(parsed) <= egress.pool_limit(15), name
    assert listed == []

    gone = next(
        hit["path"]
        for hit in recall(stored, "find keyword")
        if hit["path"].startswith("Knowledge Base/Notes/Gen/")
    )
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "test", deletes=(write.PageDelete(gone),)),
    )
    assert gone not in [hit["path"] for hit in recall(stored, "find keyword")]


IDENTIFIED = "Knowledge Base/Notes/Gen/Identified.md"
IDENTITY = "0f5c3a52-7d1e-4b8a-9c1f-2a6e4d8b0c11"
#: When set, the index test writes each statement's ``EXPLAIN QUERY PLAN`` to this file: the plans
#: that tasks.md 3.2 records, run with ``EXOMEM_READERS_DRILL_PAGES`` at scale.
PLAN_REPORT = os.environ.get("EXOMEM_READERS_PLAN_REPORT")
#: The store's settings tables hold one row per setting, never one per page, so reading all of
#: them costs the same at any vault size.
SETTINGS_TABLES = {"meta", "store_meta", "store_mode"}


def full_reads(reader, sql):
    """The stored tables that ``sql`` reads from end to end, from its SQLite bytecode.

    A cursor that ``OpenRead`` opens on a table or an index and that ``Rewind`` or ``Last`` then
    starts at one end walks every row; an indexed lookup seeks instead. SQLite's opcode names are
    its documented, closed instruction set.
    """
    tables = {
        root: table
        for root, table in reader.execute(
            "SELECT rootpage, tbl_name FROM sqlite_schema WHERE rootpage > 0"
        )
    }
    code = reader.execute(f"EXPLAIN {sql}").fetchall()
    opened = {cursor: tables.get(root) for _addr, op, cursor, root, *_rest in code if op == "OpenRead"}
    return {
        opened[cursor]
        for _addr, op, cursor, *_rest in code
        if op in ("Rewind", "Last") and cursor in opened
    } - SETTINGS_TABLES


@pytest.mark.timeout(1800)
def test_a_store_mode_reader_finds_its_rows_through_an_index_once_its_admitted_set_is_read(
    stored, writer, monkeypatch  # noqa: F811
):
    """A dropped index, or a reader that adds a predicate no index covers, makes a 10,000-page
    vault answer in seconds and fails no other test: each answer is still correct."""
    commit_generated(writer, GENERATED)
    write.commit(
        writer,
        put(
            IDENTIFIED,
            f"---\ntitle: I\ntype: note\nexomem_id: {IDENTITY}\n---\n# I\n{UNIT}".encode(),
        ),
    )

    def read_through_each_reader():
        for name in ("find hybrid", "find keyword", "find browse"):
            assert recall(stored, name), name
        assert commands.op_ask_memory(stored, query="lighthouse", limit=15, graph=False)["hits"]
        assert find_module.find(stored, query="", categories=["constraint"], limit=15, graph=False)
        assert find_module.find(
            stored, query="lantern", result_level="unit", limit=15, graph=False
        )
        identified = memory_refs.resolve_identifier_read_only(
            stored, memory_refs.REF_PREFIX + IDENTITY
        )
        assert identified == IDENTIFIED
        assert memory_refs.ReferenceIndex(stored).refs_for_paths([IDENTIFIED])[IDENTIFIED]

    # The first pass reads each scope's admitted set, once per store revision, as file mode does.
    find_module.clear_cache()
    read_through_each_reader()
    find_module.reset_page_and_result_caches()
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
        read_through_each_reader()

    selects = sorted({sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))})
    assert selects, "the readers must have read the store"
    with contextlib.closing(connection.open_reader(connection.store_path(stored))) as reader:
        if PLAN_REPORT:
            plans = {
                sql: [row[-1] for row in reader.execute(f"EXPLAIN QUERY PLAN {sql}")]
                for sql in selects
            }
            Path(PLAN_REPORT).write_text(json.dumps({"pages": GENERATED, "plans": plans}, indent=1))
        scans = {sql: found for sql in selects if (found := full_reads(reader, sql))}
    assert scans == {}


def find_lighthouse(root):
    """A keyword find, with the timings that say whether the result cache answered it."""
    timings = FindTimings()
    find_module.find(root, query="lighthouse", mode="keyword", limit=15, timings=timings)
    return timings.cache.get("hit") is True


@pytest.mark.timeout(300)
def test_a_reader_serves_and_says_so_while_a_schema_change_is_applied_to_the_stored_pages(
    stored, writer, monkeypatch  # noqa: F811
):
    """A reader that refuses for a pass that ends in seconds, one that serves rows derived under
    the previous schema without saying so, or one that keeps serving an answer cached during the
    pass after the pass has derived the rows again."""
    repairs = []
    for seam in ("_schedule_repair", "request_repair", "_schedule_runtime_catalog_repair"):
        monkeypatch.setattr(
            lexstore, seam, lambda *args, _seam=seam, **kwargs: repairs.append(_seam)
        )
    write.commit(writer, put(PROFILES, b"profiles: {}\n"))
    find_module.clear_cache()

    found = commands.op_find(stored, query="lighthouse", limit=15, graph=False, rerank=False)
    asked = commands.op_ask_memory(stored, query="lighthouse", limit=15, graph=False)

    assert found["hits"] and asked["hits"]
    assert "warming" not in found and "warming" not in asked
    assert found["lag"]["registry"] == "rebuilding"
    assert found["lag"]["revision"] < found["lag"]["target"] == read.current_revision(stored)
    assert found["notice"] in asked["notice"]
    assert (find_lighthouse(stored), find_lighthouse(stored)) == (False, True)
    assert repairs == []

    project(writer, stored)
    settle_to_the_end(stored, writer)
    found = commands.op_find(stored, query="lighthouse", limit=15, graph=False, rerank=False)

    assert "lag" not in found and "notice" not in found
    assert find_lighthouse(stored) is False


ROOT_PAGE = "Harbour.md"
RECORD = "Knowledge Base/Records/Work/Tide Ledger.md"
RECORD_ID = "6b1d2e48-3c7a-4f59-8e20-9a4c5d7f1b36"
#: A one-shot process: no importer runs in it, and no memo of this test's process reaches it. It
#: prints its answer as the last line of its output.
ONE_SHOT = """
import json, sys
from pathlib import Path
from exomem import find, memory_refs
from exomem.find_types import FindTimings
from exomem.governance.principal import library_scope

root, record = Path(sys.argv[1]), sys.argv[2]
timings = FindTimings()
with library_scope():
    hits = find.find(root, query="cormorant", scope="vault", limit=15, graph=False, timings=timings)
    refs = memory_refs.ReferenceIndex(root).refs_for_paths([record])
paths = sorted(hit.path for hit in hits)
print(json.dumps({"paths": paths, "refs": refs, "lanes": timings.profile.get("lexical_lanes")}))
"""


def outside_page(title, text, exomem_id=None):
    identity = f"exomem_id: {exomem_id}\n" if exomem_id else ""
    return f"---\ntitle: {title}\ntype: note\n{identity}---\n# {title}\n\n{text}\n".encode()


@pytest.mark.timeout(300)
def test_a_file_that_the_store_does_not_own_is_answered_from_the_rows_that_its_sweep_wrote(
    stored, writer, monkeypatch  # noqa: F811
):
    """A reader that still sends the files that the store does not own to the file-mode sidecars,
    or an importer that gives a raw Records item no identity row, so that an outside edit is found
    only after a rebuild and a duplicate check misses the item."""
    importing = importer.Importer(stored, run=lambda work: work(writer))
    (stored / ROOT_PAGE).write_bytes(outside_page("Harbour", "The pilot boards at the breakwater."))
    (stored / RECORD).write_bytes(
        outside_page("Tide Ledger", "The harbour master logs each tide.", RECORD_ID)
    )
    assert importing.sweep().refreshed == [ROOT_PAGE, RECORD]
    (stored / ROOT_PAGE).write_bytes(outside_page("Harbour", "The pilot boards at the cormorant."))
    (stored / RECORD).write_bytes(
        outside_page("Tide Ledger", "The harbour master counts each cormorant.", RECORD_ID)
    )
    assert importing.import_paths([ROOT_PAGE, RECORD]).refreshed == [ROOT_PAGE, RECORD]

    def file_mode_route(*_args, **_kwargs):
        raise AssertionError("a store-mode reader opened the reference sidecar or read the files")

    monkeypatch.setattr(memory_refs.ReferenceIndex, "_connect", file_mode_route)
    monkeypatch.setattr(memory_refs, "_scan_pages", file_mode_route)
    find_module.clear_cache()
    timings = FindTimings()

    found = find_module.find(
        stored, query="cormorant", scope="vault", limit=15, graph=False, timings=timings
    )
    refs = memory_refs.ReferenceIndex(stored).refs_for_paths([RECORD])
    owner = memory_refs.resolve_identifier_read_only(stored, memory_refs.REF_PREFIX + RECORD_ID)
    swept_at = vault_store_mode.status(stored)["unowned_rows_as_of"]
    one_shot = subprocess.run(
        [sys.executable, "-c", ONE_SHOT, str(stored), RECORD],
        capture_output=True, text=True, check=True, timeout=120,
    )

    assert ROOT_PAGE in {hit.path for hit in found}
    assert refs == {RECORD: memory_refs.REF_PREFIX + RECORD_ID}
    assert owner == RECORD
    assert swept_at is not None
    assert timings.profile["lexical_lanes"]["unowned_rows_as_of"] == swept_at
    assert json.loads(one_shot.stdout.splitlines()[-1]) == {
        "paths": sorted(hit.path for hit in found),
        "refs": refs,
        "lanes": timings.profile["lexical_lanes"],
    }

    write.commit(writer, put(IDENTIFIED, outside_page("Identified", "A second holder.", RECORD_ID)))
    with pytest.raises(memory_refs.ReferenceError) as resolved:
        memory_refs.resolve_identifier_read_only(stored, memory_refs.REF_PREFIX + RECORD_ID)
    holders = [
        issue["path"]
        for issue in memory_refs.scan_issues(stored)
        if issue["kind"] == "duplicate" and issue["value"] == RECORD_ID
    ]

    assert resolved.value.code == "AMBIGUOUS_REFERENCE"
    assert holders == [IDENTIFIED, RECORD]


CLAIMED = "Knowledge Base/Notes/Gen/Claimed.md"


@pytest.mark.timeout(300)
def test_a_container_claim_is_read_from_the_stored_page_though_a_file_mode_read_remembered_none(
    vault, monkeypatch  # noqa: F811
):
    """A claim check that trusts what a file-mode read remembered of the projected file: the file
    never changed, so the memo says "no claim" for a page whose stored bytes now make one."""
    monkeypatch.setattr(vault_module, "GENERATION_RACY_WINDOW_NS", 50_000_000)
    plain = b"---\ntitle: C\ntype: note\n---\n# C\n\nNo claim yet.\n"
    (vault / CLAIMED).parent.mkdir(parents=True, exist_ok=True)
    (vault / CLAIMED).write_bytes(plain)
    build_indexes(vault)
    time.sleep(0.1)  # past the shortened racy window, so the read is remembered
    document_containers.clear_unclaimed_notes()
    assert document_containers.parent_binding_for_path(vault, CLAIMED) is None
    assert document_containers._remembered_unclaimed(vault, CLAIMED), "the read was remembered"

    assert migrate_apply(vault) == 0
    claim = b"---\ntitle: C\ntype: note\ncontainer_member: {version: 2}\n---\n# C\n\nA claim.\n"
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True) as handle:
        write.commit(CollectionWriter(vault, handle), put(CLAIMED, claim))

    assert (vault / CLAIMED).read_bytes() == plain, "no projection ran: the remembered file stays"
    assert egress.content_permits(vault, CLAIMED, EXTERNAL) is False


@pytest.mark.parametrize(
    ("registry", "loader", "entries"),
    (
        (context_roles, "load_roles", "roles: {}\n"),
        (activation_conventions, "load_conventions", ""),
    ),
    ids=("context roles", "activation conventions"),
)
def test_a_registry_override_is_read_from_the_stored_page_when_its_file_is_gone(
    stored, writer, registry, loader, entries  # noqa: F811
):
    """A loader that reads the override's file: in store mode that file is a projection that may
    lag or be absent, so the owner's override silently falls back to the shipped registry."""
    path = registry.override_path(stored)
    body = f"schema_version: {registry.SCHEMA_VERSION}\n{entries}".encode()
    write.commit(writer, put(path.relative_to(stored).as_posix(), body))
    path.unlink(missing_ok=True)
    registry.clear_cache()

    assert getattr(registry, loader)(stored).source == "vault"


@pytest.mark.timeout(300)
def test_a_governed_write_and_a_reconcile_in_store_mode_start_no_file_mode_repair(
    served, monkeypatch  # noqa: F811
):
    """A store-mode write or reconcile that re-enters the file-mode repair ladder: it rebuilds, from
    the projected files, a catalogue, graph or reference sidecar that no store-mode reader reads."""
    repairs = []
    for owner, seam in (
        (lexstore, "_schedule_runtime_catalog_repair"),
        (epistemic_graph, "schedule_background_rebuild"),
        (memory_refs, "request_rebuild"),
    ):
        monkeypatch.setattr(owner, seam, lambda *args, _seam=seam, **kwargs: repairs.append(_seam))

    created = commands.op_remember(
        served,
        title="Cormorant count",
        status="draft",
        content="## Observations\n\n- [finding] The cormorants roost on the breakwater.\n",
        summary="A count for the test rig.",
    )["path"]
    reconcile.reconcile(served)

    assert read.owned_bytes(served, created)
    assert repairs == []
    assert lexstore.get_store(served)._stranded_scopes == set()


def test_a_store_page_tagged_into_a_restricted_scope_is_withheld_whatever_its_projected_file_says(
    stored, writer  # noqa: F811
):
    """A decision read from the projected file releases a page that the store already holds in a
    restricted scope (the file still has the old tags, or none), and withholds an open page whose
    file has not been written yet."""
    restrict_external(stored, 'tags: ["secret"]')
    restricted = "Knowledge Base/Notes/Gen/Restricted.md"
    open_page = "Knowledge Base/Notes/Gen/Open.md"
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED,
            "test",
            puts=(
                write.PagePut(
                    restricted,
                    b"---\ntitle: R\ntype: note\ntags: [secret]\n---\n# R\n\nHeld back.\n",
                ),
                write.PagePut(open_page, b"---\ntitle: O\ntype: note\n---\n# O\n\nOpen.\n"),
            ),
        ),
    )
    # The restricted page's file is a stale projection from before the retag; the open page has
    # none.
    (stored / restricted).parent.mkdir(parents=True, exist_ok=True)
    (stored / restricted).write_bytes(b"---\ntitle: R\ntype: note\n---\n# R\n\nHeld back.\n")
    hits = [
        Hit(path=rel, type="note", scope="proj", title="t", updated="2026-01-01", excerpt="e",
            bm25_rank=1, vector_score=0.5)
        for rel in (restricted, open_page)
    ]

    released = egress.annotate_hits(
        stored, hits, principal=RequestPrincipal(audience_id="external", surface="mcp"), limit=5
    )

    assert [hit.path for hit in released.hits] == [open_page]


@pytest.mark.parametrize("link_into", ("the vault", "outside the vault"))
def test_a_stored_page_in_a_paths_scope_is_withheld_whatever_its_projection_is_linked_to(
    stored, writer, tmp_path_factory, link_into  # noqa: F811
):
    """Scope membership that names the projected file's link target decides the page under another
    path: a link into the vault names the target's path, and a link out of it names an absolute path
    that no `paths:` selector matches. Either way the page leaves its scope."""
    restrict_external(stored, 'paths: ["Knowledge Base/Notes/Secret/**"]')
    secret = "Knowledge Base/Notes/Secret/S.md"
    decoy = "Knowledge Base/Notes/Gen/Decoy.md"
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED,
            "test",
            puts=(
                write.PagePut(
                    secret, b"---\ntitle: S\ntype: note\n---\n# S\n\nThe zebracorn stays here.\n"
                ),
                write.PagePut(decoy, b"---\ntitle: D\ntype: note\n---\n# D\n\nA public decoy.\n"),
            ),
        ),
    )
    (stored / decoy).parent.mkdir(parents=True, exist_ok=True)
    (stored / decoy).write_bytes(b"---\ntitle: D\ntype: note\n---\n# D\n\nA public decoy.\n")
    target = stored / decoy
    if link_into == "outside the vault":
        target = tmp_path_factory.mktemp("outside") / "elsewhere.md"
        target.write_bytes(b"---\ntitle: E\ntype: note\n---\n# E\n\nElsewhere.\n")
    (stored / secret).parent.mkdir(parents=True, exist_ok=True)
    (stored / secret).unlink(missing_ok=True)
    (stored / secret).symlink_to(target)

    with request_scope(EXTERNAL):
        found = commands.op_find(stored, query="zebracorn", mode="keyword", limit=10)
        asked = commands.op_ask_memory(stored, query="zebracorn", limit=10)
        listed = commands.op_browse_memory(
            stored, path="Knowledge Base/Notes", mode="list", recursive=True
        )

    assert "Decoy" in json.dumps(listed, default=str)  # the listing itself was served
    for answer in (found, asked, listed):
        assert "zebracorn" not in json.dumps(answer, default=str)
        assert "Secret/S.md" not in json.dumps(answer, default=str)


@pytest.mark.timeout(300)
def test_a_stored_page_with_an_invalid_container_claim_is_refused_as_in_file_mode(
    vault, monkeypatch  # noqa: F811
):
    """A claim check that probes the projected file before the identity inventory exists reads a
    missing projection as a page with no claim, so a page that file mode refuses is released."""
    claim = "Knowledge Base/Notes/Gen/Claim.md"
    (vault / claim).parent.mkdir(parents=True, exist_ok=True)
    (vault / claim).write_bytes(
        b"---\ntitle: C\ntype: note\ncontainer_member: {version: 2}\n---\n# C\n\nA claim.\n"
    )
    build_indexes(vault)

    def permitted():
        reserved_paths._BASELINE_IDENTITY_CATALOGUES.clear()  # no inventory is built yet
        document_containers.clear_unclaimed_notes()
        return egress.content_permits(vault, claim, EXTERNAL)

    in_file_mode = permitted()
    assert in_file_mode is False
    assert migrate_apply(vault) == 0
    (vault / claim).unlink()

    assert permitted() is in_file_mode


def test_a_decision_receipt_hashes_the_stored_page_not_its_projected_file(
    stored, writer  # noqa: F811
):
    """A receipt that hashes a lagging projection records bytes that no reader was served, and a
    store-only page loses its hash."""
    page = "Knowledge Base/Notes/Gen/Receipted.md"
    body = b"---\ntitle: Re\ntype: note\n---\n# Re\n\nStored bytes.\n"
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "test", puts=(write.PagePut(page, body),)),
    )

    assert egress._receipt_bytes(stored, page) == body  # no projected file yet
    (stored / page).parent.mkdir(parents=True, exist_ok=True)
    (stored / page).write_bytes(b"stale projection")
    assert egress._receipt_bytes(stored, page) == body


def test_warm_and_status_leave_no_sidecar_and_status_reports_the_store(
    stored, capsys  # noqa: F811
):
    """A warm that builds the lexical sidecar from the projected files in a vault whose pages are
    in the store, or a status that reports nothing of the mode the vault is in."""
    sidecar = lexstore.lexical_path(stored)
    sidecar.unlink(missing_ok=True)
    present = {path for path in stored.rglob("*") if path.is_file()}

    assert warmup.warm_caches(stored, preload_models=False, preload_cpu_caches=True) is not None
    capsys.readouterr()
    assert cli._status_main(["--vault", str(stored), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert not sidecar.exists()
    assert {path for path in stored.rglob("*") if path.is_file()} == present
    assert report["vault_store"]["mode"] == "store"


def test_a_registry_overlay_that_only_the_store_holds_loads_and_follows_its_commits(
    stored, writer  # noqa: F811
):
    """An overlay loader that reads the file's stat takes an overlay with no file for none, so a
    store-mode vault runs on the shipped pack alone."""
    rel = "Knowledge Base/_Schema/project-keys.yaml"
    (stored / rel).unlink()
    assert not (stored / rel).exists()

    write.commit(writer, put(rel, b"projects:\n  harbour:\n    folder: Harbour\n"))
    assert "harbour" in project_keys.load_project_registry(stored).project_to_folder

    write.commit(writer, put(rel, b"projects:\n  quay:\n    folder: Quay\n"))
    keys = project_keys.load_project_registry(stored).project_to_folder
    assert "quay" in keys and "harbour" not in keys


def test_a_note_type_overlay_that_only_the_store_holds_reaches_the_registry_loader(
    stored, writer  # noqa: F811
):
    """The shared registry loader, which keys its cache on the overlay file: it serves the shipped
    pack for an overlay that the store holds and no file does, and keeps serving it after a
    commit."""
    from exomem import note_types

    rel = "Knowledge Base/_Schema/note-types.yaml"
    assert not (stored / rel).exists()
    pack = registry.load(note_types.SPEC, stored)
    document = (
        b"schema_version: 1\nentries:\n  harbour-log:\n    label: Harbour log\n"
        b"    description: A log of the harbour.\n"
        b"    attributes:\n      role: compiled\n      folder: Notes/Harbour\n"
    )

    write.commit(writer, put(rel, document))
    loaded = registry.load(note_types.SPEC, stored)

    assert "harbour-log" in loaded.entries
    assert "harbour-log" not in pack.entries
    assert loaded.content_hash != pack.content_hash
    assert vault_module.read_bytes_without_pinning(stored / rel, stored) == document


def test_the_terminal_ui_counts_the_stored_notes_and_starts_no_file_warm_up(
    stored, writer, monkeypatch  # noqa: F811
):
    """A session that starts the file warm-up in a store-mode vault builds its catalogue and drain
    queues from files that the vault does not have, and its home screen counts a note that only the
    store holds as none."""
    from exomem.tui.backend import ExomemBackend

    started = []
    monkeypatch.setattr(warmup, "start_background", lambda root: started.append(root))
    # The lean fallback sets process-wide environment variables, which this test must not leak.
    monkeypatch.setattr(ExomemBackend, "_apply_lean_fallback", staticmethod(lambda: None))
    backend = ExomemBackend(str(stored))
    assert backend.resolve_vault().initialized
    notes = backend.corpus_size()["notes"]

    write.commit(writer, put(NEW, EDITED))
    backend.start_runtime()

    assert not (stored / NEW).exists()
    after = backend.corpus_size()
    assert (after["notes"], after["known"]) == (notes + 1, True)
    assert started == []


def test_a_store_mode_listing_hides_only_a_disk_folder_that_holds_nothing_but_stray_pages(
    stored, writer  # noqa: F811
):
    """A listing that takes every folder on disk for a folder of the vault, or that hides a folder a
    person made: a stray Markdown file at a path that the store owns is no page, so a folder of
    only such files is not there. An empty folder, one that holds a binary and one that holds a
    stored page are."""
    notes = stored / "Knowledge Base/Notes"
    (notes / "Stray").mkdir()
    (notes / "Stray/stray.md").write_bytes(b"a file that the store never held\n")
    (notes / "Pictures").mkdir()
    (notes / "Pictures/photo.bin").write_bytes(b"\x00\x01")
    (notes / "Empty").mkdir()
    (notes / "Nested/Empty").mkdir(parents=True)
    write.commit(writer, put("Knowledge Base/Notes/Fresh/Page.md", EDITED))

    listing = list_directory.list_directory(stored, path="Knowledge Base/Notes", recursive=True)
    names = {entry.path.removeprefix("Knowledge Base/Notes/") for entry in listing.entries}

    assert {"Pictures", "Pictures/photo.bin", "Fresh", "Fresh/Page.md"} <= names
    assert {"Empty", "Nested", "Nested/Empty"} <= names
    assert not {name for name in names if name.startswith("Stray")}


def test_a_page_the_store_deleted_is_not_found_though_a_link_sits_at_its_name(
    stored, writer  # noqa: F811
):
    """A page read that follows a link at the deleted page's name returns the bytes of the page the
    link points to, as if the deleted page were still there."""
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "test", deletes=(write.PageDelete(ALPHA),)),
    )
    (stored / ALPHA).unlink(missing_ok=True)
    (stored / ALPHA).symlink_to(stored / BETA)

    with pytest.raises(get_page.GetError) as missing:
        get_page.get_page(stored, path=ALPHA)

    assert missing.value.code == "NOT_FOUND"
    assert get_page.get_page(stored, path=BETA).content


def test_a_broken_state_root_leaves_a_file_vault_a_vault_and_a_bare_folder_not_one(
    stored  # noqa: F811
):
    """A vault probe that asks the store before the disk: a broken state root then fails every
    start of a vault whose sentinel is on disk, or raises for a directory that is no vault."""
    from exomem import state_migration, state_paths

    manifest = state_migration._manifest_path(state_paths.vault_state_dir(stored))
    manifest.write_bytes(b"not a manifest")

    assert vault_module._is_vault(stored)
    for sentinel in stored.rglob(vault_module._SCHEMA_SENTINEL):
        sentinel.unlink()
    assert not vault_module._is_vault(stored)
