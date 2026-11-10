"""Row queries that name declared references: related expansion, dotted paths, filtered reductions.

The fixtures are the declaration tests': sites, skills and a diary whose fields declare references,
plus hours and leave fields for filtering and aggregates.
"""

import pytest
from test_collection_row_references import (
    DIARY,
    NORTH,
    SITE_FIELD,
    SITES,
    SITES_SEEN_FIELD,
    SKILL_FIELD,
    SKILLS,
    SLUG,
    YARD,
    collection_text,
    create,
    diary_text,
    revise,
    row,
    sites_text,
    skills_text,
)
from test_collection_store_writer import store as store  # noqa: F401
from test_governance_egress import _external, write_rule, write_scope

from exomem import structured_collections as collections
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import reference_rows, route, runtime

K1 = "00000000-0000-4000-8000-0000000000c1"
K2 = "00000000-0000-4000-8000-0000000000c2"
K3 = "00000000-0000-4000-8000-0000000000c3"
K4 = "00000000-0000-4000-8000-0000000000c4"

FIELDS = (SITE_FIELD.replace("type: string,", "type: string, filterable: true,") + SKILL_FIELD
          + "    hours: {type: integer, filterable: true, sortable: true}\n"
          + "    leave: {type: boolean, filterable: true}\n    day: {type: date}\n")


@pytest.fixture
def world(store):
    """Sites North-Field and south-yard, one skill, and a diary whose fields declare references."""
    create(store, SITES, sites_text(), "Sites")
    create(store, SKILLS, skills_text(), "Skills")
    text = diary_text(FIELDS).replace(
        "title: {type: string, required: true}",
        "title: {type: string, required: true, filterable: true, sortable: true}")
    create(store, DIARY, text, "Diary")
    store._site_added = row(store, SITES, NORTH, {"code": "North-Field", "name": "North field"})
    row(store, SITES, YARD, {"code": "south-yard", "name": "South yard"})
    row(store, SKILLS, SLUG, {"slug": "welding"})
    row(store, DIARY, K1, {"title": "day one", "site": "North-Field", "hours": 3, "leave": True,
                           "skills": [{"skill_id": "welding"}]})
    row(store, DIARY, K2, {"title": "day two", "site": "East-Gate", "hours": 6, "leave": False})
    row(store, DIARY, K3, {"title": "day three", "hours": 1, "leave": True})
    return store


def run(store, query, who=None):
    with request_scope(who or owner_principal()):
        return route.run(store, DIARY, {"version": 1, **query}, facade_profile="records")


def refused(store, query, who=None):
    with pytest.raises(runtime.QueryError) as error:
        run(store, query, who)
    return error.value


def test_related_and_path_fields_report_each_reference_state(world):
    """A missing target, a missing value and a resolved one must each read their own state."""
    result = run(world, {"select": ["title", "site.name"], "related": ["site"],
                         "order_by": [{"field": "title"}]})
    rows = {item["title"]: item for item in result["rows"]}
    one, two, three = rows["day one"], rows["day two"], rows["day three"]
    assert one["references"] == {"site": "resolved"}
    assert one["site.name"] == "North field"
    assert one["related"]["site"]["row"]["name"] == "North field"
    assert two["references"] == {"site": "unresolved"}
    assert "site.name" not in two and "row" not in two["related"]["site"]
    assert three["references"] == {"site": "none"}
    assert "site.name" not in three and "row" not in three["related"]["site"]
    assert result["combined"]["unresolved_rows"] == 1


def test_a_many_related_expands_bounded_without_multiplying_rows(world):
    """A repeated, a present and a missing skill must count once each and honour the limit."""
    row(world, SKILLS, "00000000-0000-4000-8000-0000000000b2", {"slug": "rigging"})
    row(world, DIARY, K4, {"title": "day four",
                           "skills": [{"skill_id": "welding"}, {"skill_id": "welding"},
                                      {"skill_id": "rigging"}, {"skill_id": "missing"}]})
    result = run(world, {"select": ["title"], "related": [{"field": "skills.skill_id", "limit": 1}],
                         "where": {"field": "title", "op": "eq", "value": "day four"}})
    assert result["returned"] == 1
    expansion = result["rows"][0]["related"]["skills.skill_id"]
    assert expansion["state"] == "unresolved" and expansion["count"] == 3
    assert len(expansion["items"]) == 1 and expansion["truncated"]
    assert expansion["items"][0]["value"] == "welding" and "row" in expansion["items"][0]


