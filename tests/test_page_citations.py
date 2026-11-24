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

from exomem import freshness, page_citations, record_governance, semantic_contract
from exomem import structured_collections as collections
from exomem.collection_store import connection
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import reference_resolution, runtime
from exomem.vault import walk_vault_md

OTHER_LOG = "ffffffff-ffff-4fff-8fff-ffffffffffff"
ALPHA_REF = f"exomem://memory/{ALPHA}"
BETA_REF = f"exomem://memory/{BETA}"
AWAY = "exomem://memory/99999999-9999-4999-8999-999999999999"


@pytest.fixture
def world(store, monkeypatch, corpus_cache_on):
    monkeypatch.setattr(connection, "store_path", lambda root: store.handle.path)
    page(store.root, "Notes/Alpha.md", "Alpha", ALPHA)
    page(store.root, "Notes/Beta.md", "Beta", BETA)
    store.create_collection(path("Log"), log_text(), why="declare", scaffold=False)
    publish(store.root)
    return store


def publish(root):
    """Publish the corpus generation that a running service keeps current; a citation read borrows it to find pages.

    The watcher's seed comes first, as a service's does, so the generation counts as live.
    """
    freshness.seed(root, "vault", ((str(found), freshness.stat_signature(found)) for found in walk_vault_md(root)))
    semantic_contract.build_corpus_context(root)


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
    publish(world.root)
    append(world, {"page": f"exomem://memory/{HIDDEN}"})
    _ready(world)
    with request_scope(owner_principal()):
        assert page_citations.inbound_citations(f"exomem://memory/{HIDDEN}", world.root)["count"] == 1
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


def test_a_reader_whom_no_row_rule_names_counts_every_row_without_deciding_one(world, monkeypatch):
    """The scope that names one row carries a rule for the external audience only, so for the owner no row differs."""
    for key in (K1, K2, K3):
        append(world, {"page": ALPHA_REF}, key=key)
    _ready(world)
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{collections.record_ref(LOG, K2)}"]\n')
    write_rule(world.root, ceiling=0)
    decided = []
    batch = reference_resolution.rows_released

    def counted(session, lookup, row_ids):
        decided.append(len(row_ids))
        return batch(session, lookup, row_ids)

    monkeypatch.setattr(reference_resolution, "rows_released", counted)
    assert cited(world)["count"] == 3 and decided == []
    assert cited(world, who=_external())["count"] == 2 and decided == [3]


def traced_statements(monkeypatch) -> list[str]:
    """The statements every citation read runs from now on, in order."""
    statements: list[str] = []
    opened = runtime.read_session

    @contextlib.contextmanager
    def traced(*args, **kwargs):
        with opened(*args, **kwargs) as session:
            session.connection.set_trace_callback(statements.append)
            yield session

    monkeypatch.setattr(runtime, "read_session", traced)
    return statements


def test_a_batch_reads_the_index_twice_for_all_its_pages(world, monkeypatch):
    """One probe finds the fields that cite any asked page, and one read lists their entries, however many pages."""
    append(world, {"page": ALPHA_REF}, key=K1)
    append(world, {"page": BETA_REF, "pages": [ALPHA_REF]}, key=K2)
    _ready(world)
    statements = traced_statements(monkeypatch)
    with request_scope(owner_principal()):
        replies = page_citations.inbound_citations_batch([ALPHA_REF, BETA_REF, AWAY], world.root, per_ref=5)
    assert sum("item_references r" in sql for sql in statements) == 2
    assert {ref: reply["count"] for ref, reply in replies.items()} == {ALPHA_REF: 2, BETA_REF: 1, AWAY: 0}


def test_a_read_runs_the_same_statements_however_many_governed_rows_cite_the_page(world, monkeypatch):
    """A policy that decides rows one by one costs one subject read per collection, never a statement per citing row."""
    def key(number):
        return f"00000000-0000-4000-8000-{number:012d}"

    for number in range(3):
        append(world, {"page": ALPHA_REF}, key=key(number))
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{collections.record_ref(LOG, key(1))}"]\n')
    write_rule(world.root, ceiling=0)
    statements = traced_statements(monkeypatch)

    def measured_read():
        _ready(world)
        cited(world, who=_external(), limit=1)  # warm the caches a running service keeps warm
        statements.clear()
        return cited(world, who=_external(), limit=1)["count"], len(statements)

    few = measured_read()
    for number in range(3, 40):
        append(world, {"page": ALPHA_REF}, key=key(number))
    many = measured_read()
    assert (few[0], many[0]) == (2, 39)
    assert few[1] == many[1]


def test_with_no_published_corpus_to_borrow_a_read_is_unavailable_never_a_walk_or_an_empty_list(world, monkeypatch):
    append(world, {"page": ALPHA_REF})
    _ready(world)
    freshness.clear()
    semantic_contract.reset_corpus_context_cache()

    def walked(*args, **kwargs):
        raise AssertionError("a citation read walked the vault")

    monkeypatch.setattr(record_governance._LinkProjector, "_walk_candidate_index", walked)
    assert cited(world) == {"state": "unavailable"}


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
