"""The inverse-expansion index: item_references maintenance, backfill, warming and leakage rules."""

import sqlite3
import time as _time

import pytest
from test_collection_row_reference_queries import FIELDS
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
from test_records_bulk_upsert import EVIDENCE, _evidence  # noqa: F401

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
    return sorted(store.handle.connection.execute(
        "SELECT collection_id, field_path, match, row_id FROM item_references "
        "WHERE collection_id=? AND field_path=?", (cid, field)).fetchall())


def _implied(store, cid=DIARY):
    """The (collection, field, match, row) set the current items imply, computed without the index."""
    from exomem.collection_store import typed_storage

    _, diary, _ = store._collection(cid)
    declaration = next(d for d in collections.declared_references(diary.schema) if d.dotted == "skills.skill_id")
    target = reference_index._target_schema(store.handle.connection, declaration.collection)
    implied = set()
    for row_id, in store.handle.connection.execute(
            "SELECT row_id FROM items WHERE collection_id=? ORDER BY row_id", (cid,)).fetchall():
        for value in declaration.values(typed_storage.item_values(store.handle.connection, row_id)):
            found = reference_index.match(target, declaration.collection, value)
            if found is not None:
                implied.add((cid, "skills.skill_id", found, row_id))
    return sorted(implied)


# --- 1. rebuild from items ----------------------------------------------------------


def test_rebuild_from_items_matches_an_independent_computation(world):
    """A stale or partial index must rebuild byte-identical rows from items alone."""
    store_ = ready(world)
    before = _indexed(store_)
    assert before == _implied(store_)

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
            deadline=_time.monotonic() + 10, attaches_source=True) as artifact:
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
    assert all(entry["collection"] != DIARY for entry in inspect["inbound"]["declarations"])


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
    inbound = world.inspect_collection(SKILLS)["inbound"]["declarations"]
    entry = next(e for e in inbound if e["collection"] == DIARY)
    assert entry["state"] == "warming"
    while _pending(world, DIARY):
        world.backfill_derived(DIARY)
    result = run(world, {"related": [INVERSE],
                         "where": {"field": "slug", "op": "eq", "value": "welding"}})
    assert result["rows"][0]["related"][f"inverse:{DIARY}:skills.skill_id"]["state"] == "ready"


# --- 6. budget -------------------------------------------------------------------------------


def test_candidate_visits_charge_against_the_visit_bound(world, monkeypatch):
    """Every released candidate source row pays one visit, as `admit` charges."""
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


# --- 9. backfill_due reports every kind -------------------------------------------------


def test_backfill_due_lists_projection_rollup_and_reference_index(world):
    """One due row must carry all three flags so no kind of build waits on another."""
    from exomem.collection_store import index_migrations

    ready(world)
    world.rebuild_references(DIARY)
    due = {row[0]: row[1:] for row in index_migrations.backfill_due(world.handle.connection)}
    assert DIARY in due
    projection, rollup, reference = due[DIARY]
    assert reference is True
    # The diary's projection is built and it declares no rollup: it is due on the
    # reference flag alone, so a row that drops that kind stops listing it.
    assert due[DIARY] == (False, False, True)


# --- 10. bulk writes maintain the index -------------------------------------------------


def test_a_bulk_upsert_updates_the_inverse_count(world):
    """The bulk path for new rows must maintain the index like the single-row path."""
    ready(world)
    guards = world.inspect_collection(DIARY)["lifecycle_guards"]
    _evidence(world.root)
    world.bulk_upsert_records(
        DIARY, rows=[{"item": {"title": "bulk one", "skills": [{"skill_id": "welding"}]}},
                     {"item": {"title": "bulk two", "skills": [{"skill_id": "welding"}]}}],
        why="bulk", source=EVIDENCE,
        expected_container_hash=guards["expected_container_hash"])
    result = run(world, {"related": [INVERSE],
                         "where": {"field": "slug", "op": "eq", "value": "welding"}})
    assert result["rows"][0]["related"][f"inverse:{DIARY}:skills.skill_id"]["count"] == 3


