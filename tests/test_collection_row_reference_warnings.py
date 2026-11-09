"""Writes, bulk upserts, imports and inspection report unresolved references as warnings."""

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
from test_collection_store_writer import store as store  # noqa: F401
from test_governance_egress import _external, write_rule, write_scope
from test_records_bulk_upsert import EVIDENCE, _evidence

from exomem import structured_collections as collections
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import reference_report

K1 = "00000000-0000-4000-8000-0000000000c1"


@pytest.fixture
def world(store):
    create(store, SITES, sites_text(), "Sites")
    create(store, DIARY, diary_text(SITE_FIELD + "    hours: {type: integer}\n"), "Diary")
    return store


def test_an_append_with_a_missing_target_commits_and_warns(world):
    """The row is saved; the warning names the reference and the value that found no row."""
    with request_scope(owner_principal()):
        result = world.append_record(DIARY, item={"title": "d1", "site": "East-Gate"}, why="write")
    assert result["outcome"] == "committed"
    warnings = result["warnings"]
    assert any(text.startswith("REFERENCE_UNRESOLVED") and "site" in text and "East-Gate" in text
               for text in warnings)
    assert result["reference_check"]["unresolved_values"]["values"] == [
        {"reference": "site", "value": "East-Gate", "rows": 1}]


def test_a_withheld_target_row_warns_like_a_missing_one(world):
    """The writer cannot tell a withheld target from a missing one, so the outputs are identical."""
    row(world, SITES, NORTH, {"code": "North-Field", "name": "North field"})
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{collections.record_ref(SITES, NORTH)}"]\n')
    write_rule(world.root, ceiling=0)
    with request_scope(_external()):
        hidden = reference_report.check_values(world.root, world.handle.path, DIARY,
                                               [{"site": "North-Field"}])
        missing = reference_report.check_values(world.root, world.handle.path, DIARY,
                                                [{"site": "Absent"}])
    def redact(report):
        return repr(report).replace("North-Field", "X").replace("Absent", "X")
    assert redact(hidden) == redact(missing)
    assert reference_report.warnings(hidden) and \
        [w.replace("North-Field", "X") for w in reference_report.warnings(hidden)] == \
        [w.replace("Absent", "X") for w in reference_report.warnings(missing)]


def test_an_unavailable_declaration_is_not_reported_unresolved(world):
    """A key the writer cannot read gives REFERENCE_UNAVAILABLE, never UNRESOLVED."""
    keyed = collection_text(SITES, "Sites", "code",
                            "    code: {type: string, required: true, classification: location}\n")
    revise(world, SITES, keyed)
    with request_scope(_external()):
        report = reference_report.check_values(world.root, world.handle.path, DIARY,
                                               [{"site": "North-Field"}])
    warnings = reference_report.warnings(report)
    assert any(text.startswith("REFERENCE_UNAVAILABLE") for text in warnings)
    assert not any(text.startswith("REFERENCE_UNRESOLVED") for text in warnings)
    assert report["unavailable"] == ["site"]


def test_a_bulk_upsert_bounds_its_unresolved_report(world):
    """Many distinct missing values still produce one bounded report."""
    rows = [{"item": {"title": f"gap {index:02d}", "site": f"gone-{index:02d}"}}
            for index in range(25)]
    _evidence(world.root)
    guards = world.inspect_collection(DIARY)["lifecycle_guards"]
    with request_scope(owner_principal()):
        result = world.bulk_upsert_records(
            DIARY, rows=rows, why="import", source=EVIDENCE,
            expected_container_hash=guards["expected_container_hash"])
    values = result["reference_check"]["unresolved_values"]
    assert len(values["values"]) == 20 and values["truncated"] and values["total"] == 25


def test_an_update_warns_on_a_newly_unresolvable_value(world):
    """Repointing a row at a target that does not exist warns on the update receipt too."""
    added = row(world, DIARY, K1, {"title": "d1", "site": "East-Gate"})
    guards = world.inspect_collection(DIARY)["lifecycle_guards"]
    with request_scope(owner_principal()):
        result = world.update_record(
            DIARY, item_key=K1, changes={"site": "Nowhere"}, why="repoint",
            expected_item_version=added["after_item_hash"],
            expected_container_hash=guards["expected_container_hash"])
    assert result["outcome"] == "committed"
    assert any("Nowhere" in text for text in result["warnings"])


def test_inspect_counts_resolution_under_the_callers_view(world):
    """Inspect reports resolved and unresolved rows; withheld and missing targets read alike."""
    row(world, SITES, NORTH, {"code": "North-Field", "name": "North field"})
    row(world, DIARY, K1, {"title": "d1", "site": "North-Field"})
    row(world, DIARY, "00000000-0000-4000-8000-0000000000c2", {"title": "d2", "site": "East-Gate"})
    with request_scope(owner_principal()):
        result = world.inspect_collection(DIARY)
    entry = next(item for item in result["references"] if item["reference"] == "site")
    assert entry["state"] == "available" and entry["collection"] == SITES
    assert entry["resolved_rows"] == 1 and entry["unresolved_rows"] == 1
    assert entry["rows_with_values"] == 2 and entry["distinct_values"] == 2


def test_an_import_reports_reference_check_at_completion(store):
    """A non-derived import finishing with a declaration must carry the resolution report."""
    from test_collection_store_importer import (
        ndjson,
        run,
        setup,
        start,
        status,
    )
    fields = ("  natural_key: [exercise_id]\n  fields:\n"
              "    exercise_id: {type: string, required: true}\n"
              f"    site: {{type: string, references: {{collection: {SITES}}}}}\n")
    create(store, SITES, sites_text(), "Sites")
    row(store, SITES, NORTH, {"code": "North-Field", "name": "North field"})
    data = ndjson([
        {"id": "w1", "site": "North-Field"},
        {"id": "w2", "site": "gone"},
    ])
    mapping = {"fields": {"exercise_id": "id", "site": "site"},
               "coverage": {"id": {"classification": None}, "site": {"classification": None}},
               "on_invalid": "skip"}
    setup(store, data, fields=fields)
    job = start(store, mapping=mapping)
    run(store)
    result = status(store, job)
    assert result["state"] == "complete"
    assert "reference_check" in result, "a completed import must carry the resolution report"
    site = next(item for item in result["reference_check"] if item.get("reference") == "site")
    assert site["state"] == "available" and site["rows_with_values"] == 2
    assert site["unresolved_rows"] == 1 and site["unresolved_values"]["total"] == 1
