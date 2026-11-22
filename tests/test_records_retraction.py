"""Retracting a store-backed Records row (OpenSpec add-records-retraction).

A retracted row leaves current state and keeps its history; writing its natural key again brings
the same row back as its next version. Every case drives the real CollectionWriter on an invented
store and names the defect only it catches.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from test_collection_query_index_migrations import indexed_text
from test_collection_rollups import by_bucket, reduce, request
from test_collection_rollups import manifest as rollup_manifest
from test_collection_row_reference_index import INVERSE, _implied, _indexed, ready
from test_collection_row_reference_queries import FIELDS as DIARY_FIELDS
from test_collection_row_references import (
    DIARY,
    SITES,
    SKILLS,
    SLUG,
    diary_text,
    row,
    sites_text,
    skills_text,
)
from test_collection_row_references import (
    create as create_collection,
)
from test_collection_store_governance import withhold
from test_collection_store_import_exports import agent, finish, logged, preserve_export
from test_collection_store_importer import CID as WORKOUTS
from test_collection_store_importer import manifest_text as importer_manifest_text
from test_collection_store_importer import (
    ndjson,
    run,
    setup,
    start,
    status,
    summarized,
    write_source,
)
from test_collection_store_summary import bulk, create, pages, summary_text
from test_collection_store_writer import CID, KEY, manifest_path, manifest_text
from test_collection_store_writer import store as store  # noqa: F401
from test_governance_egress import _external, write_rule, write_scope
from test_records_bulk_upsert import _evidence
from test_typed_history_generations import before_retractable_rows

from exomem import (
    command_surface,
    commands,
    memory_refs,
    mutation_terminal,
    page_ids,
    record_history,
    records,
)
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import (
    chain,
    connection,
    history,
    index_migrations,
    schema,
    typed_storage,
)
from exomem.collection_store.preview import preview_store
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope, owner_principal, request_scope
from exomem.query_engine import route
from exomem.record_memory import record_memory

ABSENT_KEY = "44444444-4444-4444-8444-444444444444"
WRONG, KEPT = "Wrong entry", "Kept entry"


@pytest.fixture(autouse=True)
def owner():
    with request_scope(owner_principal()):
        yield


def retract(store, key, why="entered in error", cid=CID):
    container, version = store.current_guards(cid, key)
    return store.retract_record(
        cid, item_key=key, expected_container_hash=container, expected_item_version=version, why=why)


def titles(store, cid=CID, **arguments):
    with preview_store(store.root, store.handle):
        found = record_memory(store.root, "query", collection=cid, columns=["title"], **arguments)
    return sorted(item["title"] for item in found["rows"])


def log_manifest():
    """The writer fixture's markdown log with the heading and child-row grammar a store-owned log declares."""
    return manifest_text(layout="markdown-log").replace(
        "  item: {level: 3, key: record_id}",
        "  item_heading: {level: 3, fields: [{name: title, type: string}], separator: ' · '}\n"
        "  child_rows: {prefix: '- ', delimiter: '|', fields: [field, value], container_field: details}\n"
        "  insertion: newest-first",
    ).replace("    count: {type: integer}", "    count: {type: integer}\n    details: {type: array, items: {type: object}}")


def append(store, title, count=1, cid=CID):
    return store.append_record(cid, item={"title": title, "count": count}, why="observe")["item_key"]


# --- T1 the row leaves current state ---------------------------------------------------------


def test_a_retracted_summary_row_leaves_queries_counts_and_the_page(store):
    """A read of the `items` table instead of the current rows keeps showing the row it should have dropped."""
    create(store)
    wrong = append(store, WRONG)
    append(store, KEPT, 2)
    retract(store, wrong)
    assert titles(store) == [KEPT]
    assert titles(store, filters=[{"column": "title", "op": "eq", "value": WRONG}]) == []
    assert store.inspect_collection(CID)["coverage"]["committed"] == 1
    store.reconcile_views()
    assert "metric: committed rows\nvalue: 1\n" in (store.root / pages(store)[0]).read_text()


