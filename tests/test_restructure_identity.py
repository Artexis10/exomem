"""A store restructure moves rows between keys and collections, and its undo moves them back.

`rekey`, `merge_into` and `split_out` run through the dispatcher that MCP, REST and the CLI share, on a real store vault.
Each case compares every row, every view file and the manifests before and after, and verifies the chain from genesis.
"""

from __future__ import annotations

import re

import pytest
from test_automatic_collection import call as store_call
from test_collection_store_service import service as service  # noqa: F401 - the fixture, by name
from test_collection_store_summary import summary_text
from test_collection_store_writer import CID, manifest_path, manifest_text
from test_collection_store_writer import store as store  # noqa: F401 - the fixture, by name
from test_restructure import WHY, make_file_work, make_store_work, stored, verify_from_genesis

from exomem import structured_collections as collections
from exomem import vault
from exomem.cli_ops import OpError
from exomem.collection_store import history
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import route


def views(store) -> dict[str, tuple[dict, str]]:
    """Every published view file under the vault's Records folder after reconcile: its front and its body, by path.

    It leaves out what every write advances: a row view's stamp names its row version, and a summary page names the
    store generation in its basis and its text. The generated history page is left out, since every call adds to it.
    """
    store.reconcile_views()
    found = {}
    for path in sorted((store.root / "Knowledge Base/Records").rglob("*.md")):
        if path.name == "_history.md":
            continue
        front, body, _ = vault.parse_frontmatter(path.read_text())
        found[path.relative_to(store.root).as_posix()] = (
            {name: value for name, value in front.items() if name not in {"exomem_view", "basis"}},
            re.sub(r"generation \d+", "generation", body))
    return found


@pytest.fixture(params=["view-per-row", "summary"])
def shape(request):
    return request.param


@pytest.fixture
def work(store, shape):
    if shape == "summary":
        store.create_collection(manifest_path(), summary_text(), why=WHY, scaffold=False)
        work = make_store_work(store, created=False)
        for title, count in (("a", 3), ("b", 4)):
            work.append({"title": title, "count": count})
        return work
    return make_store_work(store)


def test_a_rekey_moves_every_row_to_its_new_key_and_its_undo_moves_them_back(store, work):
    rows, manifest, before, files = work.rows(), work.manifest(), stored(store, CID), views(store)

    applied = work.restructure(steps=[{"step": "rekey", "key": ["title", "count"]}])
    verify_from_genesis(store, CID)
    assert work.rows() == rows
    assert set(stored(store, CID)).isdisjoint(before)
    assert work.manifest()["item_schema"]["natural_key"] == ["title", "count"]
    moved = views(store)
    work.restructure(undo=applied["shape"]["id"])
    verify_from_genesis(store, CID)

    assert (work.rows(), work.manifest(), stored(store, CID)) == (rows, manifest, before)
    assert views(store) == files
    assert set(moved) == set(files)


TARGET = "5f0c56f6-1d0e-4c55-9a43-0e7f0b7e2a11"


def other(store, shape, rows=(("a", 3), ("c", 5))):
    """A second collection, titled Estimates, of the same shape, holding `rows`."""
    text = manifest_text().replace(CID, TARGET).replace("title: Work\n", "title: Estimates\n")
    store.create_collection(manifest_path().replace("Work", "Estimates"),
                            summary_text(text) if shape == "summary" else text, why=WHY, scaffold=False)
    work = make_store_work(store, created=False, collection_id=TARGET)
    for title, count in rows:
        work.append({"title": title, "count": count})
    return work


def state(store, *works):
    """Every row, stored value and manifest of each collection, and every view file: what an undo must restore."""
    return [(work.rows(), work.manifest(), stored(store, work.collection_id)) for work in works], views(store)


def test_a_merge_copies_every_row_into_the_target_and_its_undo_takes_them_back_out(store, work, shape):
    target = other(store, shape)
    before = state(store, work, target)

    applied = work.restructure(steps=[{"step": "merge_into", "target": "Estimates"}])
    verify_from_genesis(store, CID)
    verify_from_genesis(store, TARGET)
    assert applied["shape"]["steps"] == [{"step": "merge_into", "target": TARGET}]
    assert applied["user_summary"].startswith("Work now lives in Estimates")
    assert target.rows() == {"a": {"title": "a", "count": 3}, "b": {"title": "b", "count": 4},
                             "c": {"title": "c", "count": 5}}
    assert work.manifest()["lifecycle"] == "retired" and work.manifest()["successor"] == TARGET
    assert work.rows() == before[0][0][0]
    work.restructure(undo=applied["shape"]["id"])
    verify_from_genesis(store, CID)
    verify_from_genesis(store, TARGET)

    assert state(store, work, target) == before