def test_a_path_through_a_many_valued_reference_is_refused(store):
    """Path expansion would multiply rows, so only related may expand a many-valued reference."""
    create(store, SITES, sites_text(), "Sites")
    create(store, DIARY, diary_text(SITES_SEEN_FIELD), "Diary")
    for query in ({"select": ["sites_seen.code"]},
                  {"group_by": [{"field": "sites_seen.code"}], "aggregates": {"n": {"op": "count"}}}):
        assert refused(store, query).code == "QUERY_UNSUPPORTED"


def test_an_unresolved_branch_can_still_match_under_any(world):
    """A row excluded only by an unresolvable branch is not silently dropped when another matches."""
    result = run(world, {"select": ["title"],
                         "where": {"any": [{"field": "site.name", "op": "eq", "value": "North field"},
                                           {"field": "hours", "op": "gt", "value": 5}]}})
    titles = [item["title"] for item in result["rows"]]
    assert "day two" in titles and "day one" in titles
    assert result["combined"]["excluded_unresolved"] == 0


def test_an_unresolved_row_is_excluded_and_counted_only_when_the_filter_is_unknown(world):
    """eq and not(eq) on an unresolved path are both unknown, so the row counts once either way."""
    for where in ({"field": "site.name", "op": "eq", "value": "North field"},
                  {"not": {"field": "site.name", "op": "eq", "value": "North field"}}):
        result = run(world, {"select": ["title"], "where": where})
        assert "day two" not in [item["title"] for item in result["rows"]]
        assert result["combined"]["excluded_unresolved"] == 1
        assert result["combined"]["unresolved_values"]["values"] == [
            {"reference": "site", "value": "East-Gate", "rows": 1}]


def test_order_by_a_path_ranks_present_null_missing_and_unresolved(world):
    """Unresolved must sort below missing, and a resolved null must sort as null."""
    row(world, SITES, "00000000-0000-4000-8000-0000000000a6", {"code": "null-yard", "name": None})
    row(world, DIARY, K4, {"title": "day four", "site": "null-yard", "hours": 2})
    seen = {}
    for nulls in ("last", "first"):
        result = run(world, {"select": ["title"], "order_by": [{"field": "site.name", "nulls": nulls}]})
        order = [item["title"] for item in result["rows"]]
        assert order == (["day one", "day four", "day three", "day two"] if nulls == "last"
                         else ["day two", "day three", "day four", "day one"])
        seen[nulls] = order
    assert seen["last"] != seen["first"]


def test_a_two_hop_path_reports_the_rows_own_first_hop_value(store):
    """A hop-2 failure names the source row's own key, so a renamed hop-1 target stays visible."""
    regions = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    create(store, regions, collection_text(regions, "Regions", "rid",
                                           "    rid: {type: string, required: true}\n"
                                           "    name: {type: string}\n"), "Regions")
    create(store, SITES, collection_text(
        SITES, "Sites", "code", "    code: {type: string, required: true}\n"
        f"    region: {{type: string, references: {{collection: {regions}}}}}\n"), "Sites")
    create(store, DIARY, diary_text(SITE_FIELD + "    hours: {type: integer}\n"), "Diary")
    row(store, regions, NORTH, {"rid": "north", "name": "North"})
    row(store, SITES, YARD, {"code": "yard", "region": "north"})
    row(store, SITES, "00000000-0000-4000-8000-0000000000a5", {"code": "gate", "region": "gone"})
    row(store, DIARY, K1, {"title": "d1", "site": "yard", "hours": 2})
    row(store, DIARY, K2, {"title": "d2", "site": "gate", "hours": 4})
    result = run(store, {"select": ["title", "site.region.name"], "order_by": [{"field": "title"}]})
    one, two = result["rows"]
    assert one["site.region.name"] == "North" and one["references"]["site.region"] == "resolved"
    assert "site.region.name" not in two and two["references"]["site.region"] == "unresolved"
    assert result["combined"]["unresolved_values"]["values"] == [
        {"reference": "site", "value": "gate", "rows": 1}]
    assert refused(store, {"select": ["site.region.name.deep"]}).code == "QUERY_INPUT_LIMIT"


