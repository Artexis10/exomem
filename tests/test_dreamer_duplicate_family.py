"""The duplicate-fact family, `upkeep_duplicate`: a row and the page it links that state one fact twice.

The fixtures invent a services collection (natural key `name`, a free-text `location` and a `page` link) over
pages that carry an `exomem_id` the way captured pages do, and run the real dreamer tick over them.
"""

from __future__ import annotations

import json

import dreamer_fixture as fx
import pytest
from test_collection_link_values import _ready
from test_collection_row_references import collection_text, path
from test_collection_store_writer import store as store  # noqa: F401
from test_governance_egress import SCOPE_ID, _external, write_rule
from test_page_citations import publish

from exomem import dreamer, dreamer_families, dreamer_store, duplicate_facts, freshness, upkeep
from exomem.collection_store import connection
from exomem.governance import egress, membership, policy
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import runtime

SERVICES = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1"
SECRETS = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2"
GADGETS = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee3"
OTHER_PAGE = "33333333-3333-4333-8333-333333333333"
WIDGET = "11111111-1111-4111-8111-111111111111"
VAULT_DOOR = "22222222-2222-4222-8222-222222222222"
FIELDS = "    name: {type: string, required: true}\n    location: {type: string}\n    page: {type: link, relation: cites}\n"


def key(number):
    return f"00000000-0000-4000-8000-{number:012d}"


@pytest.fixture(autouse=True)
def _clean():
    def clear():
        freshness.clear()
        dreamer.reset_for_tests()
        dreamer_store.clear_reader_memo()
        policy._CACHE.clear()
        membership.clear_memo()
        egress.clear_decision_memo()

    clear()
    yield
    clear()


@pytest.fixture
def world(store, monkeypatch, tmp_path, corpus_cache_on):  # noqa: F811
    monkeypatch.setattr(connection, "store_path", lambda root: store.handle.path)
    vault = fx.build(tmp_path)
    store.create_collection(path("Services"), collection_text(SERVICES, "Services", "name", FIELDS),
                            why="declare", scaffold=False)
    return store, vault


def service_page(vault, rel, title, identity, *lines):
    body = "".join(f"- [finding] {line}\n" for line in lines)
    fx.edit(vault, f"Knowledge Base/{rel}",
            f"---\nexomem_id: {identity}\ntitle: {title}\n---\n# {title}\n\n## Observations\n\n{body}")


def row(store, collection, number, name, location, link):
    with request_scope(owner_principal()):
        return store.append_record(collection, item={"name": name, "location": location, "page": link},
                                   item_key=key(number), why="record")


def tick(vault):
    publish(vault)  # a citation read borrows the corpus generation that a running service keeps current
    results = fx.run_to_quiet(vault)
    assert all(result.stop_reason != "error" for result in results), results
    return results


def served(vault, who):
    with request_scope(who):
        return upkeep.review(vault, categories=[dreamer_families.DUPLICATE_FAMILY])["items"]


def store_state(store):
    conn = store.handle.connection
    return (conn.execute("SELECT max(txn_id) FROM txns").fetchone(),
            conn.execute("SELECT row_id, item_key, values_json FROM items ORDER BY row_id").fetchall())


def open_candidates(vault):
    conn = dreamer_store.DreamerStore(vault).connect()
    try:
        return [candidate for candidate in dreamer_store.DreamerStore.open_candidates(conn)
                if candidate["family"] == dreamer_families.DUPLICATE_FAMILY]
    finally:
        conn.close()


