"""The agent route for typed collection queries and their discovery chapter.

OpenSpec add-collection-query-engine S1.3/S1.4, 5.3 and 7.6a (design §8, §11,
§12). Store-routed cases call the dispatcher that MCP, REST and the CLI share,
with the store preview bound as the store tests do, and read refusals as the
error object those surfaces send. The preview cannot cross the MCP tool layer:
its writer belongs to the opening thread and FastMCP runs a sync tool on a
worker thread. Cases that need no store therefore run through an in-process MCP
server. Answers are compared with the independent ``s1_export_fixture``
reference. The installed service's own store session answers the route in
test_collection_store_service.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import initialize_vault_state_offline
from s1_export_fixture import daily_summaries, expected_daily, iter_exercises
from test_collection_rollups import DAILY, FIELDS, SUMMARY_FIELDS, exercise, load, manifest, request
from test_collection_store_importer import (
    CID,
    MAPPING,
    SOURCE,
    TWO_NUMERIC,
    WORKOUT_FIELDS,
    ndjson,
    run,
    setup,
    small,
    summarized,
    valid,
    write_source,
)
from test_collection_store_importer import manifest_text as workout_manifest
from test_collection_store_summary import COPY_CID
from test_collection_store_writer import CID as SUMMARY_CID
from test_collection_store_writer import KEY, OTHER, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope

import exomem
from exomem import commands, records, server, vault
from exomem.cli_ops import OpError
from exomem.collection_store import authority
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import runtime
from exomem.writer_lease import invoke_command

OWNER = owner_principal(surface="mcp")
ERROR_SHAPE = ("code", "at", "expected", "allowed", "repair", "retryable")
DAILY_CALORIES = {"version": 1, "group_by": [{"field": "local_date", "bucket": "day"}],
                  "aggregates": {"count": {"op": "count", "field": "calories"},
                                 "sum": {"op": "sum", "field": "calories"},
                                 "avg": {"op": "avg", "field": "calories"}}}
STEPS = {"version": 1, "group_by": [{"field": "date", "bucket": "day"}],
         "aggregates": {"sum": {"op": "sum", "field": "steps"}}}
ABSENT = "00000000-0000-4000-8000-000000000000"


@pytest.fixture(autouse=True)
def owner():
    """Direct writer calls that set up a collection act as the owner, as in the rollup tests."""
    with request_scope(OWNER):
        yield


def tool(store, tool_name, /, who=OWNER, **arguments):
    """One call through the dispatcher every surface shares, with the store preview bound."""
    command = next(command for command in commands.COMMANDS if command.name == tool_name)
    with request_scope(who), preview_store(store.root, store.handle):
        return invoke_command(command, store.root, **arguments)


def refusal(store, tool_name, /, who=OWNER, **arguments):
    """The error object MCP and REST send for a refused dispatcher call."""
    with pytest.raises(OpError) as error:
        tool(store, tool_name, who, **arguments)
    return error.value.as_public_dict()


def query(store, raw, collection=CID):
    return tool(store, "record_memory", action="query", collection=collection, query=raw)


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    """Call one tool through an in-process MCP server on a file-mode vault.

    Returns the structured result, or the ``error`` object of a refusal envelope.
    """
    vault = tmp_path / "served"
    shutil.copytree(Path(exomem.__file__).parent / "_scaffold" / "_Schema", vault / ".exomem" / "schema")
    (vault / "Knowledge Base").mkdir()
    initialize_vault_state_offline(vault, source="query route MCP fixture")
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    for feature in ("EMBEDDINGS", "RELEVANCE_CHECK", "MEDIA_EXTRACTION", "CLIP", "FILE_WATCHER"):
        monkeypatch.setenv(f"EXOMEM_DISABLE_{feature}", "1")
    app = server.build_server(require_auth=False)

    def call(tool_name, /, **arguments):
        content = asyncio.run(app.call_tool(tool_name, arguments, run_middleware=False)).structured_content
        content = content.get("result", content) if set(content) == {"result"} else content
        return content["error"] if content.get("success") is False else content

    call.vault = vault
    return call


def test_agent_imports_and_reads_source_local_daily_totals_in_every_mode(store, monkeypatch):
    """The route an agent uses end to end: describe names the import and query routes, an import
    previews, runs, reports and cancels, and every query mode answers without inventing values; the
    daily count, sum and mean equal the independent reference across cursor pages, and a typed row
    page comes back in declared order."""
    small(monkeypatch)
    setup(store, fields=WORKOUT_FIELDS.replace("calories: {type: integer}", "calories: {type: integer, sortable: true}"))
    records = list(iter_exercises())
    described = tool(store, "record_memory", action="describe")
    assert described["query"]["grammar"]["args"] == {"subject": "query-engine", "operation": "inspect",
                                                     "name": "collections"}
    assert set(described["import"]["import_request"]) >= {"mode", "source_ref", "format", "mapping"}

    def imported(**request):
        return tool(store, "record_memory", action="import", collection=CID, import_request=request)

    preview = imported(mode="preview", source_ref=SOURCE, format="ndjson", mapping=MAPPING)
    assert preview["mapping"]["findings"] == []
    job = imported(mode="start", source_ref=SOURCE, format="ndjson", mapping=MAPPING)
    run(store)
    done = imported(mode="status", continuation=job["continuation"])
    assert (done["state"], done["rows"]["imported"]) == ("complete", len(valid(records)))
    later = "Knowledge Base/Evidence/wearable/export/later.ndjson"
    write_source(store.root, ndjson(records[:10]), later)
    again = imported(mode="start", source_ref=later, format="ndjson", mapping=MAPPING)
    cancelled = imported(mode="cancel", continuation=again["continuation"])
    assert (cancelled["state"], cancelled["reason"]) == ("partial", "cancelled")

    composed = query(store, {**DAILY_CALORIES, "mode": "compose"})
    assert composed["kind"] == "groups" and len(composed["fingerprint"]) == 64 and "groups" not in composed
    explained = query(store, {**DAILY_CALORIES, "mode": "explain"})
    assert explained["plan"]["strategy"] == "base" and "admitted_rows" not in explained
    previewed = query(store, {**DAILY_CALORIES, "mode": "preview"})
    assert previewed["admitted_rows"] == len(valid(records)) and "groups" not in previewed
    assert query(store, {**DAILY_CALORIES, "mode": "dry_run"})["admitted"] is True

    pages = [query(store, {**DAILY_CALORIES, "page": {"limit": 10}})]
    while pages[-1]["has_more"]:
        pages.append(query(store, {**DAILY_CALORIES, "page": {"limit": 10, "after": pages[-1]["next_cursor"]}}))
    daily, flagged = expected_daily(records, "metrics.calories")
    assert flagged and len(pages) > 1
    answer = {group.pop("local_date"): group for page in pages for group in page["groups"]}
    assert answer == daily
    first = pages[0]
    assert (first["source"]["ref"], first["query_version"], first["execution_profile"]) == (CID, 1, "interactive")
    assert first["truncation_reason"] == "limit" and first["as_of"] == pages[-1]["as_of"]

    rows = query(store, {"version": 1, "select": ["exercise_id", "calories"],
                         "order_by": [{"field": "calories", "direction": "desc"}], "page": {"limit": 5}})
    assert rows["returned"] == 5 and rows["has_more"] is True and rows["next_cursor"]
    assert [row["calories"] for row in rows["rows"]] == sorted(
        (record["metrics"]["calories"] for record in valid(records)), reverse=True)[:5]


def test_applying_the_previewed_declarations_lets_imported_rows_answer_from_a_rollup(store, monkeypatch):
    """An import that works but leaves the daily totals on the base scan, so the agent still has to
    choose declarations by hand; or a recommendation that its own revise refuses. The rollup's
    daily count, sum and mean equal the base path's and the independent reference."""
    small(monkeypatch)
    records = list(iter_exercises())
    text = summarized(workout_manifest())
    setup(store, text=text)
    # The same collection without the recommendation is the base path.
    setup(store, cid=COPY_CID, title="Workouts base", text=summarized(workout_manifest(COPY_CID, "Workouts base")))

    def imported(collection, **request):
        return tool(store, "record_memory", action="import", collection=collection, import_request=request)

    source = {"source_ref": SOURCE, "format": "ndjson", "mapping": TWO_NUMERIC}
    recommended = imported(CID, mode="preview", **source)["recommended_declarations"]
    assert recommended["fields"] and recommended["rollups"] and recommended["omitted"] == []
    data, _, _ = vault.parse_frontmatter(text, strict=True)
    for name, flags in recommended["fields"].items():
        data["item_schema"]["fields"][name].update(flags)
    data["rollups"] = recommended["rollups"]
    guards = tool(store, "record_memory", action="inspect", collection=CID)["lifecycle_guards"]
    tool(store, "record_memory", action="revise", collection=CID, why="declare the recommended index and rollups",
         manifest_text="---\n" + yaml.safe_dump(data, sort_keys=False) + "---\n", **guards)

    for collection in (CID, COPY_CID):
        job = imported(collection, mode="start", **source)
        run(store)
        assert imported(collection, mode="status", continuation=job["continuation"])["state"] == "complete"
    window = {"version": 1, "select": ["exercise_id"], "order_by": [{"field": "local_date"}],
              "where": {"field": "local_date", "op": "gte", "value": "2026-03-01"}, "mode": "explain"}
    assert query(store, window)["plan"]["index"] == "field:local_date"
    answers = {}
    for collection in (CID, COPY_CID):
        explained = query(store, {**DAILY_CALORIES, "mode": "explain"}, collection=collection)
        pages = [query(store, {**DAILY_CALORIES, "page": {"limit": 10}}, collection=collection)]
        while pages[-1]["has_more"]:
            pages.append(query(store, {**DAILY_CALORIES, "page": {"limit": 10, "after": pages[-1]["next_cursor"]}},
                               collection=collection))
        answers[explained["plan"]["strategy"]] = {group.pop("local_date"): group for page in pages
                                                  for group in page["groups"]}
    daily, flagged = expected_daily(records, "metrics.calories")
    assert flagged and set(answers) == {"rollup", "base"}
    assert answers["rollup"] == answers["base"] == daily