def test_cyclic_and_three_hop_paths_are_refused_before_any_value_is_read(world):
    """A cycle would make resolution unbounded, and the grammar caps hops at two."""
    cyclic = collection_text(SITES, "Sites", "code",
                             "    code: {type: string, required: true}\n    name: {type: string}\n"
                             f"    diary: {{type: string, references: {{collection: {DIARY}}}}}\n")
    revise(world, SITES, cyclic)
    assert refused(world, {"select": ["site.diary.site"]}).code == "QUERY_UNSUPPORTED"


def test_the_governance_twin_refusals_are_byte_identical(world):
    """A withheld target collection and a deleted one must give the caller the same finding."""
    write_scope(world.root, paths="Records/Sites/**")
    write_rule(world.root, ceiling=0)
    withheld = refused(world, {"related": ["site"]}, who=_external())
    world.connection.execute("PRAGMA foreign_keys=OFF")
    world.connection.execute("DELETE FROM collections WHERE collection_id=?", (SITES,))
    world.connection.commit()
    missing = refused(world, {"related": ["site"]})
    assert withheld.code == missing.code == "COLLECTION_NOT_FOUND"
    assert withheld.finding.as_dict() == missing.finding.as_dict()


def test_withheld_and_missing_target_rows_read_the_same(world):
    """No state, count or diagnostic may tell a withheld row from one that does not exist."""
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{collections.record_ref(SITES, NORTH)}"]\n')
    write_rule(world.root, ceiling=0)
    result = run(world, {"select": ["title", "site.name"], "related": ["site"],
                         "order_by": [{"field": "title"}]}, who=_external())
    by_title = {item["title"]: item for item in result["rows"]}
    for item in (by_title["day one"], by_title["day two"]):
        assert item["references"]["site"] == "unresolved" and "row" not in item["related"]["site"]
    assert by_title["day three"]["references"]["site"] == "none"
    assert result["combined"]["unresolved_values"]["total"] == 2


def test_a_withheld_target_key_refuses_and_a_withheld_field_is_omitted(store):
    """Matching through a hidden key would confirm it; a hidden non-key field is just absent."""
    keyed = collection_text(SITES, "Sites", "code",
                            "    code: {type: string, required: true, classification: location}\n")
    create(store, SITES, keyed, "Sites")
    create(store, DIARY, diary_text(SITE_FIELD), "Diary")
    assert refused(store, {"related": ["site"]}, who=_external()).code == "QUERY_FIELD_UNAVAILABLE"
    hidden = collection_text(SITES, "Sites", "code",
                             "    code: {type: string, required: true}\n    name: {type: string}\n"
                             "    gate_code: {type: string, classification: location}\n")
    revise(store, SITES, hidden)
    row(store, SITES, NORTH, {"code": "North-Field", "name": "North field", "gate_code": "1"})
    row(store, DIARY, K1, {"title": "d", "site": "North-Field"})
    assert refused(store, {"select": ["site.gate_code"]}, who=_external()).code == "QUERY_FIELD_UNAVAILABLE"
    result = run(store, {"related": ["site"]}, who=_external())
    assert "gate_code" not in result["rows"][0]["related"]["site"]["row"]


def test_a_withheld_source_field_is_refused_but_selecting_the_raw_value_is_not(store):
    """Naming a reference the caller cannot read would reveal the value; selecting it cannot."""
    guarded = SITE_FIELD.replace("type: string,", "type: string, classification: location,")
    create(store, SITES, sites_text(), "Sites")
    create(store, DIARY, diary_text(guarded), "Diary")
    row(store, SITES, NORTH, {"code": "North-Field", "name": "North field"})
    row(store, DIARY, K1, {"title": "d", "site": "North-Field"})
    assert refused(store, {"select": ["site.name"]}, who=_external()).code == "QUERY_FIELD_UNAVAILABLE"
    assert refused(store, {"related": ["site"]}, who=_external()).code == "QUERY_FIELD_UNAVAILABLE"
    write_scope(store.root, paths="Records/Sites/**")
    write_rule(store.root, ceiling=0)
    revise(store, DIARY, diary_text(SITE_FIELD))
    result = run(store, {"select": ["site"]}, who=_external())
    assert result["rows"][0]["site"] == "North-Field"