# --- 11. the inverse continuation binds nothing rendered --------------------------------


def test_an_inverse_page_two_continues_without_a_stale_cursor(world):
    """A continuation binds state read at bind time, never rendering's account of it."""
    ready(world)
    row(world, SKILLS, K5, {"slug": "grinding"})
    first = run(world, {"related": [INVERSE], "select": ["slug"],
                        "order_by": [{"field": "slug"}], "page": {"limit": 1}})
    assert first["has_more"] and first["next_cursor"]
    second = run(world, {"related": [INVERSE], "select": ["slug"],
                         "order_by": [{"field": "slug"}],
                         "page": {"limit": 1, "after": first["next_cursor"]}})
    assert second["returned"] == 1


def test_a_hidden_source_write_keeps_the_inverse_cursor_valid(world):
    """A write the caller cannot see must not stale a cursor that never showed it."""
    ready(world)
    row(world, SKILLS, K5, {"slug": "grinding"})
    write_scope(world.root, paths="Records/Diary/**")
    write_rule(world.root, ceiling=0)
    query = {"related": [INVERSE], "select": ["slug"],
             "order_by": [{"field": "slug"}], "page": {"limit": 1}}
    first = run(world, query)
    assert first["has_more"] and first["next_cursor"]
    with request_scope(owner_principal()):
        row(world, DIARY, K4, {"title": "day four", "skills": [{"skill_id": "welding"}]})
    second = run(world, {**query, "page": {"limit": 1, "after": first["next_cursor"]}})
    assert second["returned"] == 1


def test_a_source_manifest_change_stales_the_inverse_cursor(world):
    """A bound source manifest that changes must stale the continuation, not answer it."""
    ready(world)
    row(world, SKILLS, K5, {"slug": "grinding"})
    query = {"related": [INVERSE], "select": ["slug"],
             "order_by": [{"field": "slug"}], "page": {"limit": 1}}
    first = run(world, query)
    assert first["has_more"] and first["next_cursor"]
    revise(world, DIARY, diary_text(FIELDS + "    note: {type: string}\n"))
    with pytest.raises(runtime.QueryError) as error:
        run(world, {**query, "page": {"limit": 1, "after": first["next_cursor"]}})
    assert error.value.code == "QUERY_CURSOR_STALE"


# --- 12. withheld candidates cost nothing ------------------------------------------------


def test_withheld_candidates_charge_no_visit(world, monkeypatch):
    """Released rows alone charge: withheld referrers read exactly like absent ones."""
    ready(world)
    write_scope(world.root, paths="Unrelated/**")
    hidden = []
    for i in range(1, 8):
        key = f"00000000-0000-4000-8000-0000000000e{i}"
        with request_scope(owner_principal()):
            row(world, DIARY, key, {"title": f"hidden {i}", "skills": [{"skill_id": "welding"}]})
        hidden.append(collections.record_ref(DIARY, key))
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text()
                     + "refs: [" + ", ".join(f'"{ref}"' for ref in hidden) + "]\n")
    write_rule(world.root, ceiling=0)
    query = {"related": [INVERSE],
             "where": {"field": "slug", "op": "eq", "value": "welding"}}
    result = run(world, query, who=_external())
    entry = result["rows"][0]["related"][f"inverse:{DIARY}:skills.skill_id"]
    assert entry["count"] == 1  # only the caller-visible referrer counts

    # Under a bound below the withheld candidates' number, the caller still answers:
    # only released rows charge, so the outcome matches a vault where they never existed.
    monkeypatch.setitem(runtime.PROFILES["interactive"], "max_row_visits", 3)
    assert run(world, query, who=_external())["rows"][0]["related"][
        f"inverse:{DIARY}:skills.skill_id"]["count"] == 1