def test_a_restated_fact_opens_one_item_per_candidate_and_writes_nothing_else(world):
    """A job that opened an item on every tick would bury the owner in repeats, one that compared unresolved
    or heading-linked rows would walk the whole collection, and one that edited the page or row would rewrite
    what the owner wrote. A row written after the first tick must still be found with no page change."""
    store, vault = world
    service_page(vault, "Notes/Widget Service.md", "Widget Service", WIDGET,
                 "The Widget Service runs in the home office on a spare laptop.", "Backups finish before dawn.")
    link = f"exomem://memory/{WIDGET}"
    row(store, SERVICES, 1, "Widget Service", "home office", link)   # restated: the unit holds the value and the name
    row(store, SERVICES, 2, "Gadget Service", "garage", link)        # no unit holds this value
    row(store, SERVICES, 3, "Widget", "home office", "Nowhere Page")  # the link resolves to no page
    # A link to a heading is stored as written, never as the page's ref, so the row does not cite the page.
    anchored = row(store, SERVICES, 5, "Service", "home office", "[[Widget Service#Backups]]")
    assert any(text.startswith("LINK_ANCHORED") for text in anchored["warnings"])
    _ready(store, SERVICES)
    pages, rows = fx.tree_state(vault), store_state(store)
    tick(vault)
    [first] = served(vault, owner_principal())
    assert first["family"] == dreamer_families.DUPLICATE_FAMILY
    [stored] = open_candidates(vault)
    assert (stored["measures"]["field"], stored["measures"]["relation"]) == ("location", "duplicates")
    assert stored["measures"]["unit_ref"].startswith(link) and stored["measures"]["unit_ref"] in first["why"]
    assert "location" in first["why"] and first["why"].count("home office") == 2
    # The owner finds the row from the item: its ref and the label that names it.
    assert stored["measures"]["row_ref"] in first["why"] and '("Widget Service")' in first["why"]
    assert "home office" not in json.dumps(stored), "the shared sidecar must hold no row value"
    assert (fx.tree_state(vault), store_state(store)) == (pages, rows)

    tick(vault)
    assert [item["id"] for item in served(vault, owner_principal())] == [first["id"]]

    row(store, SERVICES, 4, "Backups", "before dawn", link)
    rows = store_state(store)
    tick(vault)
    assert first["id"] in {item["id"] for item in served(vault, owner_principal())}
    assert len(served(vault, owner_principal())) == 2
    assert (fx.tree_state(vault), store_state(store)) == (pages, rows)


def test_a_caller_never_sees_a_hidden_row_or_page_named_in_an_item(world):
    """The worker compares as the owner, so an item that carried a hidden collection's value or a hidden
    page's title would show a caller what they may not read."""
    store, vault = world
    store.create_collection(path("Secrets"), collection_text(SECRETS, "Secrets", "name", FIELDS),
                            why="declare", scaffold=False)
    service_page(vault, "Notes/Widget Service.md", "Widget Service", WIDGET,
                 "The Widget Service runs in the home office.", "The Widget Service archive sits in the attic.")
    service_page(vault, "Private/Vault Door.md", "Vault Door", VAULT_DOOR, "The Vault Door opens onto the sub-basement.")
    row(store, SERVICES, 1, "Widget Service", "home office", f"exomem://memory/{WIDGET}")
    row(store, SECRETS, 2, "Widget Service", "attic", f"exomem://memory/{WIDGET}")
    row(store, SERVICES, 3, "Vault Door", "sub-basement", f"exomem://memory/{VAULT_DOOR}")
    _ready(store, SERVICES)
    _ready(store, SECRETS)
    tick(vault)
    assert len(served(vault, owner_principal())) == 3
    scope = vault / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    hidden = ["Knowledge Base/Records/Secrets/**", "Knowledge Base/Private/**"]
    scope.write_text(f"governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\npaths: {json.dumps(hidden)}\n")
    write_rule(vault, ceiling=0)
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    seen = served(vault, _external())
    assert len(seen) == 1 and "home office" in seen[0]["why"]
    text = json.dumps(seen)
    assert not any(secret in text for secret in ("attic", "Vault Door", "sub-basement", "Secrets"))


def test_a_page_cited_by_more_rows_than_one_read_returns_is_walked_and_a_bound_is_reported(world, monkeypatch):
    """A detector that kept the first page of citations would never compare the rows past it, and would say nothing."""
    store, vault = world
    service_page(vault, "Notes/Widget Service.md", "Widget Service", WIDGET,
                 "The Widget Service runs in the home office.", "The Gadget Service sits in the garage.")
    link = f"exomem://memory/{WIDGET}"
    row(store, SERVICES, 1, "Widget Service", "home office", link)
    for number in (2, 3, 4):
        row(store, SERVICES, number, f"Filler {number}", "nowhere", link)
    row(store, SERVICES, 5, "Gadget Service", "garage", link)  # the fifth citing row, past two pages of two
    _ready(store, SERVICES)
    monkeypatch.setattr(duplicate_facts.page_citations, "MAX_LIMIT", 2)
    tick(vault)
    items = served(vault, owner_principal())
    assert sorted("garage" in item["why"] for item in items) == [False, True]
    assert not any("were compared" in item["why"] for item in items)

    monkeypatch.setattr(duplicate_facts, "ROWS_PER_PAGE", 4)
    fx.edit(vault, "Knowledge Base/Notes/Widget Service.md",
            f"---\nexomem_id: {WIDGET}\ntitle: Widget Service\n---\n# Widget Service\n\n## Observations\n\n"
            "- [finding] The Widget Service runs in the home office.\n- [finding] The Gadget Service sits in the garage.\n"
            "- [finding] Added so that the page changes.\n")
    tick(vault)
    [item] = served(vault, owner_principal())
    assert "home office" in item["why"] and "only the first 4 records that cite this page were compared" in item["why"]