def test_resolution_visits_count_against_the_row_budget(world, monkeypatch):
    """Resolving must pay like any other row visit, or expansion would bypass the cost bound."""
    monkeypatch.setitem(runtime.PROFILES["interactive"], "max_row_visits", 4)
    assert refused(world, {"related": ["site"]}).code == "QUERY_COST_LIMIT"


def test_the_byte_cap_truncates_and_mints_a_continuation(world, monkeypatch):
    """A page that cannot fit must say so and still offer a resumable cursor."""
    monkeypatch.setattr(reference_rows, "MAX_RESULT_BYTES", 4400)
    first = run(world, {"select": ["title"], "related": ["site"], "page": {"limit": 50},
                        "order_by": [{"field": "title"}]})
    assert first["truncated"] and first["truncation_reason"] == "bytes" and first["next_cursor"]
    second = run(world, {"select": ["title"], "related": ["site"], "page": {"limit": 50,
                         "after": first["next_cursor"]}, "order_by": [{"field": "title"}]})
    assert [item["title"] for item in first["rows"] + second["rows"]] == [
        "day one", "day three", "day two"]


def test_a_continuation_resumes_and_stales_only_on_visible_changes(world):
    """Cursor validity must track exactly the rows the caller can see, source or target."""
    query = {"select": ["title"], "related": ["site"], "page": {"limit": 1},
             "order_by": [{"field": "title"}]}
    first = run(world, query)
    assert first["rows"][0]["title"] == "day one"
    second = run(world, {**query, "page": {"limit": 1, "after": first["next_cursor"]}})
    assert second["rows"][0]["title"] == "day three"
    added = world._site_added
    guards = world.inspect_collection(SITES)["lifecycle_guards"]
    world.update_record(SITES, item_key=NORTH, changes={"name": "North renamed"}, why="rename",
                        expected_item_version=added["after_item_hash"],
                        expected_container_hash=guards["expected_container_hash"])
    with pytest.raises(runtime.QueryError, match="QUERY_CURSOR_STALE"):
        run(world, {**query, "page": {"limit": 1, "after": first["next_cursor"]}})


def test_a_declaration_change_stales_the_cursor(world):
    """The binding covers declared references, so adding or dropping one must restart pagination."""
    query = {"select": ["title"], "related": ["site"], "page": {"limit": 1}}
    first = run(world, query)
    revise(world, DIARY, diary_text(SITE_FIELD + SKILL_FIELD.replace(
        f'skill_id: {{type: string, references: {{collection: {SKILLS}}}}}', 'skill_id: {type: string}')
        + "    hours: {type: integer}\n    leave: {type: boolean}\n    day: {type: date}\n"))
    with pytest.raises(runtime.QueryError, match="QUERY_CURSOR_STALE"):
        run(world, {**query, "page": {"limit": 1, "after": first["next_cursor"]}})


@pytest.mark.parametrize("where", [
    {"field": "hours", "op": "eq", "value": 3},
    {"field": "hours", "op": "ne", "value": 3},
    {"field": "hours", "op": "gt", "value": 2},
    {"field": "hours", "op": "between", "value": {"lower": 2, "upper": 6}},
    {"field": "hours", "op": "in", "value": [1, 3]},
    {"field": "hours", "op": "nin", "value": [1, 3]},
    {"field": "title", "op": "contains", "value": "two"},
    {"field": "title", "op": "icontains", "value": "TWO"},
    {"field": "title", "op": "startswith", "value": "day"},
    {"field": "site", "op": "exists"},
    {"field": "site", "op": "missing"},
    {"field": "site", "op": "is_null"},
    {"field": "site", "op": "is_missing"},
    {"field": "site", "op": "is_not_null"},
    {"not": {"field": "hours", "op": "eq", "value": 3}},
    {"any": [{"field": "hours", "op": "eq", "value": 3}, {"field": "leave", "op": "eq", "value": True}]},
])
def test_source_only_predicates_match_the_sql_path(world, where):
    """The Python path must return exactly the rows the typed path returns, in the same order."""
    plain = run(world, {"select": ["title"], "where": where, "order_by": [{"field": "title"}]})
    expanded = run(world, {"select": ["title"], "where": where, "related": ["site"],
                           "order_by": [{"field": "title"}]})
    assert [item["title"] for item in plain["rows"]] == [item["title"] for item in expanded["rows"]]