def test_a_retracted_log_row_leaves_the_log_and_the_count(store):
    """A log is rendered from current rows; a retraction the render misses leaves the entry in the file."""
    store.create_collection(manifest_path(), log_manifest(), why="create")
    wrong = append(store, WRONG)
    append(store, KEPT, 2)
    store.reconcile_views()
    log = store.root / "Knowledge Base/Records/Work/Log.md"
    assert WRONG in log.read_text()
    retract(store, wrong)
    store.reconcile_views()
    assert WRONG not in log.read_text() and KEPT in log.read_text()
    assert titles(store) == [KEPT]
    assert store.inspect_collection(CID)["coverage"]["committed"] == 1


def test_a_retracted_row_id_is_refused_like_an_absent_id(store):
    """An id that answers differently for a retracted row confirms that the row once existed."""
    create(store)
    append(store, WRONG)
    absent = page_ids.part_id(ABSENT_KEY, "x")

    def refusal(item_id):
        with preview_store(store.root, store.handle), pytest.raises(OpError) as error:
            record_memory(store.root, "update", collection=CID, item_id=item_id, changes={"title": "x"}, why="try")
        return error.value.code, error.value.details, str(error.value).replace(item_id, "<id>")

    with preview_store(store.root, store.handle):
        row_id = record_memory(store.root, "query", collection=CID, columns=["title"])["rows"][0]["id"]
        receipt = record_memory(store.root, "retract", collection=CID, item_id=row_id, why="entered in error")
    assert receipt["operation"] == "retract" and receipt["outcome"] == "committed"
    assert refusal(row_id) == refusal(absent) and refusal(row_id)[0] == "ITEM_NOT_FOUND"


def test_the_person_reads_that_the_row_was_retracted_not_that_its_page_was_deleted(store):
    """The receipt names the collection's page, which stays; "Deleted -> <page>" tells the person it is gone."""
    create(store)
    receipt = retract(store, append(store, WRONG))
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "record_memory")
    terminal = mutation_terminal.committed_terminal(
        receipt, request_id="11111111-1111-4111-8111-111111111111", receipt_id="receipt-1", idempotency_key=None)
    verb = command_surface.outcome_verb_of(command, {"action": "retract"})
    summary = mutation_terminal.project_terminal(terminal, outcome_verb=verb)["user_summary"]
    assert summary.startswith(f"Retracted -> {pages(store)[0]} (")


# --- T2 history and the chain -----------------------------------------------------------------


def test_row_history_shows_the_versions_and_the_retraction_with_its_reason(store):
    """The retraction is an audited effect: a history that drops it cannot say who took the row out or why."""
    create(store)
    key = append(store, WRONG)
    container, version = store.current_guards(CID, key)
    store.update_record(CID, item_key=key, changes={"count": 2}, expected_container_hash=container,
                        expected_item_version=version, why="correct the count")
    retract(store, key, why="a validation defect admitted this row")
    with preview_store(store.root, store.handle):
        history = record_history.read(store.root, CID, item_key=key, semantic_profile="records")
        agent = records.agent_audit_history(store.root, CID)
    assert [(revision["operation"], revision["why"]) for revision in history["revisions"]] == [
        ("retract", "a validation defect admitted this row"), ("update", "correct the count")]
    assert history["revisions"][0]["prior"]["values"] == {"title": WRONG, "count": 2}
    assert history["revisions"][0]["after_item_hash"] is None
    assert agent["events"][0]["operation"] == "retract" and agent["events"][0]["item_key"] == key


def test_a_retracted_rows_history_opens_after_the_collection_joins_a_project(store):
    """A row inherits the manifest's projects; a revise that reaches only current rows leaves a retracted row on the old ones."""
    create(store)
    key = append(store, WRONG)
    retract(store, key)
    guards = store.inspect_collection(CID)["lifecycle_guards"]
    store.revise_collection(CID, manifest_text=summary_text().replace("title: Work", "title: Work\nprojects: [Alpha]"),
                            why="file the collection under a project", **guards)
    with preview_store(store.root, store.handle):
        history = record_history.read(store.root, CID, item_key=key, semantic_profile="records")
        agent = records.agent_audit_history(store.root, CID)
    assert [revision["operation"] for revision in history["revisions"]] == ["retract"]
    assert history["revisions"][0]["prior"]["values"] == {"title": WRONG, "count": 1}
    assert [event["operation"] for event in agent["events"]] == ["revise", "retract", "append", "create"]


