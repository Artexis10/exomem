"""The inverse-expansion index: item_references maintenance, backfill, warming and leakage rules."""

import sqlite3

import pytest
from test_collection_row_reference_queries import FIELDS  # noqa: F401
from test_collection_row_reference_queries import run as run_diary  # noqa: F401
from test_collection_row_reference_queries import world as world
from test_collection_row_references import (
    DIARY,
    GONE,
    SITE_FIELD,
    SITES,
    SKILL_FIELD,
    SKILLS,
    SLUG,
    collection_text,
    create,
    diary_text,
    revise,
    row,
    sites_text,
    skills_text,
)
from test_collection_store_schema import _allow
from test_collection_store_writer import store as store  # noqa: F401
from test_governance_egress import _external, write_rule, write_scope

from exomem import structured_collections as collections
from exomem.collection_store import connection, reference_index, schema, snapshot
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import route, runtime

K1 = "00000000-0000-4000-8000-0000000000c1"
K2 = "00000000-0000-4000-8000-0000000000c2"
K3 = "00000000-0000-4000-8000-0000000000c3"
K4 = "00000000-0000-4000-8000-0000000000c4"
K5 = "00000000-0000-4000-8000-0000000000c5"
K6 = "00000000-0000-4000-8000-0000000000c6"
K7 = "00000000-0000-4000-8000-0000000000c7"

INVERSE = {"inverse": {"collection": DIARY, "field": "skills.skill_id"}}


def run(store, query, who=None):
    with request_scope(who or owner_principal()):
        return route.run(store, SKILLS, {"version": 1, **query}, facade_profile="records")


def ready(store, cid=DIARY):
    store.backfill_derived(cid)  # also seeds state rows a restored replica lacks
    while _pending(store, cid):
        store.backfill_derived(cid)
    return store


def _pending(store, cid):
    return bool(store.handle.connection.execute(
        "SELECT 1 FROM item_reference_state WHERE collection_id=? AND ready=0", (cid,)).fetchone())


def _indexed(store, cid=DIARY, field="skills.skill_id"):
    return sorted((row_id, field, ordinal, value, match)
                  for row_id, field, ordinal, value, match in store.handle.connection.execute(
                      "SELECT row_id, field_path, ordinal, value, match FROM item_references "
                      "WHERE collection_id=? AND field_path=?", (cid, field)).fetchall())


def _implied(store, cid=DIARY):
    """The index rows the current items imply for skills.skill_id, computed without the index."""
    from exomem.collection_store import typed_storage

    _, diary, _ = store._collection(cid)
    declaration = next(d for d in collections.declared_references(diary.schema) if d.dotted == "skills.skill_id")
    implied = []
    for row_id, in store.handle.connection.execute(
            "SELECT row_id FROM items WHERE collection_id=? ORDER BY row_id", (cid,)).fetchall():
        for ordinal, value in enumerate(
                declaration.values(typed_storage.item_values(store.handle.connection, row_id))):
            implied.append((row_id, "skills.skill_id", ordinal, value))
    return sorted(implied)


# --- 1. rebuild from items ----------------------------------------------------------


def test_rebuild_from_items_matches_an_independent_computation(world):
    """A stale or partial index must rebuild byte-identical rows from items alone."""
    store_ = ready(world)
    before = _indexed(store_)
    implied = _implied(store_)
    assert [(r, f, o, v) for r, f, o, v, _ in before] == implied

    store_.rebuild_references(DIARY)
    assert _pending(store_, DIARY)
    while _pending(store_, DIARY):
        store_.backfill_derived(DIARY)
    assert _indexed(store_) == before

    # A declaration added by revise and one removed by revise keep the table in step.
    revise(store_, DIARY, diary_text(FIELDS + SITE_FIELD.replace("site:", "also_site:")))
    assert _pending(store_, DIARY)
    while _pending(store_, DIARY):
        store_.backfill_derived(DIARY)
    states = {f for (f,) in store_.handle.connection.execute(
        "SELECT field_path FROM item_reference_state WHERE collection_id=?", (DIARY,)).fetchall()}
    assert states == {"site", "skills.skill_id", "also_site"}
    revise(store_, DIARY, diary_text(FIELDS))
    assert not store_.handle.connection.execute(
        "SELECT 1 FROM item_reference_state WHERE collection_id=? AND field_path='also_site'",
        (DIARY,)).fetchone()
    assert not store_.handle.connection.execute(
        "SELECT 1 FROM item_references WHERE collection_id=? AND field_path='also_site'",
        (DIARY,)).fetchone()