def test_a_filtered_reduction_totals_exactly_the_combined_rows(world):
    """The row page and every aggregate must come from one filter over one snapshot."""
    result = run(world, {"where": {"field": "site.name", "op": "eq", "value": "North field"},
                         "related": ["site", "skills.skill_id"],
                         "aggregates": {"total": {"op": "sum", "field": "hours"},
                                        "leaves": {"op": "count",
                                                   "where": {"field": "leave", "op": "eq", "value": True}}}})
    assert result["combined"]["count"] == 1 and result["combined"]["excluded_unresolved"] == 1
    assert result["combined"]["unresolved_values"]["values"] == [
        {"reference": "site", "value": "East-Gate", "rows": 1}]
    groups = result["groups"]
    assert groups[0]["total"] == 3 and groups[0]["leaves"] == 1
    assert [item["title"] for item in result["rows"]] == ["day one"]
    assert result["rows"][0]["references"]["site"] == "resolved"


def test_grouping_by_a_path_keeps_unresolved_distinct_from_null_and_missing(world):
    """The unresolved group is a typed state, not a null and not a missing value."""
    row(world, SITES, "00000000-0000-4000-8000-0000000000a6", {"code": "null-yard", "name": None})
    row(world, DIARY, K4, {"title": "day four", "site": "null-yard", "hours": 2})
    result = run(world, {"group_by": [{"field": "site.name"}],
                         "aggregates": {"n": {"op": "count"}, "total": {"op": "sum", "field": "hours"}},
                         "related": ["site"]})
    unresolved = [group for group in result["groups"] if group.get("unresolved") == ["site.name"]]
    assert len(unresolved) == 1 and unresolved[0]["n"] == 1
    names = {group.get("site.name", "<absent>") for group in result["groups"]}
    assert "North field" in names and None in names and "<absent>" in names
    assert all("combined" in group for group in result["groups"])


def test_a_filtered_reduction_without_references_still_uses_the_base_path(world):
    """A ready rollup would skip the filter, so the plan must name a base scan and carry rows."""
    result = run(world, {"where": {"field": "leave", "op": "eq", "value": True},
                         "aggregates": {"n": {"op": "count"}, "total": {"op": "sum", "field": "hours"}}})
    assert result["plan"]["strategy"] == "base" and result["plan"]["reason"] == "filtered"
    assert result["combined"]["count"] == 2 and result["groups"][0]["total"] == 4
    assert sorted(item["title"] for item in result["rows"]) == ["day one", "day three"]


def test_a_counted_where_that_matches_no_row_in_a_group_reports_zero(world):
    """A group with rows but none matching a count's own filter has a count of 0, never null."""
    result = run(world, {"group_by": [{"field": "leave"}],
                         "aggregates": {"long_days": {"op": "count",
                                                      "where": {"field": "hours", "op": "gt", "value": 5}},
                                        "total": {"op": "sum", "field": "hours"}}})
    by_leave = {group["leave"]: group for group in result["groups"]}
    assert by_leave[True]["long_days"] == 0 and by_leave[True]["total"] == 4
    assert by_leave[False]["long_days"] == 1


def test_an_unfiltered_reduction_is_unchanged(world):
    """The old path must not grow rows or combined keys it never had."""
    result = run(world, {"aggregates": {"n": {"op": "count"}, "total": {"op": "sum", "field": "hours"}}})
    assert "rows" not in result and "combined" not in result
    assert result["groups"][0]["n"] == 3 and result["groups"][0]["total"] == 10