def test_revising_a_collection_reprojects_every_retracted_row_in_bounded_batches(store):
    """Retracted rows keep their own tags whatever their number; a title-only revise leaves them alone and a project change reaches each."""
    create(store)
    keys = [append(store, f"row-{n}") for n in range(30)]
    for key in keys:
        retract(store, key)
    for n, row_id in enumerate(r[0] for r in store.connection.execute(
            "SELECT row_id FROM item_rows WHERE retracted_txn IS NOT NULL ORDER BY row_id")):
        store.connection.execute("UPDATE item_rows SET governance_json=? WHERE row_id=?",
                                 (f'{{"classes":[],"projects":[],"tags":["t{n}"]}}', row_id))
    statements = []
    store.connection.set_trace_callback(statements.append)
    guards = store.inspect_collection(CID)["lifecycle_guards"]
    store.revise_collection(CID, manifest_text=summary_text().replace("title: Work", "title: Renamed"),
                            why="rename only", **guards)
    touched = [q for q in statements if "item_rows" in q and "retracted_txn IS NOT NULL" in q]
    guards = store.inspect_collection(CID)["lifecycle_guards"]
    store.revise_collection(CID, manifest_text=summary_text().replace("title: Work", "title: Renamed\nprojects: [Alpha]"),
                            why="file under a project", **guards)
    store.connection.set_trace_callback(None)
    assert touched == []
    stored = [r[0] for r in store.connection.execute(
        "SELECT governance_json FROM item_rows WHERE retracted_txn IS NOT NULL ORDER BY row_id")]
    assert stored == [f'{{"classes":[],"projects":["alpha"],"tags":["t{n}"]}}' for n in range(30)]


def test_a_cold_reopen_verifies_the_chain_and_a_later_write_commits(store):
    """A chain whose hash omits the retract effect verifies on the writer's connection and fails on a fresh one."""
    create(store)
    key = append(store, WRONG)
    retract(store, key)
    with closing(connection.open_reader(store.handle.path)) as cold:
        head = chain.verify_store_chain(cold)
    later = store.append_record(CID, item={"title": KEPT}, why="observe")
    assert later["outcome"] == "committed"
    with closing(connection.open_reader(store.handle.path)) as cold:
        assert chain.verify_store_chain(cold)[0] == head[0] + 1
    assert store.inspect_collection(CID)["audit"] == {"status": "ok", "gaps": []}


# --- T3 re-admission ---------------------------------------------------------------------------


def test_appending_a_retracted_natural_key_brings_the_same_row_back(store):
    """A key twin splits the row's history; reading identical content as a replay drops the write."""
    create(store)
    key = append(store, WRONG)
    (row_id,) = store.connection.execute("SELECT row_id FROM item_rows WHERE item_key=?", (key,)).fetchone()
    retract(store, key)
    again = store.append_record(CID, item={"title": WRONG, "count": 1}, why="entered again")
    assert (again["outcome"], again["item_key"]) == ("committed", key)
    assert store.connection.execute(
        "SELECT row_id, row_version, retracted_txn FROM item_rows WHERE item_key=?", (key,)).fetchall() == [(row_id, 2, None)]
    assert titles(store) == [WRONG]
    with preview_store(store.root, store.handle):
        history = record_history.read(store.root, CID, item_key=key, semantic_profile="records")
    assert [revision["operation"] for revision in history["revisions"]] == ["append", "retract"]


