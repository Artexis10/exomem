"""End to end: one governed call answers a cross-collection question (task 3.1).

A diary, its sites and its skills — one query filters on the site's name, expands
both references, sums hours and counts leave days, without the caller composing
key lookups or joins.
"""

import pytest
from test_collection_row_references import (
    DIARY,
    NORTH,
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
from test_collection_store_writer import store as store  # noqa: F401
from test_governance_egress import _external, write_rule, write_scope

from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import route, runtime

K1, K2, K3, K4 = (f"00000000-0000-4000-8000-0000000000c{i}" for i in (1, 2, 3, 4))

FIELDS = (SITE_FIELD + SKILL_FIELD
          + "    hours: {type: integer, filterable: true}\n    leave: {type: boolean}\n")


@pytest.fixture
def world(store):
    create(store, SITES, sites_text(), "Sites")
    create(store, SKILLS, skills_text(), "Skills")
    text = diary_text(FIELDS).replace(
        "title: {type: string, required: true}",
        "title: {type: string, required: true, filterable: true, sortable: true}")
    create(store, DIARY, text, "Diary")
    row(store, SITES, NORTH, {"code": "North-Field", "name": "North field"})
    row(store, SKILLS, SLUG, {"slug": "welding"})
    row(store, DIARY, K1, {"title": "monday", "site": "North-Field", "hours": 3,
                           "leave": True, "skills": [{"skill_id": "welding"}]})
    row(store, DIARY, K2, {"title": "tuesday", "site": "North-Field", "hours": 5,
                           "leave": False, "skills": [{"skill_id": "welding"},
                                                      {"skill_id": "missing"}]})
    row(store, DIARY, K3, {"title": "wednesday", "site": "south-yard", "hours": 7})
    return store


def answer(store, query, who=None):
    """The store-mode query entry; record_memory refuses the full grammar on summary collections."""
    with request_scope(who or owner_principal()):
        return route.run(store, DIARY, {"version": 1, **query}, facade_profile="records")


def test_one_call_answers_across_collections(world):
    """The caller asks once; the engine resolves, filters, expands and reduces."""
    result = answer(world, {
        "version": 1,
        "where": {"field": "site.name", "op": "eq", "value": "North field"},
        "related": ["site", "skills.skill_id"],
        "aggregates": {"total_hours": {"op": "sum", "field": "hours"},
                       "leave_days": {"op": "count",
                                      "where": {"field": "leave", "op": "eq", "value": True}}},
    })
    assert result["combined"]["count"] == 2
    groups = result["groups"]
    assert groups[0]["total_hours"] == 8 and groups[0]["leave_days"] == 1
    titles = [item["title"] for item in result["rows"]]
    assert titles == ["monday", "tuesday"]
    monday = result["rows"][0]
    assert monday["related"]["site"]["row"]["name"] == "North field"
    assert monday["related"]["site"]["state"] == "resolved"
    tuesday = result["rows"][1]
    assert tuesday["related"]["skills.skill_id"]["count"] == 2
    assert tuesday["related"]["skills.skill_id"]["state"] == "unresolved"


def test_missing_and_renamed_targets_report_exactly(world):
    """A renamed site strands its old key: counted out of the total, itemized in diagnostics."""
    row(world, DIARY, K4, {"title": "thursday", "site": "East-Gate", "hours": 9})
    row(world, DIARY, "00000000-0000-4000-8000-0000000000c5",
        {"title": "friday", "site": "East-Gate", "hours": 1})
    result = answer(world, {
        "version": 1,
        "where": {"field": "site.name", "op": "eq", "value": "North field"},
        "related": ["site"],
        "aggregates": {"total_hours": {"op": "sum", "field": "hours"},
                       "n": {"op": "count"}},
    })
    combined = result["combined"]
    # K1+K2 combine (North field resolves), K3 south-yard + K4+K5 East-Gate are all unknown.
    assert combined["count"] == 2
    assert combined["excluded_unresolved"] == 3
    values = {item["value"]: item["rows"] for item in combined["unresolved_values"]["values"]}
    assert values == {"south-yard": 1, "East-Gate": 2}
    assert result["groups"][0]["total_hours"] == 8


def test_withheld_target_fields_bound_like_missing_ones(store):
    """A hidden non-key field omits itself in related; a hidden key refuses the query."""
    hidden = collection_text(SITES, "Sites", "code",
                             "    code: {type: string, required: true}\n    name: {type: string}\n"
                             "    gate_code: {type: string, classification: location}\n")
    create(store, SITES, hidden, "Sites")
    create(store, DIARY, diary_text(SITE_FIELD), "Diary")
    row(store, SITES, NORTH, {"code": "North-Field", "name": "North field", "gate_code": "1"})
    row(store, DIARY, K1, {"title": "d1", "site": "North-Field"})
    write_scope(store.root, paths="Unrelated/**")
    write_rule(store.root, ceiling=0)
    result = answer(store, {"version": 1, "related": ["site"]}, who=_external())
    target_row = result["rows"][0]["related"]["site"]["row"]
    assert "name" in target_row and "gate_code" not in target_row
    with pytest.raises(runtime.QueryError) as error:
        answer(store, {"version": 1, "where": {"field": "site.gate_code", "op": "eq", "value": "1"}},
               who=_external())
    assert error.value.code == "QUERY_FIELD_UNAVAILABLE"
    keyed = collection_text(SITES, "Sites", "code",
                            "    code: {type: string, required: true, classification: location}\n"
                            "    name: {type: string}\n"
                            "    gate_code: {type: string, classification: location}\n")
    revise(store, SITES, keyed)
    with pytest.raises(runtime.QueryError) as error:
        answer(store, {"version": 1, "related": ["site"]}, who=_external())
    assert error.value.code == "QUERY_FIELD_UNAVAILABLE"