def test_execution_profile_defaults_to_interactive_and_refuses_an_unknown_profile(store):
    """A query without a profile run under analytics bounds, or an unknown profile silently run
    as interactive instead of a located refusal."""
    store.create_collection(manifest_path(), manifest(fields=SUMMARY_FIELDS, natural_key="date"),
                            why="create", scaffold=False)
    load(store, daily_summaries(10))
    default = query(store, {**STEPS, "mode": "preview"}, collection=SUMMARY_CID)
    assert default["execution_profile"] == "interactive" and default["bounds"] == runtime.PROFILES["interactive"]
    wide = query(store, {**STEPS, "mode": "preview", "execution_profile": "analytics"}, collection=SUMMARY_CID)
    assert wide["bounds"] == runtime.PROFILES["analytics"]
    error = refusal(store, "record_memory", action="query", collection=SUMMARY_CID,
                    query={**STEPS, "execution_profile": "batch"})
    assert {name: error[name] for name in ERROR_SHAPE} == {
        "code": "QUERY_VALUE_INVALID", "at": "execution_profile", "expected": "closed enumerated value",
        "allowed": ["analytics", "interactive"], "repair": "Revise the addressed query field.", "retryable": False}


def test_one_analytics_session_per_store_leaves_an_interactive_reader(store):
    """Two analytics calls holding both of the store's readers for seconds, so an interactive
    query waits behind them, or a second analytics call that is queued instead of told to retry."""
    store.create_collection(manifest_path(), manifest(fields=SUMMARY_FIELDS, natural_key="date"),
                            why="create", scaffold=False)
    load(store, daily_summaries(10))
    held = runtime.QueryLimits(profile="analytics")
    with runtime.read_session(store.root, store.handle.path, limits=held):
        error = refusal(store, "record_memory", action="query", collection=SUMMARY_CID,
                        query={**STEPS, "execution_profile": "analytics"})
        assert (error["code"], error["retryable"]) == ("QUERY_BUSY", True)
        assert query(store, STEPS, collection=SUMMARY_CID)["returned"] == 10
    write_scope(store.root, paths="Records/**")
    write_rule(store.root, ceiling=0)