def test_a_merge_whose_rows_hold_other_values_under_one_key_is_refused_naming_them(store, work, shape):
    target = other(store, shape, rows=(("a", 30),))
    before = state(store, work, target)

    refused = work.refused(steps=[{"step": "merge_into", "target": TARGET}])

    assert refused["code"] == "RESTRUCTURE_MERGE_CONFLICT"
    assert "count (3 against 30)" in refused["message"]
    assert state(store, work, target) == before


def test_a_merge_undo_is_refused_when_a_copied_row_changed_in_the_target_since(store, work, shape):
    target = other(store, shape)
    applied = work.restructure(steps=[{"step": "merge_into", "target": TARGET}])
    target.update("b", {"count": 40})
    before = state(store, work, target)

    refused = work.refused(undo=applied["shape"]["id"])

    assert refused["code"] == "RESTRUCTURE_UNDO_REFUSED"
    assert work.key("b") in refused["message"]
    assert state(store, work, target) == before


def split_off(work) -> str:
    """The id of the collection that a split of `work` created, named in the split's undo."""
    (inverse,) = work.operate("inspect", collection=work.collection_id)["shape_history"]["entries"][0]["inverse"]
    return inverse["target"]


def test_a_split_moves_the_selected_rows_into_a_new_collection_and_its_undo_brings_them_back(store, work):
    before = state(store, work)

    applied = work.restructure(steps=[{"step": "split_out", "where": {"field": "title", "op": "eq", "value": "b"},
                                       "title": "Later"}])
    new = make_store_work(store, created=False, collection_id=split_off(work))
    verify_from_genesis(store, CID)
    verify_from_genesis(store, new.collection_id)
    assert work.rows() == {"a": {"title": "a", "count": 3}}
    assert new.rows() == {"b": {"title": "b", "count": 4}}
    assert new.manifest()["title"] == "Later" and new.manifest()["item_schema"] == work.manifest()["item_schema"]
    work.restructure(undo=applied["shape"]["id"])
    verify_from_genesis(store, CID)
    verify_from_genesis(store, new.collection_id)

    assert state(store, work)[0] == before[0]
    assert new.manifest()["lifecycle"] == "retired" and new.manifest()["successor"] == CID
    assert {path: view for path, view in views(store).items() if "/Later/" not in path} == before[1]


def test_a_split_verifies_each_event_of_the_store_at_most_twice(store, monkeypatch):
    """A store without a marker commits the split's new collection before the moves. A create inside the moves'
    transaction made every later retraction verify its collection's history from the first event, so a split's work
    grew with the square of the rows it moved. Now an event is verified as it commits, and once more at most, when the
    first transaction after the create's schema change starts without cached proofs."""
    store.create_collection(manifest_path(), summary_text(), why=WHY, scaffold=False)
    work = make_store_work(store, created=False)
    for count in range(24):
        work.append({"title": f"row {count}", "count": count})
    verified = []
    verify_event = history.verify_event
    monkeypatch.setattr(history, "verify_event", lambda conn, txn: verified.append(txn) or verify_event(conn, txn))

    work.restructure(steps=[{"step": "split_out", "where": {"field": "count", "op": "gte", "value": 0},
                             "title": "Apart"}])
    during = len(verified)

    with store.read_snapshot():
        events = store.connection.execute("SELECT COUNT(*) FROM txns").fetchone()[0]
    assert len(make_store_work(store, created=False, collection_id=split_off(work)).rows()) == 24
    assert 0 < during <= 2 * events


def test_a_rekey_undo_is_refused_when_a_row_written_since_collides_under_the_old_key(store, work):
    applied = work.restructure(steps=[{"step": "rekey", "key": ["title", "count"]}])
    work.append({"title": "a", "count": 9})
    before = state(store, work)

    refused = work.refused(undo=applied["shape"]["id"])

    assert refused["code"] == "RESTRUCTURE_STEP_REFUSED"
    assert "share a value under the new key" in refused["message"]
    assert state(store, work) == before