def test_bulk_upsert_of_a_retracted_row_inserts_it_again(store):
    """Identical content must read as an insert for a retracted row and as unchanged for a current one."""
    create(store)
    first = bulk(store, [{"title": WRONG, "count": 1}, {"title": KEPT, "count": 2}])
    wrong = first["rows"][0]["item_key"]
    (row_id,) = store.connection.execute("SELECT row_id FROM item_rows WHERE item_key=?", (wrong,)).fetchone()
    retract(store, wrong)
    again = bulk(store, [{"title": WRONG, "count": 1}, {"title": KEPT, "count": 2}])
    assert again["counts"] == {"inserted": 1, "updated": 0, "unchanged": 1, "rejected": 0}
    assert [item["outcome"] for item in again["rows"]] == ["inserted", "unchanged"]
    assert store.connection.execute(
        "SELECT row_id FROM items WHERE item_key=?", (wrong,)).fetchall() == [(row_id,)]
    assert titles(store) == sorted([WRONG, KEPT])


def test_an_import_of_the_same_source_restores_a_retracted_row(store):
    """An import that restates every row must still restore the one a retraction took out."""
    rows = [{"id": f"r{n}", "kind": "run"} for n in range(3)]
    mapping = {"fields": {"exercise_id": "id", "kind": "kind"}, "on_invalid": "skip",
               "coverage": {name: {"classification": None} for name in ("id", "kind")}}
    setup(store, ndjson(rows), text=summarized(importer_manifest_text()))
    start(store, mapping=mapping)
    run(store)
    key, row_id = store.connection.execute("SELECT item_key, row_id FROM items WHERE collection_id=? "
                                           "ORDER BY row_id LIMIT 1", (WORKOUTS,)).fetchone()
    retract(store, key, cid=WORKOUTS)
    write_source(store.root, ndjson(reversed(rows)))  # the same rows, so another source and another job
    job = start(store, mapping=mapping)
    run(store)
    counts = status(store, job)["rows"]
    assert (counts["inserted"], counts["restated_existing"]) == (1, 2)
    assert store.connection.execute("SELECT row_id FROM items WHERE item_key=?", (key,)).fetchall() == [(row_id,)]
    assert store.connection.execute(
        "SELECT COUNT(*) FROM items WHERE collection_id=?", (WORKOUTS,)).fetchone() == (3,)


# --- T4 refusals --------------------------------------------------------------------------------


def test_stale_guards_write_nothing(store):
    """A retraction that proceeds on a stale guard takes out a row the caller never saw."""
    create(store)
    key = append(store, WRONG)
    container, version = store.current_guards(CID, key)
    before = tuple(store.connection.iterdump())
    for stale, name in (({"expected_container_hash": "0" * 64}, "container"), ({"expected_item_version": "0" * 64}, "item")):
        with pytest.raises(collections.CollectionError, match=f"STALE_RECORD.*{name}"):
            store.retract_record(CID, item_key=key, why="stale",
                                 **{"expected_container_hash": container, "expected_item_version": version, **stale})
    assert tuple(store.connection.iterdump()) == before


def test_an_absent_key_and_a_retracted_key_refuse_identically(store):
    """A refusal that differs for a retracted key confirms that the row once existed."""
    create(store)
    key = append(store, WRONG)
    container, version = store.current_guards(CID, key)
    retract(store, key)

    def refusal(item_key):
        with pytest.raises(collections.CollectionError) as error:
            store.retract_record(CID, item_key=item_key, expected_container_hash=container,
                                 expected_item_version=version, why="again")
        return error.value.code, error.value.details, str(error.value).replace(item_key, "<key>")

    assert refusal(key) == refusal(ABSENT_KEY) and refusal(key)[0] == "RECORD_NOT_FOUND"


def test_a_restricted_caller_gets_the_collection_refusal_and_nothing_is_written(store):
    """A retraction that skips the collection's read authority lets a caller edit what it cannot see."""
    create(store)
    key = append(store, WRONG)
    container, version = store.current_guards(CID, key)
    withhold(store)
    before = tuple(store.connection.iterdump())
    with request_scope(_external()), pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
        store.retract_record(CID, item_key=key, expected_container_hash=container,
                             expected_item_version=version, why="try")
    assert tuple(store.connection.iterdump()) == before