def test_continuation_without_freshness_coverage_refuses_rows_and_groups_with_one_code(store):
    """The same missing freshness coverage refused under different codes on the row and group paths,
    so an agent's repair depends on which path its query took."""
    fields = {**SUMMARY_FIELDS, "steps": "{type: integer, sortable: true}"}
    store.create_collection(manifest_path(), manifest(fields=fields, natural_key="date"), why="create",
                            scaffold=False)
    load(store, daily_summaries(10))
    store.connection.execute("DELETE FROM query_cursor_state")
    rows = refusal(store, "record_memory", action="query", collection=SUMMARY_CID,
                   query={"version": 1, "select": ["date", "steps"], "order_by": [{"field": "steps"}],
                          "page": {"limit": 5}})
    groups = refusal(store, "record_memory", action="query", collection=SUMMARY_CID,
                     query={**STEPS, "page": {"limit": 5}})
    assert (rows["code"], groups["code"]) == ("QUERY_UNAVAILABLE", "QUERY_UNAVAILABLE")


def test_sealed_summary_collection_is_absent_to_another_audience_in_every_mode(store):
    """A row-withheld summary collection whose name, field names or row count reach another audience
    through a query refusal, a composed declaration or an estimate."""
    store.create_collection(manifest_path(), manifest(fields=SUMMARY_FIELDS, natural_key="date"),
                            why="create", scaffold=False)
    load(store, daily_summaries(10))
    assert query(store, STEPS, collection=SUMMARY_CID)["returned"] == 10
    write_scope(store.root, paths="Records/**")
    write_rule(store.root, ceiling=0)
    for mode in ("compose", "preview", "execute"):
        sealed = json.dumps(refusal(store, "record_memory", _external(), action="query", collection=SUMMARY_CID,
                                    query={**STEPS, "mode": mode}))
        missing = json.dumps(refusal(store, "record_memory", _external(), action="query", collection=ABSENT,
                                     query={**STEPS, "mode": mode}))
        assert sealed.replace(SUMMARY_CID, ABSENT) == missing
        assert "steps" not in sealed and "10" not in sealed


