"""The inbound-citation reader answers who cites a page, as the caller may see it.

The fixtures reuse the link-values log: a record collection whose `page` field links to pages by ref.
"""

import contextlib

import pytest
from test_collection_link_values import (
    ALPHA,
    BETA,
    HIDDEN,
    K1,
    K2,
    K3,
    LOG,
    _ready,
    append,
    log_text,
    page,
)
from test_collection_row_references import collection_text, path
from test_collection_store_writer import store as store  # noqa: F401
from test_governance_egress import _external, write_rule, write_scope

from exomem import page_citations
from exomem import structured_collections as collections
from exomem.collection_store import connection
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import runtime

OTHER_LOG = "ffffffff-ffff-4fff-8fff-ffffffffffff"
ALPHA_REF = f"exomem://memory/{ALPHA}"
BETA_REF = f"exomem://memory/{BETA}"
AWAY = "exomem://memory/99999999-9999-4999-8999-999999999999"


@pytest.fixture
def world(store, monkeypatch):
    monkeypatch.setattr(connection, "store_path", lambda root: store.handle.path)
    page(store.root, "Notes/Alpha.md", "Alpha", ALPHA)
    page(store.root, "Notes/Beta.md", "Beta", BETA)
    store.create_collection(path("Log"), log_text(), why="declare", scaffold=False)
    return store


def cited(store, who=None, **arguments):
    with request_scope(who or owner_principal()):
        return page_citations.inbound_citations(ALPHA_REF, store.root, **arguments)


def test_a_ready_page_lists_each_citing_row_with_what_a_reader_needs(world):
    append(world, {"page": ALPHA_REF}, key=K1)
    append(world, {"page": "Alpha"}, key=K2)
    append(world, {"page": BETA_REF}, key=K3)
    _ready(world)
    reply = cited(world, limit=1)
    assert (reply["state"], reply["count"]) == ("ready", 2)
    [first] = reply["items"]
    assert first == {"profile": "records", "collection": LOG, "collection_title": "Log", "label": "c1",
                     "ref": f"exomem://record/{LOG}/{K1}", "field": "page", "relation": "cites"}
    second = cited(world, limit=1, more=reply["more"])
    assert [item["label"] for item in second["items"]] == ["c2"]
    assert second["count"] == 2 and second["more"] is None


def test_a_warming_index_never_reads_as_an_empty_list(world):
    append(world, {"page": ALPHA_REF})
    assert cited(world) == {"state": "warming"}
    _ready(world)
    assert cited(world)["count"] == 1


def test_a_withheld_page_answers_exactly_like_a_missing_one(world):
    page(world.root, "Private/Secret.md", "Secret", HIDDEN)
    append(world, {"page": f"exomem://memory/{HIDDEN}"})
    _ready(world)
    (world.root / "Knowledge Base/_access.yaml").write_text('excluded: ["Private/Secret.md"]\n')
    with request_scope(owner_principal()):
        withheld = page_citations.inbound_citations(f"exomem://memory/{HIDDEN}", world.root)
        missing = page_citations.inbound_citations(AWAY, world.root)
    assert withheld == missing == {"state": "ready", "items": [], "count": 0, "more": None}


def test_a_row_of_a_collection_the_caller_cannot_read_is_absent_from_items_and_count(world):
    world.create_collection(path("Other"), collection_text(OTHER_LOG, "Other", "label", "    label: {type: string, required: true}\n"
                                                           "    page: {type: link}\n"),
                            why="declare", scaffold=False)
    append(world, {"page": ALPHA_REF}, key=K1)
    with request_scope(owner_principal()):
        world.append_record(OTHER_LOG, item={"label": "o", "page": ALPHA_REF}, item_key=K2, why="record")
    _ready(world)
    _ready(world, OTHER_LOG)
    assert cited(world)["count"] == 2
    write_scope(world.root, paths="Knowledge Base/Records/Other/**")
    write_rule(world.root, ceiling=0)
    seen = cited(world, who=_external())
    assert seen["count"] == 1 and [item["collection"] for item in seen["items"]] == [LOG]


def test_a_withheld_row_of_a_collection_the_caller_can_read_is_absent_from_items_and_count(world):
    append(world, {"page": ALPHA_REF}, key=K1)
    append(world, {"page": ALPHA_REF}, key=K2)
    _ready(world)
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{collections.record_ref(LOG, K2)}"]\n')
    write_rule(world.root, ceiling=0)
    seen = cited(world, who=_external())
    assert seen["count"] == 1 and [item["label"] for item in seen["items"]] == ["c1"]


def test_a_batch_reads_the_index_once_for_all_its_pages(world, monkeypatch):
    append(world, {"page": ALPHA_REF}, key=K1)
    append(world, {"page": BETA_REF, "pages": [ALPHA_REF]}, key=K2)
    _ready(world)
    statements = []
    opened = runtime.read_session

    @contextlib.contextmanager
    def traced(*args, **kwargs):
        with opened(*args, **kwargs) as session:
            session.connection.set_trace_callback(statements.append)
            yield session

    monkeypatch.setattr(runtime, "read_session", traced)
    with request_scope(owner_principal()):
        replies = page_citations.inbound_citations_batch([ALPHA_REF, BETA_REF, AWAY], world.root, per_ref=5)
    assert sum("item_references r" in sql for sql in statements) == 1
    assert {ref: reply["count"] for ref, reply in replies.items()} == {ALPHA_REF: 2, BETA_REF: 1, AWAY: 0}


def test_a_value_that_is_not_a_ref_is_unavailable_never_an_empty_list(world):
    with request_scope(owner_principal()):
        reply = page_citations.inbound_citations("Alpha", world.root)
    assert reply == {"state": "unavailable"}


def test_a_row_is_cited_by_the_rows_that_link_to_it_and_a_withheld_row_answers_like_a_missing_one(world):
    target = collections.record_ref(LOG, K1)
    append(world, {"pages": [target]}, key=K2)
    _ready(world)

    def asked(ref, who):
        with request_scope(who):
            return page_citations.inbound_citations(ref, world.root)

    append(world, {}, key=K1)
    assert [item["label"] for item in asked(target, owner_principal())["items"]] == ["c2"]
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{target}"]\n')
    write_rule(world.root, ceiling=0)
    missing = collections.record_ref(LOG, "00000000-0000-4000-8000-0000000000ee")
    assert asked(target, _external()) == asked(missing, _external()) == {
        "state": "ready", "items": [], "count": 0, "more": None}