@pytest.mark.parametrize("step", [
    {"step": "rekey", "key": ["title", "count"]},
    {"step": "merge_into", "target": "Estimates"},
    {"step": "split_out", "where": {"field": "title", "op": "eq", "value": "b"}, "title": "Later"},
], ids=["rekey", "merge_into", "split_out"])
def test_an_identity_move_on_a_collection_kept_only_in_files_is_refused_and_changes_nothing(tmp_path, step):
    work = make_file_work(tmp_path)
    before = (work.rows(), work.read_manifest_text(), sorted(path.name for path in tmp_path.rglob("*.md")))

    refused = work.refused(steps=[step])

    assert refused["code"] == "RESTRUCTURE_MOVE_UNAVAILABLE"
    assert refused["remediation"]
    assert (work.rows(), work.read_manifest_text(), sorted(path.name for path in tmp_path.rglob("*.md"))) == before


def test_a_served_store_merges_and_undoes(service):
    from test_collection_store_service import CID as SERVED
    from test_collection_store_service import DAILY, DAILY_PATH, DAILY_TEXT, call, titles

    root = service.root
    call(root, "record_memory", action="create", manifest_path=DAILY_PATH, manifest_text=DAILY_TEXT, why=WHY)
    call(root, "record_memory", action="append", collection=DAILY, item={"title": "Later"}, why=WHY)

    applied = call(root, "record_memory", action="restructure", collection=SERVED, why=WHY,
                   steps=[{"step": "merge_into", "target": "Daily"}])
    merged = titles(root, DAILY)
    call(root, "record_memory", action="restructure", collection=SERVED, why=WHY, undo=applied["shape"]["id"])

    assert merged == ["Canonical", "Later"]
    assert (titles(root, SERVED), titles(root, DAILY)) == (["Canonical"], ["Later"])


def served_state(root, *collections_):
    """Each collection's rows and manifest, and every published view file, once the service has published them all.

    A row that moved and came back is its next version, so its version token is left out. The new collection's folder
    is left out too, since a split's undo retires that collection and keeps it.
    """
    from contextlib import closing

    from test_collection_store_s1_gate import _until
    from test_collection_store_service import call

    from exomem.collection_store import connection

    def states():
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            return {state for (state,) in reader.execute("SELECT state FROM projection_state WHERE kind!='history'")}

    _until(lambda: states() == {"current"})
    held = [([{name: value for name, value in row.items() if name not in {"item_version", "id"}}
              for row in call(root, "record_memory", action="query", collection=cid)["rows"]],
             served_manifest(root, cid)) for cid in collections_]
    files = {}
    for path in sorted((root / "Knowledge Base/Records").rglob("*.md")):
        if path.name != "_history.md" and "/Apart/" not in path.as_posix():
            front, body, _ = vault.parse_frontmatter(path.read_text())
            files[path.relative_to(root).as_posix()] = (
                {name: value for name, value in front.items() if name not in {"exomem_view", "basis"}},
                re.sub(r"generation \d+", "generation", body))
    return held, files


def served_manifest(root, collection_id) -> str:
    """The collection's current manifest text in the served store."""
    from contextlib import closing

    from exomem.collection_store import connection

    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute("SELECT manifest_text FROM collection_manifests WHERE collection_id=? "
                              "ORDER BY manifest_version DESC LIMIT 1", (collection_id,)).fetchone()[0]


def served_chains(root, *collection_ids):
    """Verify each collection's chain and the store chain from genesis on the served store, through a reader."""
    from contextlib import closing

    from exomem.collection_store import connection, history

    with closing(connection.open_reader(connection.store_path(root))) as reader:
        for cid in collection_ids:
            history.verify(reader, cid)


SPLIT = [{"step": "split_out", "where": {"field": "title", "op": "eq", "value": "Canonical"}, "title": "Apart"}]


def test_a_served_store_splits_through_its_admitted_create_and_the_undo_brings_the_rows_back(service):
    from test_collection_store_service import CID as SERVED
    from test_collection_store_service import call, titles

    root = service.root
    call(root, "record_memory", action="append", collection=SERVED, item={"title": "Kept"}, why=WHY)
    before = served_state(root, SERVED)

    applied = call(root, "record_memory", action="restructure", collection=SERVED, why=WHY, steps=SPLIT)
    (inverse,) = applied["shape"]["inverse"]
    apart = inverse["target"]
    split = (titles(root, SERVED), titles(root, apart))
    served_chains(root, SERVED, apart)
    call(root, "record_memory", action="restructure", collection=SERVED, why=WHY, undo=applied["shape"]["id"])
    served_chains(root, SERVED, apart)

    assert split == (["Kept"], ["Canonical"])
    assert served_state(root, SERVED) == before
    manifest = vault.parse_frontmatter(served_manifest(root, apart), strict=True)[0]
    assert (manifest["title"], manifest["lifecycle"], manifest["successor"]) == ("Apart", "retired", SERVED)


