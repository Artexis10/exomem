"""`search_memory` hits and `recall_context` leads show how many rows and plan items cite each page.

The fixtures give the writer-test vault the Cargo Sled structure the recall tests share, one of its pages
carrying an `exomem_id`, and the link-values log whose rows cite pages.
"""

import contextlib

import pytest
from test_activate_context_maps import _activate
from test_activate_context_turn_slice import CARGO_SLED, SLED_TURN
from test_collection_link_values import ALPHA, K1, K2, K3, LOG, _ready, append, log_text, page
from test_collection_row_references import collection_text, path
from test_collection_store_writer import store as store  # noqa: F401
from test_context_catalogue import _built
from test_governance_egress import _external, write_rule, write_scope
from test_page_citations import publish
from test_working_set_index import _seed_structure

from exomem import commands, lexstore, page_citations
from exomem.collection_store import connection
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import runtime

OTHER_LOG = "ffffffff-ffff-4fff-8fff-ffffffffffff"
SLED_REF = f"exomem://memory/{ALPHA}"


@pytest.fixture
def world(store, monkeypatch, corpus_cache_on):
    monkeypatch.setattr(connection, "store_path", lambda root: store.handle.path)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _seed_structure(store.root)
    sled = store.root / CARGO_SLED
    sled.write_text(sled.read_text().replace("type: note", f"exomem_id: {ALPHA}\ntype: note", 1))
    _built(store.root).close()
    lexstore.ensure_fresh(store.root)
    store.create_collection(path("Log"), log_text(), why="declare", scaffold=False)
    publish(store.root)
    return store


def search(world, who=None, query="Cargo Sled"):
    with request_scope(who or owner_principal()):
        return commands.op_ask_memory(world.root, query=query, mode="keyword")["hits"]


def sled_hit(hits):
    [hit] = [hit for hit in hits if hit.get("ref") == SLED_REF]
    return hit


def test_a_page_hit_shows_its_citation_count_and_its_first_citations_never_the_citing_rows_as_hits(world):
    for key in (K1, K2, K3):
        append(world, {"page": SLED_REF}, key=key)
    _ready(world)
    hits = search(world)
    hit = sled_hit(hits)
    assert hit["cited_by_count"] == 3
    assert [item["label"] for item in hit["cited_by"]] == ["c1", "c2", "c3"]
    assert {key: hit["cited_by"][0][key] for key in ("collection_title", "field")} == {
        "collection_title": "Log", "field": "page"}
    assert not [other for other in hits if str(other.get("ref")).startswith(("exomem://record/", "exomem://plan/"))]


def test_a_warming_index_says_so_on_the_hit_instead_of_a_zero_count(world):
    append(world, {"page": SLED_REF})
    hit = sled_hit(search(world))
    assert hit["cited_by_state"] == "warming" and "cited_by_count" not in hit


def test_a_collection_the_caller_cannot_read_adds_nothing_to_a_hits_citations(world):
    world.create_collection(path("Other"), collection_text(OTHER_LOG, "Other", "label", "    label: {type: string, required: true}\n"
                                                           "    page: {type: link}\n"),
                            why="declare", scaffold=False)
    append(world, {"page": SLED_REF}, key=K1)
    with request_scope(owner_principal()):
        world.append_record(OTHER_LOG, item={"label": "o", "page": SLED_REF}, item_key=K2, why="record")
    _ready(world)
    _ready(world, OTHER_LOG)
    assert sled_hit(search(world))["cited_by_count"] == 2
    write_scope(world.root, paths="Knowledge Base/Records/Other/**")
    write_rule(world.root, ceiling=0)
    seen = sled_hit(search(world, who=_external()))
    assert seen["cited_by_count"] == 1 and [item["label"] for item in seen["cited_by"]] == ["c1"]


