"""A write that creates rows lists the pages that already name the row's subject.

The fixtures invent a services collection (natural key `name`) over pages that bear service names as a
title or an alias, with the activation index built the way the running service holds it.
"""

import pytest
from test_collection_link_values import page
from test_collection_row_references import collection_text, path
from test_collection_store_writer import store as store  # noqa: F401
from test_records_bulk_upsert import EVIDENCE, _evidence

from exomem import working_set_index
from exomem.governance.principal import owner_principal, request_scope

SERVICES = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1"
FIELDS = "    name: {type: string, required: true}\n    owner: {type: string}\n"


def identity(number):
    return f"{number:08d}-0000-4000-8000-000000000000"


def build_index(root):
    index = working_set_index.WorkingSetIndex(root)
    index.rebuild(freshness_stamp="test")
    index.close()


@pytest.fixture
def world(store):  # noqa: F811
    store.create_collection(path("Services"), collection_text(SERVICES, "Services", "name", FIELDS),
                            why="declare", scaffold=False)
    return store


def guards(world):
    return world.inspect_collection(SERVICES)["lifecycle_guards"]["expected_container_hash"]


def append(world, name, key):
    with request_scope(owner_principal()):
        return world.append_record(SERVICES, item={"name": name}, item_key=key, why="record")


def bulk(world, names):
    _evidence(world.root)
    with request_scope(owner_principal()):
        return world.bulk_upsert_records(
            SERVICES, rows=[{"item": {"name": name}} for name in names], why="import", source=EVIDENCE,
            expected_container_hash=guards(world))


def test_a_new_row_lists_the_visible_pages_that_name_it_and_an_update_lists_none(world):
    """A seeding agent wrote an unknown beside a page that already held the answer, because the reply gave
    it no way to see that page. An update that looked pages up would add cost to every later edit."""
    page(world.root, "Notes/Widget Service.md", "Widget Service", identity(1))
    page(world.root, "Notes/Widget Runbook.md", "Widget Runbook", identity(2))
    (world.root / "Knowledge Base/Notes/Widget Runbook.md").write_text(
        f"---\nexomem_id: {identity(2)}\ntitle: Widget Runbook\naliases: [Widget Service]\n---\n# Widget Runbook\n")
    build_index(world.root)
    key = "00000000-0000-4000-8000-0000000000f1"
    created = append(world, "widget service", key)
    [row] = created["related_pages"]
    assert row["item_key"] == key
    assert {page_["ref"] for page_ in row["pages"]} == {f"exomem://memory/{identity(1)}", f"exomem://memory/{identity(2)}"}
    assert {page_["title"] for page_ in row["pages"]} == {"Widget Service", "Widget Runbook"}
    with request_scope(owner_principal()):
        edited = world.update_record(
            SERVICES, item_key=key, changes={"owner": "Dana"}, why="edit",
            expected_item_version=created["after_item_hash"], expected_container_hash=guards(world))
    assert "related_pages" not in edited


def test_a_bulk_seed_resolves_every_row_in_one_lookup_and_lists_at_most_thirty_pages(world, monkeypatch):
    """A lookup per row turns a 500-row seed into 500 index queries, and a reply that lists every match
    outgrows the response bound."""
    for number in range(40):
        for copy in range(4):
            page(world.root, f"Notes/S{number}/Service {number} {copy}.md", f"Service {number}", identity(number * 10 + copy + 100))
    build_index(world.root)
    lookups = []
    original = working_set_index.WorkingSetIndex.resolve_names

    def counted(self, names):
        lookups.append(list(names))
        return original(self, names)

    monkeypatch.setattr(working_set_index.WorkingSetIndex, "resolve_names", counted)
    result = bulk(world, [f"Service {number}" for number in range(40)])
    assert result["counts"]["inserted"] == 40
    assert len(lookups) == 1 and len(lookups[0]) == 40
    listed = result["related_pages"]
    assert all(len(row["pages"]) <= 3 for row in listed)
    assert sum(len(row["pages"]) for row in listed) == 30


def test_a_withheld_page_is_listed_exactly_as_a_missing_one(world):
    """A page the writer may not read must not be named, and its absence must read as no match."""
    page(world.root, "Private/Secret Service.md", "Secret Service", identity(3))
    page(world.root, "Notes/Public Service.md", "Public Service", identity(4))
    (world.root / "Knowledge Base/_access.yaml").write_text('excluded: ["Private/Secret Service.md"]\n')
    build_index(world.root)
    hidden = append(world, "Secret Service", "00000000-0000-4000-8000-0000000000f2")
    missing = append(world, "Absent Service", "00000000-0000-4000-8000-0000000000f3")
    listed = append(world, "Public Service", "00000000-0000-4000-8000-0000000000f4")
    assert "related_pages" not in hidden and "related_pages" not in missing
    assert hidden.get("warnings") == missing.get("warnings")
    assert listed["related_pages"][0]["pages"] == [{"title": "Public Service", "ref": f"exomem://memory/{identity(4)}"}]


def test_an_index_that_was_never_built_is_reported_not_read_as_no_pages(world):
    """A just-started service with no index would otherwise say 'no related pages' for every row."""
    page(world.root, "Notes/Widget Service.md", "Widget Service", identity(1))
    created = append(world, "Widget Service", "00000000-0000-4000-8000-0000000000f5")
    assert "related_pages" not in created
    assert any(text.startswith("RELATED_PAGES_UNAVAILABLE") for text in created["warnings"])


@pytest.mark.parametrize("detail", [None, "full"])
@pytest.mark.parametrize("action", ["append", "bulk_upsert"])
def test_the_public_tool_lists_related_pages_at_the_top_of_the_reply(world, action, detail):
    """The agent reads the compact reply by default; a list that only a full reply carried never reached the
    agent that wrote the unknown beside the page that held the answer. Append and bulk build separate leaves."""
    from test_due_state_bulk_carriers import _command

    from exomem import writer_lease
    from exomem.collection_store.preview import preview_store

    page(world.root, "Notes/Widget Service.md", "Widget Service", identity(1))
    build_index(world.root)
    _evidence(world.root)
    sent = ({"item": {"name": "Widget Service"}} if action == "append" else
            {"rows": [{"item": {"name": "Widget Service"}}], "source": EVIDENCE,
             "expected_container_hash": guards(world)})
    if detail is not None:
        sent["response_detail"] = detail
    with preview_store(world.root, world.handle):
        reply = writer_lease.invoke_command(
            _command("record_memory"), world.root, action=action, collection=SERVICES, why="record", **sent)
    [row] = reply["related_pages"]
    assert row["pages"] == [{"title": "Widget Service", "ref": f"exomem://memory/{identity(1)}"}]