def test_a_served_split_that_fails_after_its_create_completes_on_the_same_call_again(service, monkeypatch):
    from contextlib import closing

    from test_collection_store_service import CID as SERVED
    from test_collection_store_service import call, titles

    from exomem.collection_store import connection
    from exomem.collection_store.writer import CollectionWriter

    root = service.root
    call(root, "record_memory", action="append", collection=SERVED, item={"title": "Kept"}, why=WHY)

    def collections_held():
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            return reader.execute("SELECT COUNT(*) FROM collections").fetchone()[0]

    held = collections_held()
    commit = CollectionWriter.restructure_collection
    with monkeypatch.context() as crash:
        def fail(*_args, **_kwargs):
            raise RuntimeError("the service stopped between the create and the moves")

        crash.setattr(CollectionWriter, "restructure_collection", fail)
        with pytest.raises((OpError, RuntimeError)):
            call(root, "record_memory", action="restructure", collection=SERVED, why=WHY, steps=SPLIT)
    begun = collections_held()
    assert CollectionWriter.restructure_collection is commit

    applied = call(root, "record_memory", action="restructure", collection=SERVED, why=WHY, steps=SPLIT)
    (inverse,) = applied["shape"]["inverse"]
    served_chains(root, SERVED, inverse["target"])

    assert (begun, collections_held()) == (held + 1, held + 1)
    assert (titles(root, SERVED), titles(root, inverse["target"])) == (["Kept"], ["Canonical"])


DIARY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def diary(store, site):
    """A Diary collection whose one row, `visit`, references the Work row that `site` names by its key."""
    text = manifest_text().replace(CID, DIARY).replace("title: Work\n", "title: Diary\n").replace(
        "    count: {type: integer}\n", f"    site: {{type: string, references: {{collection: {CID}}}}}\n")
    store.create_collection(manifest_path().replace("Work", "Diary"), text, why=WHY, scaffold=False)
    store.append_record(DIARY, item={"title": "visit", "site": site}, why=WHY)


def build_diary_index(store):
    """One batch of the reference-index build that the serving loop runs; it covers Diary's one row."""
    store.backfill_derived(DIARY, projection=False, rollup=False)


def split(title):
    return {"step": "split_out", "where": {"field": "title", "op": "eq", "value": title}, "title": "Apart"}


def test_a_split_of_a_row_that_another_collection_references_is_refused_naming_the_reference(store):
    """Until Diary's reference index is built the restructure cannot check it, so it refuses as busy."""
    work = make_store_work(store)
    diary(store, site="a")
    rows = work.rows()

    waiting = work.refused(steps=[split("a")])
    build_diary_index(store)
    refused = work.refused(steps=[split("a")])
    unchanged = work.rows()
    work.restructure(steps=[split("b")])

    assert waiting["code"] == "COLLECTION_STORE_BUSY"
    assert refused["code"] == "RESTRUCTURE_DEPENDENT_BROKEN"
    assert "collection Diary references this collection through item_schema.fields.site.references" in refused["message"]
    assert unchanged == rows
    assert list(work.rows()) == ["a"]


def test_a_rekey_to_a_key_that_a_reference_cannot_match_is_refused_naming_the_reference(store):
    work = make_store_work(store)
    diary(store, site="a")

    refused = work.refused(steps=[{"step": "rekey", "key": ["title", "count"]}])

    assert refused["code"] == "RESTRUCTURE_DEPENDENT_BROKEN"
    assert "collection Diary references this collection through item_schema.fields.site.references" in refused["message"]
    assert "natural key must be a single field" in refused["message"]