# --- 2. replica warms instead of reading a copied index --------------------------------


def test_a_collection_snapshot_holds_no_index_rows_and_a_restore_warms(world, tmp_path):
    """A copied index could read as complete on a restore; the copy carries none and warms."""
    store_ = ready(world)
    assert _indexed(store_)
    stage = tmp_path / "stage"
    stage.mkdir()
    with snapshot._staged_copy(
            snapshot._collection_fill(store_.handle.path), directory=stage,
            deadline=__import__("time").monotonic() + 10, attaches_source=True) as artifact:
        copy = sqlite3.connect(artifact.path)
        try:
            assert copy.execute("SELECT count(*) FROM item_references").fetchone()[0] == 0
            states = copy.execute(
                "SELECT built_through, ready FROM item_reference_state").fetchall()
            assert states and all(row == (0, 0) for row in states)
            # The restored store's serving loop finds its warming collections through due().
            assert DIARY in reference_index.due(copy)
        finally:
            copy.close()
    world.rebuild_references(DIARY)
    stale = run(world, {"related": [INVERSE], "select": ["slug"]})
    assert stale["rows"][0]["related"][f"inverse:{DIARY}:skills.skill_id"] == {"state": "warming"}
    ready(world)
    result = run(world, {"related": [INVERSE], "select": ["slug"]})
    assert result["rows"][0]["related"][f"inverse:{DIARY}:skills.skill_id"]["state"] == "ready"


# --- 3. exact inverse counts ------------------------------------------------------------


def test_inverse_counts_are_exact_under_duplicates_renames_and_row_refs(world):
    """A repeated value counts once, a renamed key drops its rows, a canonical ref matches."""
    ready(world)
    row(world, DIARY, K4, {"title": "day four",
                           "skills": [{"skill_id": "welding"}, {"skill_id": "welding"}]})
    result = run(world, {"related": [INVERSE], "select": ["slug"],
                         "where": {"field": "slug", "op": "eq", "value": "welding"}})
    entry = result["rows"][0]["related"][f"inverse:{DIARY}:skills.skill_id"]
    assert entry["state"] == "ready" and entry["count"] == 2
    assert len(entry["items"]) == 2 and not entry["truncated"]

    # A canonical row ref value matches the same row.
    row(world, DIARY, K5, {"title": "day five",
                           "skills": [{"skill_id": collections.record_ref(SKILLS, SLUG)}]})
    result = run(world, {"related": [INVERSE],
                         "where": {"field": "slug", "op": "eq", "value": "welding"}})
    assert result["rows"][0]["related"][f"inverse:{DIARY}:skills.skill_id"]["count"] == 3

    # A rename of the skill's key drops the natural-key matches; only the row ref still matches.
    other = "00000000-0000-4000-8000-0000000000b3"
    added = row(world, SKILLS, other, {"slug": "grinding"})
    row(world, DIARY, K6, {"title": "day six", "skills": [{"skill_id": "grinding"}]})
    row(world, DIARY, K7, {"title": "day seven",
                                 "skills": [{"skill_id": collections.record_ref(SKILLS, other)}]})
    guards = world.inspect_collection(SKILLS)["lifecycle_guards"]
    world.update_record(SKILLS, item_key=other, changes={"slug": "polished"}, why="rename",
                        expected_item_version=added["after_item_hash"],
                        expected_container_hash=guards["expected_container_hash"])
    result = run(world, {"related": [INVERSE],
                         "where": {"field": "slug", "op": "eq", "value": "polished"}})
    entry = result["rows"][0]["related"][f"inverse:{DIARY}:skills.skill_id"]
    assert entry["count"] == 1
    assert entry["items"][0]["item_key"] == K7


# --- 3b. date-keyed target ----------------------------------------------------------------