def test_a_hold_on_a_retracted_row_stays_withheld_with_that_row(store):
    """A hold is released no further than the row it would change; a hold that loses its row at a retraction shows a withheld row's values."""
    text = summary_text(manifest_text().replace("    count:", "    classes: {type: array, items: {type: string}}\n    count:"))
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    key = store.append_record(CID, item={"title": WRONG, "classes": ["Private"]}, why="observe")["item_key"]
    container, version = store.current_guards(CID, key)
    with pytest.raises(collections.CollectionError, match="SCHEMA_FIELD_TYPE"):
        store.update_record(CID, item_key=key, changes={"classes": [], "count": "many"}, why="declassify",
                            expected_container_hash=container, expected_item_version=version)
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "classes: [private]\n")  # withholds the row; the refused update carries no class
    write_rule(store.root, ceiling=0)

    def held(who):
        with request_scope(who):
            return store.inspect_collection(CID)["coverage"]["held"]

    assert (held(owner_principal()), held(_external())) == (1, 0)
    retract(store, key)
    assert (held(owner_principal()), held(_external())) == (1, 0)
    # A revise has no current row to judge the hold against; the hold keeps the retracted row's bound.
    guards = store.inspect_collection(CID)["lifecycle_guards"]
    store.revise_collection(CID, manifest_text=text.replace("title: Work", "title: Work\nprojects: [Alpha]"),
                            why="file the collection under a project", **guards)
    assert (held(owner_principal()), held(_external())) == (1, 0)


def test_a_collection_that_renders_one_view_per_row_refuses_retraction(store):
    """A retraction there would leave the row's own view file showing a row the store dropped."""
    create(store, summary=False)
    key = append(store, WRONG)
    before = tuple(store.connection.iterdump())
    with pytest.raises(collections.CollectionError) as error:
        retract(store, key)
    assert error.value.code == "RETRACT_UNAVAILABLE"
    assert tuple(store.connection.iterdump()) == before


def test_a_file_canonical_collection_refuses_retraction(tmp_path):
    """The store cannot take a row out of state that a file holds."""
    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base/log.md").write_text("# Log\n")
    records.create_collection(tmp_path, "Knowledge Base/Records/Work/_collection.md", manifest_text(),
                              why="file collection", scaffold=True)
    with pytest.raises(OpError) as error:
        record_memory(tmp_path, "retract", collection=CID, item_key=ABSENT_KEY, why="try",
                      expected_container_hash="0" * 64, expected_item_version="0" * 64)
    assert error.value.code == "RETRACT_UNAVAILABLE"


# --- T5 derived state ---------------------------------------------------------------------------


@pytest.fixture
def diary(store):
    """A diary with a filterable `hours`, a declared skill reference and a skill collection that is referred to."""
    create_collection(store, SITES, sites_text(), "Sites")
    create_collection(store, SKILLS, skills_text(), "Skills")
    create_collection(store, DIARY, diary_text(DIARY_FIELDS).replace(
        "title: {type: string, required: true}", "title: {type: string, required: true, filterable: true}"), "Diary")
    row(store, SKILLS, SLUG, {"slug": "welding"})
    for title, hours in (("day one", 3), ("day two", 6)):
        store.append_record(DIARY, item={"title": title, "hours": hours, "skills": [{"skill_id": "welding"}]},
                            why="record")
    return ready(store)


def diary_titles(store, **query):
    with request_scope(owner_principal()):
        found = route.run(store, DIARY, {"version": 1, "select": ["title"], **query}, facade_profile="records")
    return sorted(item["title"] for item in found["rows"])