def test_sealed_collection_under_a_routing_marker_refuses_like_an_absent_one(store):
    """Under a routing marker an unlisted selector falls to file mode, so a file-mode query or import
    refusal that skips resolution tells another audience which selectors name sealed store collections."""
    store.create_collection(manifest_path(), manifest(fields=SUMMARY_FIELDS, natural_key="date"),
                            why="create", scaffold=False)
    load(store, daily_summaries(10))
    write_scope(store.root, paths="Records/**")
    write_rule(store.root, ceiling=0)
    sid = store.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    marker = authority.marker_path(store.root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "version": 1, "mode": "store", "default_authority": "file", "store_id": sid, "authority_epoch": 1,
        "collections": [{"collection_id": SUMMARY_CID, "manifest_path": manifest_path(), "authority": "store",
                         "store_id": sid}],
        "collection_store_fence": {"capability": "collections-store-v1", "generation": 1}}))
    selectors = (SUMMARY_CID, manifest_path(), ABSENT, manifest_path().replace("/Work/", "/Absent/"))
    for call in ({"action": "query", "query": STEPS},
                 {"action": "import", "import_request": {"mode": "preview", "source_ref": SOURCE, "format": "ndjson"}}):
        answers = {json.dumps(refusal(store, "record_memory", _external(), collection=selector, **call))
                   for selector in selectors}
        assert len(answers) == 1 and "COLLECTION_NOT_FOUND" in answers.pop(), call["action"]