def test_a_row_in_a_collection_the_caller_cannot_read_leaves_a_page_as_if_no_row_cited_it(world):
    """The cited page and a page nothing cites answer alike, on a page read and on a recall hit."""
    spare = "Notes/Cargo Sled Spare.md"
    page(world.root, spare, "Cargo Sled Spare", "00000000-0000-4000-8000-000000000199")
    publish(world.root)
    world.create_collection(path("Other"), collection_text(OTHER_LOG, "Other", "label", "    label: {type: string, required: true}\n"
                                                           "    page: {type: link}\n"),
                            why="declare", scaffold=False)
    with request_scope(owner_principal()):
        world.append_record(OTHER_LOG, item={"label": "o", "page": SLED_REF}, item_key=K1, why="record")
    _ready(world)
    _ready(world, OTHER_LOG)
    write_scope(world.root, paths="Knowledge Base/Records/Other/**")
    write_rule(world.root, ceiling=0)
    outsider = _external()
    with request_scope(outsider):
        cited_page = commands.op_read_memory(world.root, CARGO_SLED)["cited_by"]
        uncited_page = commands.op_read_memory(world.root, spare)["cited_by"]
    assert cited_page == uncited_page == {"items": [], "count": 0, "more": None, "state": "ready"}
    hits = search(world, who=outsider)
    [uncited_hit] = [hit for hit in hits if hit.get("title") == "Cargo Sled Spare"]
    assert sled_hit(hits)["cited_by_count"] == uncited_hit["cited_by_count"] == 0
    assert "cited_by" not in sled_hit(hits) and "cited_by" not in uncited_hit


def test_a_response_reads_the_index_twice_however_many_page_hits_it_has(world, monkeypatch):
    for number in range(12):
        page(world.root, f"Notes/Sled {number:02}.md", f"Sled {number:02}", f"00000000-0000-4000-8000-0000000001{number:02}")
    publish(world.root)
    append(world, {"page": "exomem://memory/00000000-0000-4000-8000-000000000105"}, key=K1)
    _ready(world)
    statements = []
    opened = runtime.read_session

    @contextlib.contextmanager
    def traced(*args, **kwargs):
        with opened(*args, **kwargs) as session:
            session.connection.set_trace_callback(statements.append)
            yield session

    monkeypatch.setattr(runtime, "read_session", traced)
    hits = search(world, query="Sled")
    assert len(hits) > 10
    assert sum("item_references r" in sql for sql in statements) == 2  # one probe and one read for every hit
    assert sum("cited_by_count" in hit for hit in hits) == 10


def test_a_recall_lead_shows_its_citations_inside_the_packets_budget(world):
    append(world, {"page": SLED_REF}, key=K1)
    _ready(world)
    packet = _activate(world.root, turn=SLED_TURN)
    (lead,) = [item for item in packet["turn_slice"]["items"] if item["kind"] == "anchor"]
    assert lead["cited_by_count"] == 1 and [item["label"] for item in lead["cited_by"]] == ["c1"]
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]
    # Citations are read per request: the next turn sees the row saved since, as the served packet is not cached with them.
    append(world, {"page": SLED_REF}, key=K2)
    (again,) = [item for item in _activate(world.root, turn=SLED_TURN)["turn_slice"]["items"] if item["kind"] == "anchor"]
    assert again["cited_by_count"] == 2


def test_a_rule_for_the_declared_purpose_narrows_citations_on_a_page_read_a_search_and_a_recall(world):
    """A rule that withholds one row only for a purpose drops that row wherever the caller declares the purpose."""
    append(world, {"page": SLED_REF}, key=K1)
    append(world, {"page": SLED_REF}, key=K2)
    _ready(world)
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["exomem://record/{LOG}/{K2}"]\n')
    write_rule(world.root, ceiling=0, extra="purpose: marketing\npurpose_condition: matches\n")
    outsider = _external()
    with request_scope(outsider):
        undeclared = commands.op_read_memory(world.root, CARGO_SLED)["cited_by"]
        declared = commands.op_read_memory(world.root, CARGO_SLED, purpose="marketing")["cited_by"]
        found = commands.op_ask_memory(world.root, query="Cargo Sled", mode="keyword", purpose="marketing")["hits"]
    assert undeclared["count"] == 2
    assert declared["count"] == 1 and [item["label"] for item in declared["items"]] == ["c1"]
    assert sled_hit(found)["cited_by_count"] == 1
    (lead,) = [item for item in _activate(world.root, outsider, turn=SLED_TURN, purpose="marketing")["turn_slice"]["items"]
               if item["kind"] == "anchor"]
    assert lead["cited_by_count"] == 1 and [item["label"] for item in lead["cited_by"]] == ["c1"]


def test_a_fault_in_the_citation_reader_leaves_the_page_read_and_the_search_whole(world, monkeypatch):
    append(world, {"page": SLED_REF}, key=K1)
    _ready(world)

    def broken(*args, **kwargs):
        raise RuntimeError("citation reader fault")

    monkeypatch.setattr(page_citations, "_read", broken)
    with request_scope(owner_principal()):
        read = commands.op_read_memory(world.root, CARGO_SLED)
    assert read["body"] and read["cited_by"] == {"state": "unavailable"}
    hit = sled_hit(search(world))
    assert hit["cited_by_state"] == "unavailable" and "cited_by_count" not in hit