def derived_row_ids(store, cid):
    """The row ids of each relation that holds a collection's current rows: its typed relation and ready filter projection.

    A query joins both to the current rows, so only a direct read sees a retracted row left in them.
    """
    layout = typed_storage.ready_layout(store.connection, cid)
    plan = index_migrations.ready_plan(store.connection, cid)
    tables = [table for table in (layout and layout.current_relation, plan and plan.table_name) if table]
    return [sorted(row_id for (row_id,) in store.connection.execute(f"SELECT row_id FROM {table}"))  # noqa: S608 - each names its own table
            for table in tables]


def current_row_ids(store, cid):
    return sorted(row_id for (row_id,) in store.connection.execute(
        "SELECT row_id FROM items WHERE collection_id=?", (cid,)))


def holds_exactly_the_current_rows(store, cid):
    derived = derived_row_ids(store, cid)
    return bool(derived) and all(ids == current_row_ids(store, cid) for ids in derived)


def referrers(store):
    with request_scope(owner_principal()):
        found = route.run(store, SKILLS, {"version": 1, "related": [INVERSE],
                                          "where": {"field": "slug", "op": "eq", "value": "welding"}},
                          facade_profile="records")
    return found["rows"][0]["related"][f"inverse:{DIARY}:skills.skill_id"]["count"]


def test_a_typed_collection_follows_a_retraction_and_a_re_admission(diary):
    """Typed rows, the filter index and the reference index each keep a row the others dropped, unless all three move."""
    (key,) = diary.connection.execute("SELECT item_key FROM items WHERE collection_id=? ORDER BY row_id LIMIT 1",
                                      (DIARY,)).fetchone()
    filtered = {"where": {"field": "hours", "op": "eq", "value": 3}}
    assert diary_titles(diary, **filtered) == ["day one"] and referrers(diary) == 2

    retract(diary, key, cid=DIARY)
    assert diary_titles(diary) == ["day two"] and diary_titles(diary, **filtered) == []
    assert referrers(diary) == 1
    assert sorted(values["title"] for _, values in typed_storage.collection_values(diary.connection, DIARY)) == ["day two"]
    assert _indexed(diary) == _implied(diary) and len(_indexed(diary)) == 1
    assert holds_exactly_the_current_rows(diary, DIARY) and len(current_row_ids(diary, DIARY)) == 1

    diary.append_record(DIARY, item={"title": "day one", "hours": 3, "skills": [{"skill_id": "welding"}]},
                        why="entered again")
    assert diary_titles(diary, **filtered) == ["day one"] and diary_titles(diary) == ["day one", "day two"]
    assert referrers(diary) == 2
    assert _indexed(diary) == _implied(diary) and len(_indexed(diary)) == 2
    assert holds_exactly_the_current_rows(diary, DIARY) and len(current_row_ids(diary, DIARY)) == 2


@pytest.mark.parametrize("encoding", ["json-v1", "typed-v1", "typed-v2"])
def test_every_row_encoding_follows_a_retraction_and_a_re_admission(store, monkeypatch, encoding):
    """Each encoding keeps current rows in its own relation; a retraction must leave none of them holding the row."""
    migrate = typed_storage.migrate_batch

    def installed_encoding(conn, collection_id, fields, **kwargs):
        # An installed summary may still hold JSON rows until its forward migration; the others migrate at create.
        return "ready" if encoding == typed_storage.JSON_V1 else migrate(conn, collection_id, fields, **{**kwargs, "encoding": encoding})

    with monkeypatch.context() as installation:
        installation.setattr(typed_storage, "migrate_batch", installed_encoding)
        store.create_collection(manifest_path(), summary_text(indexed_text()), why="create", scaffold=False)
    wrong = append(store, WRONG)
    append(store, KEPT, 2)
    assert holds_exactly_the_current_rows(store, CID)
    retract(store, wrong)
    assert titles(store) == [KEPT] and holds_exactly_the_current_rows(store, CID)
    assert len(current_row_ids(store, CID)) == 1
    append(store, WRONG)
    assert titles(store) == sorted([WRONG, KEPT]) and holds_exactly_the_current_rows(store, CID)
    assert len(current_row_ids(store, CID)) == 2


ROLLUP_FIELDS = {"id": "{type: string, required: true}", "start": "{type: datetime, offset: utc_offset}",
                 "utc_offset": "{type: string}", "calories": "{type: integer}"}