def test_the_preview_charge_hides_how_many_referrers_are_withheld(world):
    """The preview's estimate counts no index rows: withheld referrers read as absent ones."""
    ready(world)
    query = {"related": [INVERSE],
             "where": {"field": "slug", "op": "eq", "value": "welding"}}
    charge_without = run(world, {**query, "mode": "preview"}, who=_external())["visit_charge"]
    write_scope(world.root, paths="Unrelated/**")
    hidden = []
    for i in range(1, 8):
        key = f"00000000-0000-4000-8000-0000000000f{i}"
        with request_scope(owner_principal()):
            row(world, DIARY, key, {"title": f"hidden {i}", "skills": [{"skill_id": "welding"}]})
        hidden.append(collections.record_ref(DIARY, key))
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text()
                     + "refs: [" + ", ".join(f'"{ref}"' for ref in hidden) + "]\n")
    write_rule(world.root, ceiling=0)
    charge_with = run(world, {**query, "mode": "preview"}, who=_external())["visit_charge"]
    assert charge_with == charge_without


# --- 13. seed_missing restores warming state -------------------------------------------


def test_seed_missing_restores_deleted_state_rows_and_due_lists_them(world):
    """A store assembled without the create path seeds its declarations as warming."""
    ready(world)
    with world.handle.transaction() as conn:
        conn.execute("DELETE FROM item_reference_state")
        reference_index.seed_missing(conn)
        assert conn.execute(
            "SELECT count(*) FROM item_reference_state WHERE collection_id=? AND ready=0",
            (DIARY,)).fetchone()[0] == 2
        assert DIARY in reference_index.due(conn)


# --- 14. malformed view entries refuse at save time ------------------------------------


@pytest.mark.parametrize("entry", [
    {"inverse": 42},
    {"inverse": {"collection": SKILLS, "field": "site"}, "limit": 999},
    {"field": "site", "inverse": {"collection": SKILLS, "field": "site"}},
    {"inverse": {"collection": SKILLS}},
    {"inverse": {"collection": SKILLS, "field": "site"}, "extra": 1},
    42,
])
def test_a_view_with_a_malformed_related_entry_is_refused(store, entry):
    """A view that can never run must not be saved; the entry itself is what is wrong."""
    import json as _json
    block = ("\nviews:\n  v-bad:\n    query:\n      version: 1\n"
             f"      related: [{_json.dumps(entry)}]\n")
    text = skills_text().replace("\n---\n", block + "---\n")
    with pytest.raises(collections.CollectionError) as error:
        create(store, SKILLS, text, "Skills")
    assert error.value.code == "INVALID_SAVED_VIEW"


# --- 15. bounded inbound -----------------------------------------------------------------