def test_a_rekey_rebuilds_the_reference_index_of_the_collections_that_name_it(store):
    """Diary's `c1` named no Work row by title; once Work is keyed by code, the inverse read of row c1 finds it."""
    text = manifest_text().replace("    count: {type: integer}\n", "    count: {type: integer}\n    code: {type: string}\n")
    store.create_collection(manifest_path(), text, why=WHY, scaffold=False)
    work = make_store_work(store, created=False)
    work.append({"title": "a", "count": 3, "code": "c1"})
    work.append({"title": "b", "count": 4, "code": "c2"})
    diary(store, site="c1")
    build_diary_index(store)

    work.restructure(steps=[{"step": "rekey", "key": ["code"]}])
    build_diary_index(store)
    with request_scope(owner_principal()):
        found = route.run(store, CID, {"version": 1, "select": ["code"], "related": [
            {"inverse": {"collection": DIARY, "field": "site"}}]}, facade_profile="records")

    counts = {row["code"]: row["related"][f"inverse:{DIARY}:site"]["count"] for row in found["rows"]}
    assert counts == {"c1": 1, "c2": 0}


ROADMAP = "7c9d2b1e-4f3a-4e8b-9a6d-1b2c3d4e5f60"


@pytest.fixture
def plan(store):
    """`plan_memory` on a store vault holding two Planning collections: Work, with an area, an outcome in it and an
    initiative under the outcome, and Roadmap, with one item of its own."""
    store.create_collection(manifest_path("planning"), manifest_text("planning"), why=WHY, scaffold=False)
    store.create_collection(
        manifest_path("planning").replace("Work", "Roadmap"),
        manifest_text("planning").replace(CID, ROADMAP).replace("title: Work\n", "title: Roadmap\n"),
        why=WHY, scaffold=False)

    def plan(action, **arguments):
        return store_call(store, "plan_memory", action=action, **arguments)

    def add(item):
        return plan("add", collection=CID, item=item, why=WHY)["plan_id"]

    home = add({"title": "Home", "kind": "area"})
    ship = add({"title": "Ship", "kind": "outcome", "area": collections.plan_ref(CID, home)})
    add({"title": "Build", "kind": "initiative", "parent": collections.plan_ref(CID, ship),
         "area": collections.plan_ref(CID, home)})
    plan("add", collection=ROADMAP, item={"title": "Launch", "kind": "outcome"}, why=WHY)
    return plan


def items(plan, collection) -> dict[str, dict]:
    """Each item of a Planning collection by title: its plan id, and the plan ids its parent and area name there."""
    def named(value):
        parsed = collections.parse_plan_ref(value) if value else None
        return parsed and (parsed[0] == collection or None) and parsed[1]

    return {row["title"]: {"plan_id": row["plan_id"], "parent": named(row.get("parent")), "area": named(row.get("area"))}
            for row in plan("query", collection=collection, filters=[])["rows"]}


def plan_refused(call) -> dict:
    with pytest.raises(OpError) as error:
        call()
    return error.value.as_public_dict()


def test_a_planning_merge_keeps_each_plan_id_and_its_parent_and_area_resolve_in_the_target(plan):
    work, roadmap = items(plan, CID), items(plan, ROADMAP)

    applied = plan("restructure", collection=CID, why=WHY, steps=[{"step": "merge_into", "target": "Roadmap"}])
    merged = items(plan, ROADMAP)
    later = plan("add", collection=ROADMAP, why=WHY, item={
        "title": "Polish", "kind": "initiative", "parent": collections.plan_ref(ROADMAP, work["Ship"]["plan_id"])})

    assert {title: merged[title] for title in work} == work
    assert merged["Launch"] == roadmap["Launch"]
    assert later["outcome"] == "committed"
    assert plan("inspect", collection=ROADMAP)["contract"]["semantic_profile"] == "planning"
    assert applied["shape"]["steps"] == [{"step": "merge_into", "target": ROADMAP}]


def test_a_planning_collection_that_a_merge_retired_points_its_read_and_its_refused_add_to_the_target(plan):
    plan("restructure", collection=CID, why=WHY, steps=[{"step": "merge_into", "target": "Roadmap"}])

    queried = plan("query", collection=CID)
    refused = plan_refused(lambda: plan("add", collection=CID, item={"title": "Late", "kind": "outcome"}, why=WHY))

    assert queried["retired"] is True and queried["successor"] == ROADMAP
    assert refused["code"] == "COLLECTION_RETIRED" and ROADMAP in refused["remediation"]


def test_a_planning_merge_undo_restores_both_collections(plan, store):
    before = (items(plan, CID), items(plan, ROADMAP), stored(store, CID), stored(store, ROADMAP))

    applied = plan("restructure", collection=CID, why=WHY, steps=[{"step": "merge_into", "target": "Roadmap"}])
    plan("restructure", collection=CID, why=WHY, undo=applied["shape"]["id"])
    verify_from_genesis(store, CID)
    verify_from_genesis(store, ROADMAP)

    assert (items(plan, CID), items(plan, ROADMAP), stored(store, CID), stored(store, ROADMAP)) == before