DAILY = {"bucket": "day", "timestamp": "start", "values": {"calories": ["count", "sum"]}}


def test_a_rollup_follows_a_retraction_and_a_re_admission(store):
    """A rollup that is only added to counts a retracted row's calories for good."""
    store.create_collection(manifest_path(), rollup_manifest(rollups={"daily": DAILY}, fields=ROLLUP_FIELDS),
                            why="create", scaffold=False)
    _evidence(store.root)
    items = [{"id": "a", "start": "2026-03-01T10:00:00+00:00", "calories": 5, "utc_offset": "+00:00"},
             {"id": "b", "start": "2026-03-01T11:00:00+00:00", "calories": 7, "utc_offset": "+00:00"}]
    first = bulk(store, items)
    day = {"2026-03-01": {"count": 2, "sum": 12}}
    assert by_bucket(reduce(store, request(ops=("count", "sum")))) == day

    retract(store, first["rows"][0]["item_key"])
    assert by_bucket(reduce(store, request(ops=("count", "sum")))) == {"2026-03-01": {"count": 1, "sum": 7}}

    assert bulk(store, items[:1])["counts"]["inserted"] == 1
    assert by_bucket(reduce(store, request(ops=("count", "sum")))) == day


def test_import_row_counts_exclude_a_retracted_row(store):
    """The next member's logged row count extends the last one, so a retraction between two members must reduce it."""
    fields = "  natural_key: [exercise_id]\n  fields:\n    exercise_id: {type: string, required: true}\n"
    setup(store, b"", text=summarized(importer_manifest_text(fields=fields)))
    source, _ = preserve_export(store, "two", {
        "rows/a.json": {"rows": [{"exercise_id": f"r{n}"} for n in range(3)]},
        "rows/b.json": {"rows": [{"exercise_id": f"n{n}"} for n in range(2)]}})
    job = agent(store, mode="start", source_ref=source, format="json-document", members="rows/*",
                mapping={"rows": "rows[]", "fields": {"exercise_id": "exercise_id"}, "on_invalid": "skip"})
    run(store, max_batches=1)
    assert [entry[7] for entry in logged(store.connection)] == [3]
    (key,) = store.connection.execute("SELECT item_key FROM items WHERE collection_id=? ORDER BY row_id LIMIT 1",
                                      (WORKOUTS,)).fetchone()
    retract(store, key, cid=WORKOUTS)
    assert finish(store, job)["state"] == "complete"
    assert [entry[7] for entry in logged(store.connection)] == [3, 4]
    assert store.connection.execute("SELECT COUNT(*) FROM items WHERE collection_id=?", (WORKOUTS,)).fetchone() == (4,)


# --- T6 the upgrade of a store that holds history ----------------------------------------------

HISTORY_TABLES = ("txns", "audit_effects", "item_versions", "item_sources", "version_identity",
                  "version_source_bindings", "held_candidates")


def tables_of(raw, names):
    return {name: raw.execute(f"SELECT * FROM {name} ORDER BY 1, 2").fetchall() for name in names}  # noqa: S608