def test_unresolved_values_is_bounded_and_reports_its_total(world):
    """The diagnostic list must stay bounded even when many distinct values fail."""
    for index in range(25):
        row(world, DIARY, f"00000000-0000-4000-8000-{index:012d}",
            {"title": f"gap {index:02d}", "site": f"gone-{index:02d}"})
    result = run(world, {"related": ["site"]})
    values = result["combined"]["unresolved_values"]
    assert len(values["values"]) == 20 and values["truncated"] and values["total"] == 26


def test_shared_field_aggregates_do_not_double_count(world):
    """Two aggregates over one field must share an accumulator, never add the value twice."""
    result = run(world, {"where": {"field": "leave", "op": "eq", "value": True},
                         "aggregates": {"total": {"op": "sum", "field": "hours"},
                                        "peak": {"op": "max", "field": "hours"},
                                        "mean": {"op": "avg", "field": "hours"}}})
    group = result["groups"][0]
    assert group["total"] == 4 and group["peak"] == 3 and group["mean"] == 2


_FILTER = {"field": "hours", "op": "gt", "value": 1}


@pytest.mark.parametrize("raw", [
    {"related": ["site"], "execution_profile": "analytics"},
    {"select": ["site.name"], "execution_profile": "analytics"},
    {"where": _FILTER, "aggregates": {"p": {"op": "percentile", "field": "hours", "p": 0.5}}},
    {"where": _FILTER, "aggregates": {"d": {"op": "distinct_count", "field": "hours"}}},
    {"related": ["site"], "group_by": [{"field": "leave"}], "aggregates": {"n": {"op": "count"}},
     "having": {"field": "n", "op": "gt", "value": 0}},
])
def test_compose_refuses_each_reference_shape_that_execute_refuses(world, raw):
    """An agent that refines a query by composing must not get a clean answer for one that cannot run."""
    executed = refused(world, raw)
    composed = refused(world, {**raw, "mode": "compose"})
    assert (composed.code, composed.message) == (executed.code, executed.message)
    assert composed.code == "QUERY_UNSUPPORTED"


def _planning_manifest() -> str:
    """A minimal Planning collection whose site field declares a Records reference."""
    from test_collection_store_writer import manifest_text
    site = f"    site: {{type: string, references: {{collection: {SITES}}}}}\n"
    return manifest_text("planning").replace(
        "    area: {type: string}", "    area: {type: string}\n" + site)


def _create_planning(store):
    from test_collection_store_writer import manifest_path
    create(store, SITES, sites_text(), "Sites")
    store.create_collection(manifest_path("planning"), _planning_manifest(),
                            why="plan the next cycle", scaffold=False)


def test_a_planning_query_reaches_a_records_target_through_a_path(store):
    """A Planning source resolves a declared reference to a Records collection just like a diary."""
    _create_planning(store)
    row(store, SITES, NORTH, {"code": "North-Field", "name": "North field"})
    from test_collection_store_writer import CID
    row(store, CID, "00000000-0000-4000-8000-0000000000b1",
        {"title": "cycle one", "site": "North-Field"})
    with request_scope(owner_principal()):
        result = route.run(store, CID, {"version": 1, "select": ["title", "site.name"]},
                           facade_profile="planning")
    assert result["rows"][0]["site.name"] == "North field"


def test_a_reference_never_changes_planning_state(store):
    """D8: rows referencing a plan item change none of its facade-visible state."""
    _create_planning(store)
    from test_collection_store_writer import CID
    create(store, DIARY, diary_text(
        f"    plan: {{type: string, references: {{collection: {CID}}}}}\n"), "Diary")
    row(store, CID, "00000000-0000-4000-8000-0000000000b1",
        {"title": "cycle one", "status": "candidate"})
    query = {"version": 1, "select": ["title", "status"]}
    with request_scope(owner_principal()):
        before = route.run(store, CID, query, facade_profile="planning")["rows"]
        row(store, DIARY, K1, {"title": "d1", "plan": "cycle one"})
        row(store, DIARY, K2, {"title": "d2", "plan": "cycle one"})
        after = route.run(store, CID, query, facade_profile="planning")["rows"]
        result = route.run(store, DIARY, {"version": 1, "related": ["plan"], "select": ["title"],
                                          "order_by": [{"field": "title"}]}, facade_profile="records")
    assert before == after
    for item in result["rows"]:
        assert item["related"]["plan"]["row"]["title"] == "cycle one"
