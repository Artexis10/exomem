"""A read resolves declared references under the caller's own view of source, target and target row.

The fixtures are the declaration tests': sites, skills and a diary whose fields declare references.
"""

import pytest
from test_collection_row_references import (
    DIARY,
    ENTRY,
    GONE,
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
    row,
    sites_text,
    skills_text,
)
from test_collection_store_writer import store as store  # noqa: F401
from test_governance_egress import _external, write_rule, write_scope

from exomem import structured_collections as collections
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import reference_resolution, runtime


@pytest.fixture
def world(store):
    """Sites North-Field and south-yard, a skill, and a diary whose three fields declare references."""
    create(store, SITES, sites_text(), "Sites")
    create(store, SKILLS, skills_text(), "Skills")
    create(store, DIARY, diary_text(SITE_FIELD + SITES_SEEN_FIELD + SKILL_FIELD), "Diary")
    row(store, SITES, NORTH, {"code": "North-Field", "name": "North field"})
    row(store, SITES, YARD, {"code": "south-yard", "name": "South yard"})
    row(store, SKILLS, SLUG, {"slug": "welding"})
    row(store, DIARY, ENTRY, {"title": "day one", "site": "North-Field"})
    return store


def resolve(store, path_, values, *, who=None, limits=None):
    with request_scope(who or owner_principal()), runtime.read_session(
            store.root, store.handle.path, limits=limits) as session:
        return reference_resolution.bind(session, DIARY, path_).resolve(values)


def test_a_value_resolves_to_the_row_whose_key_equals_it(world):
    """The reference is read as the key the owner writes, never as a row id or a title."""
    found = resolve(world, "site", ["North-Field", "south-yard", "East-Gate"])
    assert found["North-Field"].item_key == NORTH
    assert found["south-yard"].item_key == YARD
    assert found["East-Gate"] is None


def test_matching_is_case_sensitive_and_untrimmed(world):
    """A fuzzy match would join the wrong row after a rename and hide the broken reference."""
    found = resolve(world, "site", ["north-field", " North-Field", "North-Field "])
    assert set(found.values()) == {None}


def test_matching_normalises_unicode_to_nfc(world):
    """The same text typed with a combining accent must find the row stored precomposed."""
    row(world, SITES, "00000000-0000-4000-8000-0000000000a3", {"code": "Café"})
    found = resolve(world, "site", ["Café"])
    assert found["Café"].item_key == "00000000-0000-4000-8000-0000000000a3"


def test_a_canonical_row_ref_resolves_only_to_its_own_collection(world):
    """A ref to another collection must not fall back to a key that happens to spell the same."""
    own = collections.record_ref(SITES, YARD)
    other = collections.record_ref(SKILLS, SLUG)
    row(world, SITES, "00000000-0000-4000-8000-0000000000a4", {"code": other})
    found = resolve(world, "site", [own, other])
    assert found[own].item_key == YARD
    assert found[other] is None


def test_a_malformed_date_key_reads_unresolved_without_raising(store):
    """A bad date in a source row must show as unresolved, not fail the whole read."""
    shifts = "dddd0000-0000-4000-8000-000000000001"
    create(store, shifts, collection_text(shifts, "Shifts", "day", "    day: {type: date, required: true}\n"), "Shifts")
    create(store, DIARY, diary_text(f"    shift: {{type: string, references: {{collection: {shifts}}}}}\n"), "Diary")
    row(store, shifts, NORTH, {"day": "2026-03-04"})
    found = resolve(store, "shift", ["2026-03-04", "20260304", "not-a-date", "2026-13-40"])
    assert found["2026-03-04"].item_key == NORTH
    assert found["20260304"].item_key == NORTH
    assert found["not-a-date"] is None and found["2026-13-40"] is None


def test_declared_references_read_distinct_values_from_every_declared_position(store):
    """A repeated, null or empty value must not count as another reference."""
    create(store, SITES, sites_text(), "Sites")
    create(store, SKILLS, skills_text(), "Skills")
    create(store, DIARY, diary_text(SITE_FIELD + SITES_SEEN_FIELD + SKILL_FIELD), "Diary")
    with store.read_collection(DIARY) as manifest:
        declared = {item.dotted: item for item in collections.declared_references(manifest.schema)}
    values = {"site": "North-Field", "sites_seen": ["A", "B", "A", "", None],
              "skills": [{"skill_id": "welding"}, {"skill_id": "welding", "minutes": 5}, {"minutes": 9}, "stray"]}
    assert declared["site"].values(values) == ("North-Field",)
    assert declared["sites_seen"].values(values) == ("A", "B")
    assert declared["skills.skill_id"].values(values) == ("welding",)
    assert declared["site"].values({"site": None}) == declared["site"].values({}) == declared["site"].values({"site": ""}) == ()


def test_a_withheld_target_row_reads_exactly_like_a_missing_one(world):
    """The caller must not learn that a row exists, from its status or from what the lookup costs."""
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{collections.record_ref(SITES, YARD)}"]\n')
    write_rule(world.root, ceiling=0)
    with request_scope(_external()), runtime.read_session(world.root, world.handle.path) as session:
        bound = reference_resolution.bind(session, DIARY, "site")
        before = session._estimated_visits
        found = bound.resolve(["south-yard", "East-Gate", "North-Field"])
        spent = session._estimated_visits - before
    assert found["south-yard"] is None and found["East-Gate"] is None
    assert found["North-Field"].item_key == NORTH
    assert spent == 3