@pytest.fixture
def schema_18_store(tmp_path, monkeypatch):
    """A store from before schema 19, written by the maintained writer.

    It holds a Records collection with an updated row that has a Source binding and a held candidate, and
    summary collections with typed rows, a reference index and a ready projection.
    """
    root, path = tmp_path / "vault", tmp_path / "collections.sqlite"
    (root / "Knowledge Base/Sources").mkdir(parents=True)
    owner_id = memory_refs.new_id()
    (root / "Knowledge Base/Sources/observation.md").write_text(
        f"---\ntype: source\nexomem_id: {owner_id}\n---\n\nObservation.\n")
    with monkeypatch.context() as earlier:
        earlier.setattr(schema, "SCHEMA_VERSION", 18)
        before_retractable_rows(earlier)
        with connection.open_writer(path, lease_check=lambda: True) as handle, library_scope():
            writer = CollectionWriter(root, handle)
            writer.create_collection(manifest_path(), manifest_text(), why="declare", scaffold=False)
            first = writer.append_record(CID, item={"title": "Observed", "count": 1}, item_key=KEY,
                                         sources=["Knowledge Base/Sources/observation.md"], why="observe")
            writer.update_record(CID, item_key=KEY, changes={"count": 2}, expected_container_hash=first["after_container_hash"],
                                 expected_item_version=first["after_item_hash"], why="correct")
            append(writer, KEPT, 2)
            with pytest.raises(collections.CollectionError):
                writer.append_record(CID, item={"title": "Refused", "count": "many"}, why="observe")
            create_collection(writer, SITES, sites_text(), "Sites")
            create_collection(writer, SKILLS, skills_text(), "Skills")
            create_collection(writer, DIARY, diary_text(DIARY_FIELDS), "Diary")
            row(writer, SKILLS, SLUG, {"slug": "welding"})
            for title in ("day one", "day two"):
                writer.append_record(DIARY, item={"title": title, "skills": [{"skill_id": "welding"}]}, why="record")
            ready(writer)
    return root, path


def test_a_store_with_history_upgrades_without_losing_a_row_or_a_proof(schema_18_store):
    """A migration that rebuilds a table drops a foreign key, a trigger or a row; history is the proof that none did."""
    root, path = schema_18_store
    with closing(sqlite3.connect(path)) as raw:
        assert schema.schema_version(raw) == 18
        assert raw.execute("SELECT type FROM sqlite_master WHERE name='items'").fetchone() == ("table",)
        before_items = raw.execute("SELECT * FROM items ORDER BY row_id").fetchall()
        before_counts = raw.execute("SELECT collection_id, COUNT(*) FROM items GROUP BY 1 ORDER BY 1").fetchall()
        before = tables_of(raw, HISTORY_TABLES)
    assert len(before_counts) == 3 and before["version_source_bindings"] and before["held_candidates"]

    with connection.open_writer(path, lease_check=lambda: True) as handle, library_scope():
        conn = handle.connection
        assert schema.schema_version(conn) == 19
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert tables_of(conn, HISTORY_TABLES) == before
        assert [row[:-1] for row in conn.execute("SELECT * FROM item_rows ORDER BY row_id")] == before_items
        assert conn.execute("SELECT COUNT(*) FROM item_rows WHERE retracted_txn IS NOT NULL").fetchone() == (0,)
        assert conn.execute("SELECT * FROM items ORDER BY row_id").fetchall() == before_items
        assert conn.execute("SELECT collection_id, COUNT(*) FROM items GROUP BY 1 ORDER BY 1").fetchall() == before_counts
        for collection_id, _ in before_counts:
            history.verify(conn, collection_id)
        # The rebuilt history table still refuses a rewrite and still answers to the Source bindings.
        with pytest.raises(sqlite3.IntegrityError, match="append-only"), handle.transaction() as tx:
            tx.execute("UPDATE audit_effects SET effect_label='x'")
        assert ("audit_effects",) in conn.execute(
            "SELECT \"table\" FROM pragma_foreign_key_list('version_source_bindings')").fetchall()

        # A row that predates the upgrade retracts and comes back like any other.
        writer = CollectionWriter(root, handle)
        (key,) = conn.execute("SELECT item_key FROM items WHERE collection_id=? ORDER BY row_id LIMIT 1", (DIARY,)).fetchone()
        assert diary_titles(writer) == ["day one", "day two"]
        retract(writer, key, cid=DIARY)
        assert len(diary_titles(writer)) == 1
        for title in ("day one", "day two"):
            writer.append_record(DIARY, item={"title": title, "skills": [{"skill_id": "welding"}]}, why="record")
        assert diary_titles(writer) == ["day one", "day two"]
        assert conn.execute("SELECT COUNT(*) FROM item_rows WHERE collection_id=?", (DIARY,)).fetchone() == (2,)
        history.verify(conn, DIARY)