def test_a_link_index_that_is_still_building_holds_back_only_this_family(world):
    """A second collection's link index that warms made every page unreadable for this family, and the page's
    whole transaction rolled back: the other seven families then did nothing for any page until it was built."""
    store, vault = world
    service_page(vault, "Notes/Widget Service.md", "Widget Service", WIDGET,
                 "The Widget Service runs in the home office.", "The Gadget Service sits in the garage.")
    link = f"exomem://memory/{WIDGET}"
    row(store, SERVICES, 1, "Widget Service", "home office", link)
    _ready(store, SERVICES)
    tick(vault)
    assert len(served(vault, owner_principal())) == 1
    # No `_ready` for this collection: its link index is still building.
    store.create_collection(path("Gadgets"), collection_text(GADGETS, "Gadgets", "name", FIELDS),
                            why="declare", scaffold=False)
    service_page(vault, "Notes/Other Page.md", "Other Page", OTHER_PAGE, "Nothing about services is stated here.")
    results = tick(vault)
    assert [(r.processed, r.stop_reason, r.waiting) for r in results] == [
        (("Knowledge Base/Notes/Other Page.md",), "drained", None)]
    row(store, SERVICES, 2, "Gadget Service", "garage", link)
    service_page(vault, "Notes/Widget Service.md", "Widget Service", WIDGET,
                 "The Widget Service runs in the home office.", "The Gadget Service sits in the garage.", "Edited.")
    results = tick(vault)
    assert [(r.processed, r.stop_reason, r.waiting) for r in results] == [
        (("Knowledge Base/Notes/Widget Service.md",), "drained", None)]
    # The page was owed, not lost: once the index is built the family compares it with no further page change.
    _ready(store, GADGETS)
    tick(vault)
    assert len(served(vault, owner_principal())) == 2


def test_a_row_written_while_its_collection_index_builds_is_compared_once_it_is_built(world):
    """A new row has no link entry until the backfill reaches it, and nothing queues its page after that, so a
    row that restated a page's unit in the first minutes of its collection was never compared."""
    store, vault = world
    service_page(vault, "Notes/Widget Service.md", "Widget Service", WIDGET,
                 "The Widget Service runs in the home office.")
    _ready(store, SERVICES)
    tick(vault)
    store.create_collection(path("Gadgets"), collection_text(GADGETS, "Gadgets", "name", FIELDS),
                            why="declare", scaffold=False)
    row(store, GADGETS, 1, "Widget Service", "home office", f"exomem://memory/{WIDGET}")
    tick(vault)
    assert served(vault, owner_principal()) == []
    _ready(store, GADGETS)
    tick(vault)
    tick(vault)
    assert len(served(vault, owner_principal())) == 1


def test_a_page_no_row_cites_opens_no_query_session(world, monkeypatch):
    """Every page a tick processes would open a query session to learn that no row links it."""
    store, vault = world
    service_page(vault, "Notes/Widget Service.md", "Widget Service", WIDGET,
                 "The Widget Service runs in the home office.")
    _ready(store, SERVICES)
    opened = []
    real = runtime.read_session

    def counted(*args, **kwargs):
        opened.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(runtime, "read_session", counted)
    tick(vault)
    assert opened == []
    row(store, SERVICES, 1, "Widget Service", "home office", f"exomem://memory/{WIDGET}")
    tick(vault)
    assert opened, "a page that a row cites is read through the citation reader"
    assert len(served(vault, owner_principal())) == 1