def test_an_inverse_on_a_date_keyed_target_matches_the_serialized_key(store):
    """A date value and a date key must match by the same serialization forward lookup uses."""
    seasons = collection_text(
        "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "Seasons", "when",
        "    when: {type: date, required: true}\n")
    create(store, "dddddddd-dddd-4ddd-8ddd-dddddddddddd", seasons, "Seasons")
    fields = ("    leg: {type: string, references: "
              "{collection: dddddddd-dddd-4ddd-8ddd-dddddddddddd}, filterable: true}\n")
    create(store, DIARY, diary_text(fields), "Diary")
    while _pending(store, DIARY):
        store.backfill_derived(DIARY)
    row(store, "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "00000000-0000-4000-8000-0000000000e1",
        {"when": "2026-03-04"})
    row(store, DIARY, K1, {"title": "day one", "leg": "20260304"})
    inverse = {"inverse": {"collection": DIARY, "field": "leg"}}
    with request_scope(owner_principal()):
        result = route.run(store, "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                           {"version": 1, "related": [inverse], "select": ["when"],
                            "where": {"field": "when", "op": "eq", "value": "2026-03-04"}},
                           facade_profile="records")
    entry = result["rows"][0]["related"][f"inverse:{DIARY}:leg"]
    assert entry["state"] == "ready" and entry["count"] == 1
    assert entry["items"][0]["item_key"] == K1


# --- 4. no leak ---------------------------------------------------------------------------


def test_a_hidden_source_refuses_identically_to_a_missing_one(world, tmp_path):
    """Inverse over a withheld collection must read exactly like one that does not exist."""
    ready(world)
    write_scope(world.root, paths="Records/Diary/**")
    write_rule(world.root, ceiling=0)
    hidden = None
    with pytest.raises(runtime.QueryError) as error:
        run(world, {"related": [INVERSE]}, who=_external())
    hidden = (error.value.code, str(error.value))
    with pytest.raises(runtime.QueryError) as error:
        run(world, {"related": [{"inverse": {"collection": GONE, "field": "skills.skill_id"}}]},
            who=_external())
    assert (error.value.code, str(error.value)) == hidden
    from test_collection_store_importer import preview_store
    with request_scope(_external()):
        with preview_store(world.root, world.handle) as writer:
            inspect = writer.inspect_collection(SKILLS)
    assert all(entry["collection"] != DIARY for entry in inspect.get("inbound", []))


def test_a_withheld_source_member_reads_exactly_like_an_undeclared_field(store):
    """Matching through a withheld field would reveal it, so both cases refuse identically."""
    guarded = SKILL_FIELD.replace("      type: array\n", "      type: array\n      classification: location\n")
    create(store, SITES, sites_text(), "Sites")
    create(store, SKILLS, skills_text(), "Skills")
    create(store, DIARY, diary_text(SITE_FIELD + guarded), "Diary")
    while _pending(store, DIARY):
        store.backfill_derived(DIARY)
    with pytest.raises(runtime.QueryError) as error:
        run(store, {"related": [INVERSE]}, who=_external())
    withheld = (error.value.code, str(error.value))
    assert error.value.code == "QUERY_FIELD_UNAVAILABLE"
    with pytest.raises(runtime.QueryError) as error:
        run(store, {"related": [{"inverse": {"collection": DIARY, "field": "unseen.x"}}]},
            who=_external())
    assert (error.value.code, str(error.value)) == withheld


# --- 5. warming ----------------------------------------------------------------------------


def test_inverse_reads_warming_until_the_backfill_completes(world):
    """An unbuilt index must say warming, never an empty list; inspect agrees."""
    revise(world, DIARY, diary_text(FIELDS + "    notes: {type: string}\n"))
    revise(world, DIARY, diary_text(FIELDS))
    world.rebuild_references(DIARY)
    result = run(world, {"related": [INVERSE]})
    assert result["rows"][0]["related"][f"inverse:{DIARY}:skills.skill_id"] == {"state": "warming"}
    inbound = world.inspect_collection(SKILLS)["inbound"]
    entry = next(e for e in inbound if e["collection"] == DIARY)
    assert entry["state"] == "warming"
    while _pending(world, DIARY):
        world.backfill_derived(DIARY)
    result = run(world, {"related": [INVERSE],
                         "where": {"field": "slug", "op": "eq", "value": "welding"}})
    assert result["rows"][0]["related"][f"inverse:{DIARY}:skills.skill_id"]["state"] == "ready"


