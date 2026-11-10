"""Version-1 saved views over declared references: guard, execution, staleness and listing."""

import pytest
from test_collection_row_references import (
    DIARY,
    NORTH,
    SITE_FIELD,
    SITES,
    collection_text,
    create,
    diary_text,
    revise,
    row,
    sites_text,
)
from test_collection_store_importer import preview_store
from test_collection_store_writer import store as store  # noqa: F401

from exomem import record_governance
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.governance.principal import owner_principal, request_scope
from exomem.record_memory import record_memory

K1 = "00000000-0000-4000-8000-0000000000c1"
K2 = "00000000-0000-4000-8000-0000000000c2"
REGIONS = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

VIEW_BLOCK = """
views:
  v-north:
    query:
      version: 1
      select: [title, site.name]
      where: {field: site.name, op: eq, value: "North field"}
"""


def diary_with_view(fields=SITE_FIELD + "    hours: {type: integer}\n", block=VIEW_BLOCK):
    return diary_text(fields).replace("\n---\n", f"\n{block}---\n")


@pytest.fixture
def world(store):
    create(store, SITES, sites_text(), "Sites")
    create(store, DIARY, diary_with_view(), "Diary")
    row(store, SITES, NORTH, {"code": "North-Field", "name": "North field"})
    row(store, DIARY, K1, {"title": "d1", "site": "North-Field"})
    return store


def query_view(store, view, who=None, **kwargs):
    with request_scope(who or owner_principal()), preview_store(store.root, store.handle):
        return record_memory(store.root, action="query", collection=DIARY, view=view, **kwargs)


def test_dropping_a_referenced_declaration_is_refused_while_a_view_uses_it(world):
    """The revise must move the view and the declaration together, never strand the view."""
    no_ref = SITE_FIELD.replace(f", references: {{collection: {SITES}}}", "")
    with pytest.raises(collections.CollectionError) as error:
        revise(world, DIARY, diary_with_view(no_ref + "    hours: {type: integer}\n"))
    assert error.value.code == "REFERENCE_IN_USE"
    assert "v-north" in error.value.details["views"]


def test_removing_declaration_and_view_together_commits(world):
    """The same revise may drop both; nothing strands."""
    no_ref = SITE_FIELD.replace(f", references: {{collection: {SITES}}}", "")
    assert revise(world, DIARY, diary_text(no_ref + "    hours: {type: integer}\n")) is not None


def test_a_v1_view_runs_and_pages(world):
    """view= runs the engine query; query.page is the only paging override it accepts."""
    row(world, DIARY, K2, {"title": "d2", "site": "North-Field"})
    first = query_view(world, "v-north", query={"version": 1, "page": {"limit": 1}})
    assert first["view"]["name"] == "v-north" and first["view"]["identity"]
    assert first["returned"] == 1 and first["has_more"]
    second = query_view(world, "v-north",
                        query={"version": 1, "page": {"limit": 1, "after": first["next_cursor"]}})
    titles = {first["rows"][0]["title"], second["rows"][0]["title"]}
    assert titles == {"d1", "d2"}
    assert all(item["site.name"] == "North field" for item in first["rows"] + second["rows"])


def test_extra_keys_alongside_a_v1_view_conflict(world):
    """Anything but query={version,page} next to view is ambiguous and refused."""
    with pytest.raises((collections.CollectionError, OpError)) as error:
        query_view(world, "v-north",
                   query={"version": 1, "where": {"field": "title", "op": "eq", "value": "d1"}})
    assert error.value.code == "QUERY_ARGUMENT_CONFLICT"


def _second_hop_view(store, region_classification=""):
    """A diary view on site.region.name, over sites whose region field declares Regions."""
    create(store, REGIONS, collection_text(REGIONS, "Regions", "rid",
                                           "    rid: {type: string, required: true}\n"
                                           "    name: {type: string}\n"), "Regions")
    sites = collection_text(SITES, "Sites", "code",
                            "    code: {type: string, required: true}\n    name: {type: string}\n"
                            f"    region: {{type: string,{region_classification} "
                            f"references: {{collection: {REGIONS}}}}}\n")
    create(store, SITES, sites, "Sites")
    block = ("\nviews:\n  v-region:\n    query:\n      version: 1\n"
             "      select: [title, site.region.name]\n      where: {field: site.region.name, op: eq, value: North}\n")
    create(store, DIARY, diary_with_view(block=block), "Diary")
    row(store, REGIONS, NORTH, {"rid": "north", "name": "North"})
    row(store, SITES, "00000000-0000-4000-8000-0000000000a5", {"code": "yard", "region": "north"})
    row(store, DIARY, K1, {"title": "d1", "site": "yard"})


def _stale(store, who=None):
    with pytest.raises((collections.CollectionError, OpError)) as error:
        query_view(store, "v-region", who=who)
    return error.value.code, str(error.value)


def test_a_view_on_a_second_hop_goes_stale_when_the_target_drops_its_declaration(store):
    """The view bound a field another collection no longer offers: stale, not malformed."""
    _second_hop_view(store)
    assert query_view(store, "v-region")["returned"] == 1
    dropped = collection_text(SITES, "Sites", "code",
                              "    code: {type: string, required: true}\n    name: {type: string}\n"
                              "    region: {type: string}\n")
    revise(store, SITES, dropped)
    assert _stale(store)[0] == "SAVED_VIEW_STALE"


def test_a_withheld_second_hop_reads_exactly_like_a_dropped_declaration(store):
    """A caller who cannot read the region field must not learn it still declares a reference."""
    from test_governance_egress import _external

    _second_hop_view(store, " classification: location,")
    withheld = _stale(store, who=_external())
    assert withheld[0] == "SAVED_VIEW_STALE"
    assert withheld == _dropped_twin_refusal(store)


def _dropped_twin_refusal(store):
    """The owner's refusal after Sites drops the region declaration, in the same store."""
    dropped = collection_text(SITES, "Sites", "code",
                              "    code: {type: string, required: true}\n    name: {type: string}\n"
                              "    region: {type: string, classification: location}\n")
    revise(store, SITES, dropped)
    return _stale(store)


def test_a_deleted_first_hop_target_stays_collection_not_found(store):
    """The first hop's missing target is a missing collection, not a stale view."""
    create(store, SITES, sites_text(), "Sites")
    create(store, DIARY, diary_with_view(), "Diary")
    store.connection.execute("PRAGMA foreign_keys=OFF")
    store.connection.execute("DELETE FROM collections WHERE collection_id=?", (SITES,))
    store.connection.commit()
    # An unrelated revise keeping the declaration commits: a deleted target never blocks it.
    assert revise(store, DIARY, diary_with_view(
        SITE_FIELD + "    hours: {type: integer}\n    note: {type: string}\n")) is not None
    with pytest.raises(Exception) as error:
        query_view(store, "v-north")
    assert getattr(error.value, "code", None) == "COLLECTION_NOT_FOUND"


def test_the_legacy_executor_refuses_a_v1_view(world):
    """File-mode execution cannot run an engine query; it must say so, not misread it."""
    with world.read_collection(DIARY) as manifest, request_scope(owner_principal()):
        with pytest.raises(collections.CollectionError) as error:
            record_governance._authorize_saved_view(world.root, manifest, "v-north", None)
    assert error.value.code == "SAVED_VIEW_UNAVAILABLE"