def test_a_link_to_a_page_the_caller_cannot_read_never_shows_and_cannot_filter_or_sort(store):
    """A typed row or group label naming a page the caller cannot read, for another audience or for the
    owner under an access exclusion; a visible bare-title link dropped where the legacy query shows it;
    or a filter or sort on stored link values that recovers what a row omits."""
    link, bare = "[[Private/Secret Plan]]", "[[Open Page]]"
    fields = {"id": "{type: string, required: true}", "count": "{type: integer, sortable: true}",
              "related": "{type: link, sortable: true}"}
    store.create_collection(manifest_path(), manifest(fields=fields, summary=False), why="create", scaffold=False)
    for page in ("Private/Secret Plan", "Notes/Open Page"):
        target = store.root / f"Knowledge Base/{page}.md"
        target.parent.mkdir(parents=True)
        target.write_text(f"# {page.rsplit('/', 1)[-1]}\n")
    for key, item in ((KEY, {"id": "linked", "count": 1, "related": link}), (OTHER, {"id": "unlinked", "count": 2}),
                      ("33333333-3333-4333-8333-333333333333", {"id": "bare", "count": 3, "related": bare})):
        store.append_record(SUMMARY_CID, item=item, item_key=key, why="observe")
    rows = {"version": 1, "select": ["id", "related"], "order_by": [{"field": "count"}]}
    by_link = {"version": 1, "group_by": [{"field": "related"}], "aggregates": {"rows": {"op": "count"}}}

    def answers(who):
        def read(**arguments):
            return tool(store, "record_memory", who, action="query", collection=SUMMARY_CID, **arguments)
        typed, legacy = read(query=rows)["rows"], read()["rows"]
        assert {row["id"]: row.get("related") for row in typed} == {row["id"]: row.get("related") for row in legacy}
        return typed, read(query=by_link)["groups"]

    released_rows, released_groups = answers(OWNER)
    assert released_rows == [{"id": "linked", "related": link}, {"id": "unlinked"}, {"id": "bare", "related": bare}]
    assert released_groups == [{"rows": 1}, {"related": bare, "rows": 1}, {"related": link, "rows": 1}]
    # Withheld, the linked row reads exactly like its unlinked twin and joins the absent-link group.
    withheld = ([{"id": "linked"}, {"id": "unlinked"}, {"id": "bare", "related": bare}],
                [{"rows": 2}, {"related": bare, "rows": 1}])
    write_scope(store.root, paths="Private/**")
    write_rule(store.root, ceiling=0)
    assert answers(_external()) == withheld
    (store.root / "Knowledge Base/_access.yaml").write_text('excluded: ["Private/Secret Plan.md"]\n')
    assert answers(OWNER) == withheld
    for who in (OWNER, _external()):
        for raw, at in (({**rows, "where": {"field": "related", "op": "eq", "value": link}}, "where.field"),
                        ({**rows, "order_by": [{"field": "related"}]}, "order_by[0].field")):
            error = refusal(store, "record_memory", who, action="query", collection=SUMMARY_CID, query=raw)
            assert (error["code"], error["at"]) == ("QUERY_FIELD_UNAVAILABLE", at)


def test_only_a_reduction_that_reads_a_link_leaves_its_ready_rollup(store):
    """A link-bearing collection whose every reduction loses its ready rollup, or a reduction grouped on
    the link answered from buckets that never saw the caller's link projection."""
    rollups = {"daily": DAILY, "by_link": {**DAILY, "group_by": ["related"]}}
    store.create_collection(manifest_path(), manifest(fields={**FIELDS, "related": "{type: link}"},
                                                      rollups=rollups, summary=False), why="create", scaffold=False)
    load(store, [exercise(record) for record in iter_exercises(20)])
    daily = query(store, {**request(), "mode": "explain"}, collection=SUMMARY_CID)
    by_link = query(store, {**request(groups=("related",)), "mode": "explain"}, collection=SUMMARY_CID)
    assert daily["plan"]["strategy"] == "rollup"
    assert (by_link["plan"]["strategy"], by_link["plan"]["reason"]) == ("base", "fields_projected")