# --- 6. budget -------------------------------------------------------------------------------


def test_candidate_visits_charge_against_the_visit_bound(world, monkeypatch):
    """Every candidate source row pays one visit even when it is not released."""
    ready(world)
    for i in range(6, 10):
        row(world, DIARY, f"00000000-0000-4000-8000-0000000000d{i}",
            {"title": f"day {i}", "skills": [{"skill_id": "welding"}]})
    monkeypatch.setitem(runtime.PROFILES["interactive"], "max_row_visits", 3)
    with pytest.raises(runtime.QueryError) as error:
        run(world, {"related": [INVERSE],
                    "where": {"field": "slug", "op": "eq", "value": "welding"}})
    assert error.value.code == "QUERY_COST_LIMIT"


# --- 7. migration ----------------------------------------------------------------------------


def test_a_schema_16_store_gains_warming_state_rows_and_backfills(tmp_path, monkeypatch):
    """A store that already declared references must index nothing until the backfill runs."""
    from exomem.collection_store.writer import CollectionWriter
    from exomem.governance.principal import library_scope

    root = tmp_path / "mig-vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# log\n")
    path = tmp_path / "migrated.sqlite"
    with monkeypatch.context() as earlier:
        earlier.setattr(schema, "SCHEMA_VERSION", 16)
        with connection.open_writer(path, lease_check=_allow) as handle, library_scope():
            assert schema.schema_version(handle.connection) == 16
            writer = CollectionWriter(root, handle)
            create(writer, SITES, sites_text(), "Sites")
            create(writer, SKILLS, skills_text(), "Skills")
            create(writer, DIARY, diary_text(FIELDS), "Diary")
            row(writer, SKILLS, SLUG, {"slug": "welding"})
            row(writer, DIARY, K1, {"title": "d", "skills": [{"skill_id": "welding"}]})
    with connection.open_writer(path, lease_check=_allow) as handle, library_scope():
        conn = handle.connection
        assert schema.schema_version(conn) == 17
        state = conn.execute(
            "SELECT ready, built_through FROM item_reference_state "
            "WHERE collection_id=? AND field_path='skills.skill_id'", (DIARY,)).fetchone()
        assert state == (0, 0)
        assert not conn.execute("SELECT 1 FROM item_references").fetchone()
        writer = CollectionWriter(root, handle)
        while conn.execute("SELECT 1 FROM item_reference_state WHERE ready=0").fetchone():
            writer.backfill_derived(DIARY)
        assert conn.execute(
            "SELECT ready FROM item_reference_state WHERE collection_id=? AND field_path='skills.skill_id'",
            (DIARY,)).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM item_references WHERE collection_id=?", (DIARY,)
        ).fetchone() == (1,)


# --- 8. saved view staleness -----------------------------------------------------------------


def test_a_view_with_an_inverse_entry_goes_stale_when_the_source_drops_its_declaration(store):
    """A view living on the target sees a removed source declaration as stale, not malformed."""
    from test_collection_store_importer import preview_store

    from exomem.cli_ops import OpError
    from exomem.record_memory import record_memory
    block = ("\nviews:\n  v-back:\n    query:\n      version: 1\n"
             "      related: [{inverse: {collection: " + DIARY + ", field: skills.skill_id}}]\n")
    text = skills_text().replace("\n---\n", block + "---\n")
    create(store, SITES, sites_text(), "Sites")
    create(store, SKILLS, text, "Skills")
    create(store, DIARY, diary_text(FIELDS), "Diary")
    store.backfill_derived(DIARY)
    with request_scope(owner_principal()):
        with preview_store(store.root, store.handle):
            result = record_memory(store.root, action="query", collection=SKILLS, view="v-back")
    assert result["view"]["name"] == "v-back"
    revise(store, DIARY, diary_text("    hours: {type: integer}\n"))
    with request_scope(owner_principal()):
        with pytest.raises((OpError, collections.CollectionError)) as error:
            with preview_store(store.root, store.handle):
                record_memory(store.root, action="query", collection=SKILLS, view="v-back")
    assert getattr(error.value, "code", None) == "SAVED_VIEW_STALE"