def test_a_planning_split_that_keeps_items_with_their_parent_and_area_moves_them_with_their_plan_ids(plan, store):
    work = items(plan, CID)

    plan("restructure", collection=CID, why=WHY, steps=[{"step": "split_out", "title": "Shipping", "where": {
        "field": "title", "op": "in", "value": ["Home", "Ship", "Build"]}}])
    (entry,) = plan("inspect", collection=CID)["shape_history"]["entries"][:1]
    shipping = entry["inverse"][0]["target"]
    verify_from_genesis(store, shipping)

    assert items(plan, shipping) == work and items(plan, CID) == {}


def test_a_planning_split_that_parts_an_item_from_its_parent_or_area_is_refused_naming_it(plan):
    work = items(plan, CID)

    refused = plan_refused(lambda: plan("restructure", collection=CID, why=WHY, steps=[{
        "step": "split_out", "title": "Apart", "where": {"field": "title", "op": "eq", "value": "Build"}}]))

    assert refused["code"] == "RESTRUCTURE_STEP_REFUSED"
    assert f"{work['Build']['plan_id']} (parent {work['Ship']['plan_id']})" in refused["message"]
    assert f"{work['Build']['plan_id']} (area {work['Home']['plan_id']})" in refused["message"]
    assert items(plan, CID) == work


def test_a_planning_merge_undo_that_would_part_a_later_item_from_its_parent_is_refused_naming_it(plan):
    applied = plan("restructure", collection=CID, why=WHY, steps=[{"step": "merge_into", "target": "Roadmap"}])
    merged = items(plan, ROADMAP)
    polish = plan("add", collection=ROADMAP, why=WHY, item={
        "title": "Polish", "kind": "initiative", "parent": collections.plan_ref(ROADMAP, merged["Ship"]["plan_id"])})

    refused = plan_refused(lambda: plan("restructure", collection=CID, why=WHY, undo=applied["shape"]["id"]))

    assert refused["code"] == "RESTRUCTURE_STEP_REFUSED"
    assert f"{polish['plan_id']} (parent {merged['Ship']['plan_id']})" in refused["message"]
    assert set(items(plan, ROADMAP)) == {*merged, "Polish"}


def test_a_planning_merge_that_renames_a_core_field_is_refused(plan):
    refused = plan_refused(lambda: plan("restructure", collection=CID, why=WHY, steps=[
        {"step": "merge_into", "target": "Roadmap", "mapping": {"priority": "rank"}}]))

    assert refused["code"] == "RESTRUCTURE_STEP_REFUSED" and "Planning core field" in refused["message"]


def test_a_retitle_or_split_answers_a_title_only_a_withheld_collection_holds_as_a_free_title(store, tmp_path, monkeypatch):
    """The refusal of a taken title must not tell an author that a collection they cannot read exists."""
    from test_collection_row_references import DIARY, SITES, create, diary_text, row, sites_text
    from test_collection_store_governance import _record_writer
    from test_governance_egress import write_rule, write_scope

    from exomem.governance.principal import request_scope

    create(store, SITES, sites_text(), "Sites")
    create(store, DIARY, diary_text(""), "Diary")
    row(store, DIARY, "00000000-0000-4000-8000-0000000000c1", {"title": "kept"})
    row(store, DIARY, "00000000-0000-4000-8000-0000000000c2", {"title": "moved"})
    store.reconcile_views()
    write_scope(store.root, paths="Records/Sites/**")
    write_rule(store.root, ceiling=0)
    author = _record_writer(store, tmp_path, monkeypatch)
    split = lambda title: [{"step": "split_out", "where": {"field": "title", "op": "eq", "value": "moved"},  # noqa: E731
                            "title": title}]
    retitle = lambda title: [{"step": "retitle", "title": title}]  # noqa: E731

    def preview(steps):
        with request_scope(author):
            try:
                return store.preview_restructure(DIARY, why=WHY, steps=steps)["valid"]
            except collections.CollectionError as error:
                return error.code, error.reason

    with request_scope(author), pytest.raises(collections.CollectionError) as hidden:
        store.inspect_collection(SITES)
    assert hidden.value.code == "COLLECTION_NOT_FOUND"
    assert preview(retitle("Sites")) == preview(retitle("Nothing Here")) is True
    assert preview(split("Sites")) == preview(split("Nothing Here")) is True

