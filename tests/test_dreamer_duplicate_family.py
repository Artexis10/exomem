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

from exomem import dreamer, dreamer_families, dreamer_store, freshness, upkeep
from exomem.collection_store import connection
from exomem.governance import egress, membership, policy
from exomem.governance.principal import owner_principal, request_scope

SERVICES = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1"
SECRETS = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2"
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
def world(store, monkeypatch, tmp_path):  # noqa: F811
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
        store.append_record(collection, item={"name": name, "location": location, "page": link},
                            item_key=key(number), why="record")


def tick(vault):
    results = fx.run_to_quiet(vault)
    assert all(result.stop_reason != "error" for result in results), results


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
    rows would walk the whole collection, and one that edited the page or row would rewrite what the owner
    wrote. A row written after the first tick must still be found with no page change."""
    store, vault = world
    service_page(vault, "Notes/Widget Service.md", "Widget Service", WIDGET,
                 "The Widget Service runs in the home office on a spare laptop.", "Backups finish before dawn.")
    link = f"exomem://memory/{WIDGET}"
    row(store, SERVICES, 1, "Widget Service", "home office", link)   # restated: the unit holds the value and the name
    row(store, SERVICES, 2, "Gadget Service", "garage", link)        # no unit holds this value
    row(store, SERVICES, 3, "Widget", "home office", "Nowhere Page")  # the link resolves to no page
    _ready(store, SERVICES)
    pages, rows = fx.tree_state(vault), store_state(store)
    tick(vault)
    [first] = served(vault, owner_principal())
    assert first["family"] == dreamer_families.DUPLICATE_FAMILY
    [stored] = open_candidates(vault)
    assert (stored["measures"]["field"], stored["measures"]["relation"]) == ("location", "duplicates")
    assert stored["measures"]["unit_ref"].startswith(link) and stored["measures"]["unit_ref"] in first["why"]
    assert "location" in first["why"] and first["why"].count("home office") == 2
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