def test_link_projection_leaves_every_other_value_as_stored(store):
    """A link-bearing collection whose base scan or rows rewrite a null or empty non-link value, so its
    groups differ from the same query answered from a ready rollup, and its rows from a link-free twin."""
    fields = {**FIELDS, "calories": "{type: integer, sortable: true}", "tags": "{type: array, items: {type: string}}"}
    by_kind = {"by_kind": {**DAILY, "group_by": ["kind"], "values": {"calories": ["count", "sum"]}}}
    store.create_collection(manifest_path(), manifest(fields=fields, rollups=by_kind, summary=False),
                            why="create", scaffold=False)
    store.create_collection(manifest_path().replace("/Work/", "/Twin/"),
                            manifest(fields={**fields, "related": "{type: link}"}, summary=False, cid=COPY_CID,
                                     title="Twin"), why="create", scaffold=False)
    items = [{"id": "null-kind", "kind": None, "tags": [], "start": "2026-03-01T08:00:00+00:00", "calories": 100},
             {"id": "no-kind", "start": "2026-03-01T09:00:00+00:00", "calories": 200},
             {"id": "run", "kind": "run", "tags": ["x"], "start": "2026-03-01T10:00:00+00:00", "calories": 300}]
    for key, item in zip((KEY, OTHER, "33333333-3333-4333-8333-333333333333"), items, strict=True):
        store.append_record(SUMMARY_CID, item=item, item_key=key, why="observe")
        store.append_record(COPY_CID, item=item, item_key=key, why="observe")
    groups = {**request(ops=("count", "sum"), groups=("kind",))}
    rows = {"version": 1, "select": ["id", "kind", "tags"], "order_by": [{"field": "calories"}]}
    rollup, linked = (query(store, groups, collection=cid) for cid in (SUMMARY_CID, COPY_CID))
    assert (rollup["plan"]["strategy"], linked["plan"]["strategy"]) == ("rollup", "base")
    assert linked["groups"] == rollup["groups"]
    assert query(store, rows, collection=COPY_CID)["rows"] == query(store, rows, collection=SUMMARY_CID)["rows"]


def test_compose_refuses_every_shape_and_profile_that_execute_refuses(store):
    """A request that composes cleanly and then fails in every other mode, so an agent's refinement
    loop accepts a query it can never run."""
    store.create_collection(manifest_path(), manifest(fields=SUMMARY_FIELDS, natural_key="date"),
                            why="create", scaffold=False)
    for raw in ({"version": 1, "select": ["date"], "execution_profile": "analytics"},
                {**STEPS, "where": {"field": "steps", "op": "gt", "value": 1}}):
        composed = refusal(store, "record_memory", action="query", collection=SUMMARY_CID,
                           query={**raw, "mode": "compose"})
        assert composed["code"] == "QUERY_UNSUPPORTED"
        assert composed == refusal(store, "record_memory", action="query", collection=SUMMARY_CID, query=raw)


def test_query_object_refuses_legacy_shaping_arguments(mcp):
    """Two grammars merged in one call, so a legacy filter silently narrows or is ignored."""
    error = mcp("record_memory", action="query", collection=SUMMARY_CID, query=STEPS,
                filters=[{"column": "steps", "op": "gt", "value": 1}])
    assert {name: error[name] for name in ERROR_SHAPE} == {
        "code": "QUERY_ARGUMENT_CONFLICT", "at": "query", "expected": "one query grammar", "allowed": ["filters"],
        "repair": "Remove filters, or omit query.", "retryable": False}


def test_a_visible_file_mode_collection_refuses_the_query_object(mcp):
    """A v1 query on a file collection answered by the legacy reader under a v1-looking envelope."""
    (mcp.vault / "Knowledge Base/log.md").write_text("# Log\n")
    records.create_collection(mcp.vault, manifest_path(), manifest_text(), why="file collection", scaffold=True)
    assert mcp("record_memory", action="query", collection=manifest_path(), query=STEPS)["code"] == "QUERY_UNAVAILABLE"


def test_query_engine_chapters_are_bounded_read_only_and_name_what_is_unavailable(mcp):
    """A discovery chapter past its byte budget, without a declared capability and version, silent
    about unavailable operations, or one that writes or audits."""
    def vault_files():
        roots = (mcp.vault, Path(os.environ["EXOMEM_STATE_ROOT"]))  # audit records live in the state root
        return {path: path.read_bytes() for root in roots for path in sorted(root.rglob("*")) if path.is_file()}

    before = vault_files()
    for name in ("collections", "import"):
        chapter = mcp("schema_memory", operation="inspect", subject="query-engine", name=name)
        assert (chapter["subject"], chapter["chapter"], chapter["version"]) == ("query-engine", name, 1)
        assert chapter["capability"] and chapter["unavailable"]
        assert runtime.wire_bytes(chapter) <= runtime.MAX_RESULT_BYTES
    assert vault_files() == before
    graph = mcp("schema_memory", operation="inspect", subject="query-engine", name="graph")
    assert (graph["code"], graph["at"], graph["retryable"]) == ("QUERY_CAPABILITY_UNAVAILABLE", "name", False)