def test_a_lookup_costs_one_visit_per_distinct_value_whatever_it_resolves_to(world):
    """A cost that depended on the outcome would let a caller probe which values exist."""
    limits = runtime.QueryLimits(max_row_visits=2)
    assert len(resolve(world, "site", ["North-Field", "East-Gate"], limits=limits)) == 2
    with pytest.raises(runtime.QueryError) as error:
        resolve(world, "site", ["North-Field", "East-Gate", "East-Gate", "West-Gate"], limits=limits)
    assert error.value.code == "QUERY_COST_LIMIT"


def test_a_withheld_target_collection_reads_exactly_like_a_missing_one(world):
    """A caller who cannot read the target collection must not be able to tell it from none."""
    write_scope(world.root, paths="Records/Sites/**")
    write_rule(world.root, ceiling=0)
    seen = []
    with request_scope(_external()), runtime.read_session(world.root, world.handle.path) as session:
        for refused in (lambda: session.admit_lookup(SITES), lambda: session.admit_lookup(GONE),
                        lambda: reference_resolution.bind(session, DIARY, "site")):
            with pytest.raises(runtime.QueryError) as error:
                refused()
            seen.append((error.value.code, error.value.message))
    assert seen == [("COLLECTION_NOT_FOUND", "collection query could not complete")] * 3


def test_a_target_key_the_caller_cannot_read_is_refused_not_resolved(store):
    """Matching through a withheld key would confirm its values one probe at a time."""
    keyed = collection_text(SITES, "Sites", "code",
                            "    code: {type: string, required: true, classification: location}\n")
    create(store, SITES, keyed, "Sites")
    create(store, DIARY, diary_text(SITE_FIELD), "Diary")
    row(store, SITES, NORTH, {"code": "North-Field"})
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        with pytest.raises(runtime.QueryError) as error:
            reference_resolution.bind(session, DIARY, "site")
    assert error.value.code == "QUERY_FIELD_UNAVAILABLE"
    assert resolve(store, "site", ["North-Field"])["North-Field"].item_key == NORTH


def test_a_source_field_the_caller_cannot_read_is_refused(store):
    """Resolving a withheld field would show its value through the target row."""
    guarded = SITE_FIELD.replace("type: string,", "type: string, classification: location,")
    create(store, SITES, sites_text(), "Sites")
    create(store, DIARY, diary_text(guarded), "Diary")
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        with pytest.raises(runtime.QueryError) as error:
            reference_resolution.bind(session, DIARY, "site")
    assert error.value.code == "QUERY_FIELD_UNAVAILABLE"


def test_a_declared_member_the_caller_cannot_read_inside_a_readable_array_is_refused(store):
    """The array stays readable, so the withheld member's declaration is still visible and must not resolve."""
    guarded = SKILL_FIELD.replace("skill_id: {type: string,", "skill_id: {type: string, classification: location,")
    create(store, SKILLS, skills_text(), "Skills")
    create(store, DIARY, diary_text(guarded), "Diary")
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        with pytest.raises(runtime.QueryError) as error:
            reference_resolution.bind(session, DIARY, "skills.skill_id")
    assert error.value.code == "QUERY_FIELD_UNAVAILABLE"
    with request_scope(owner_principal()), runtime.read_session(store.root, store.handle.path) as session:
        assert reference_resolution.bind(session, DIARY, "skills.skill_id").many


def test_a_target_row_is_read_under_the_target_collections_field_plan(store):
    """A field the caller may not read on the target must not travel with the resolved row."""
    sites = collection_text(SITES, "Sites", "code", "    code: {type: string, required: true}\n"
                            "    name: {type: string}\n    gate_code: {type: string, classification: location}\n")
    create(store, SITES, sites, "Sites")
    create(store, DIARY, diary_text(SITE_FIELD), "Diary")
    row(store, SITES, NORTH, {"code": "North-Field", "name": "North field", "gate_code": "1234"})
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        bound = reference_resolution.bind(session, DIARY, "site")
        target = bound.resolve(["North-Field"])["North-Field"]
        assert bound.read(target, ["name"]) == {"name": "North field"}
        with pytest.raises(runtime.QueryError) as error:
            bound.read(target, ["name", "gate_code"])
    assert error.value.code == "QUERY_FIELD_UNAVAILABLE"


def test_a_link_on_a_target_row_to_a_page_the_caller_cannot_read_is_dropped(store):
    """A resolved row must not carry a link that the caller's own query on the target would drop."""
    sites = collection_text(SITES, "Sites", "code", "    code: {type: string, required: true}\n"
                            "    see: {type: link}\n")
    create(store, SITES, sites, "Sites")
    create(store, DIARY, diary_text(SITE_FIELD), "Diary")
    for page in ("Hidden", "Open"):
        (store.root / f"Knowledge Base/Notes/{page}.md").parent.mkdir(parents=True, exist_ok=True)
        (store.root / f"Knowledge Base/Notes/{page}.md").write_text(f"# {page}\n")
    row(store, SITES, NORTH, {"code": "North-Field", "see": "[[Knowledge Base/Notes/Hidden]]"})
    row(store, SITES, YARD, {"code": "south-yard", "see": "[[Knowledge Base/Notes/Open]]"})
    write_scope(store.root, paths="Notes/Hidden.md")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()), runtime.read_session(store.root, store.handle.path) as session:
        bound = reference_resolution.bind(session, DIARY, "site")
        found = bound.resolve(["North-Field", "south-yard"])
        assert bound.read(found["North-Field"], ["code", "see"]) == {"code": "North-Field"}
        assert bound.read(found["south-yard"], ["code", "see"]) == {
            "code": "south-yard", "see": "[[Knowledge Base/Notes/Open]]"}