def test_inbound_lists_at_most_64_declarations_with_total(world):
    """Inspect must bound the inbound report instead of failing on the 65th source."""
    ready(world)
    entries = [{"collection": f"00000000-0000-4000-8000-{i:012x}", "field": "site",
                "state": "ready"} for i in range(65)]
    from exomem.record_governance import inspection_inbound
    shaped = inspection_inbound({"declarations": entries[:64], "truncated": True, "total": 65})
    assert shaped == {"declarations": entries[:64], "truncated": True, "total": 65}
    assert inspection_inbound({"declarations": entries, "truncated": True, "total": 65}) is None

    # The same bound applies inside inbound(): a source declaring 65 references lists 64.
    many_fields = "".join(f"    ref{i}: {{type: string, references: {{collection: {SKILLS}}}}}\n"
                          for i in range(65))
    create(world, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
           collection_text("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                           "Many", "title", "    title: {type: string, required: true}\n"
                           + many_fields), "Many")
    while _pending(world, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"):
        world.backfill_derived("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
    from test_collection_store_importer import preview_store
    with request_scope(owner_principal()):
        with preview_store(world.root, world.handle) as writer, writer._authorization(mutation=False):
            _, manifest, _ = writer._collection(SKILLS)
            inbound = reference_index.inbound(writer._operation, writer.connection, manifest)
    assert len(inbound["declarations"]) == 64
    assert inbound["truncated"] and inbound["total"] == 66  # the diary's skills.skill_id is the 66th


# --- 16. inbound aligns with the query ----------------------------------------------------


def test_inbound_includes_a_self_reference_the_query_accepts(store):
    """A collection may declare a reference to itself; inspect lists it like any other."""
    self_ref = ("    friend: {type: string, references: "
                f"{{collection: {SKILLS}}}}}\n")
    create(store, SKILLS, skills_text(), "Skills")
    revise(store, SKILLS, skills_text().replace(
        "    slug: {type: string, required: true}\n",
        "    slug: {type: string, required: true}\n" + self_ref))
    row(store, SKILLS, SLUG, {"slug": "welding", "friend": "welding"})
    store.backfill_derived(SKILLS)
    with request_scope(owner_principal()):
        inbound = store.inspect_collection(SKILLS)["inbound"]
    assert inbound["declarations"] == [
        {"collection": SKILLS, "field": "friend", "state": "ready"}]
    result = run(store, {"related": [{"inverse": {"collection": SKILLS, "field": "friend"}}]})
    assert result["rows"][0]["related"][f"inverse:{SKILLS}:friend"]["state"] == "ready"


def test_inbound_is_empty_when_the_inspected_key_is_withheld(store):
    """A withheld natural key refuses every inverse query, so the report lists none."""
    keyed = collection_text(SKILLS, "Skills", "slug",
                            "    slug: {type: string, required: true, classification: location}\n")
    create(store, SKILLS, keyed, "Skills")
    create(store, SITES, sites_text(), "Sites")
    create(store, DIARY, diary_text(FIELDS), "Diary")
    while _pending(store, DIARY):
        store.backfill_derived(DIARY)
    from test_collection_store_importer import preview_store
    with request_scope(_external()):
        with preview_store(store.root, store.handle) as writer:
            inbound = writer.inspect_collection(SKILLS)["inbound"]
    assert inbound == {"declarations": [], "truncated": False, "total": 0}



def test_migration_seeds_only_the_current_manifest_versions(tmp_path, monkeypatch):
    """Historical versions must not seed state rows: duplicates refuse and stale ones spin."""
    from exomem.collection_store.writer import CollectionWriter
    from exomem.governance.principal import library_scope

    root = tmp_path / "rev-vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# log\n")
    path = tmp_path / "revised.sqlite"
    with monkeypatch.context() as earlier:
        earlier.setattr(schema, "SCHEMA_VERSION", 16)
        with connection.open_writer(path, lease_check=_allow) as handle, library_scope():
            writer = CollectionWriter(root, handle)
            create(writer, SITES, sites_text(), "Sites")
            create(writer, SKILLS, skills_text(), "Skills")
            # (a) declared `site`, then an unrelated revise: two versions share the declaration.
            create(writer, DIARY, diary_text(FIELDS), "Diary")
            revise(writer, DIARY, diary_text(FIELDS + "    note: {type: string}\n"))

    # Give the skills manifest a declaration, then a final version without it.
    with monkeypatch.context() as earlier:
        earlier.setattr(schema, "SCHEMA_VERSION", 16)
        with connection.open_writer(path, lease_check=_allow) as handle, library_scope():
            writer = CollectionWriter(root, handle)
            text = skills_text().replace(
                "    slug: {type: string, required: true}\n",
                "    slug: {type: string, required: true}\n"
                + SITE_FIELD.replace("site:", "buddy:"))
            revise(writer, SKILLS, text)
            revise(writer, SKILLS, skills_text())
    with connection.open_writer(path, lease_check=_allow) as handle, library_scope():
        conn = handle.connection
        assert schema.schema_version(conn) == 17
        diary_states = conn.execute(
            "SELECT count(*) FROM item_reference_state WHERE collection_id=? AND field_path='site'",
            (DIARY,)).fetchone()[0]
        assert diary_states == 1  # one state row despite two versions declaring `site`
        skills_states = conn.execute(
            "SELECT field_path FROM item_reference_state WHERE collection_id=?", (SKILLS,)).fetchall()
        assert skills_states == []  # the removed declaration seeds nothing
        assert SKILLS not in reference_index.due(conn)
