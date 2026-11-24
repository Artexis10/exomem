"""A link field stores the page's stable ref, names a registry relation, and its entries rebuild from rows.

The fixtures invent a log whose `page` field links to one page and whose `pages` field links to several,
over pages that carry an `exomem_id` the way captured pages do.
"""

import json

import pytest
from test_collection_row_references import collection_text, path, revise, row
from test_collection_store_writer import store as store  # noqa: F401
from test_governance_egress import _external, write_rule, write_scope
from test_records_bulk_upsert import EVIDENCE, _evidence

from exomem import structured_collections as collections
from exomem.collection_store import connection, reference_index
from exomem.governance.principal import owner_principal, request_scope
from exomem.query_engine import reference_resolution, route, runtime
from exomem.record_governance import LinkResolution, _LinkProjector

LOG = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
ALPHA = "11111111-1111-4111-8111-111111111111"
BETA = "22222222-2222-4222-8222-222222222222"
HIDDEN = "33333333-3333-4333-8333-333333333333"
K1 = "00000000-0000-4000-8000-0000000000c1"
K2 = "00000000-0000-4000-8000-0000000000c2"
K3 = "00000000-0000-4000-8000-0000000000c3"
LINKS = ("    label: {type: string, required: true}\n"
         "    page: {type: link, relation: cites}\n"
         "    pages: {type: array, items: {type: link}}\n")


def page(root, rel, title, identity):
    target = root / "Knowledge Base" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\nexomem_id: {identity}\ntitle: {title}\n---\n# {title}\n")


def log_text(fields=LINKS):
    return collection_text(LOG, "Log", "label", fields)


@pytest.fixture
def world(store):
    page(store.root, "Notes/Alpha.md", "Alpha", ALPHA)
    store.create_collection(path("Log"), log_text(), why="declare", scaffold=False)
    return store


def stored(store, key=K1):
    return json.loads(store._item(LOG, key)["values_json"])


def append(store, item, key=K1, who=None):
    with request_scope(who or owner_principal()):
        return store.append_record(LOG, item={"label": key[-2:], **item}, item_key=key, why="record")


def link_warnings(result):
    return [text for text in result.get("warnings", []) if text.startswith("LINK_")]


# --- canonicalisation --------------------------------------------------------------------------


def test_a_title_is_stored_as_the_ref_that_survives_a_page_rename(world):
    """A title stored as written breaks when its page is renamed; the stored ref still names the page."""
    result = append(world, {"page": "Alpha", "pages": ["Alpha", "exomem://memory/" + BETA]})
    assert result["outcome"] == "committed" and not link_warnings(result)
    assert stored(world)["page"] == f"exomem://memory/{ALPHA}"
    # A ref the writer was given is kept as given, without a lookup.
    assert stored(world)["pages"] == [f"exomem://memory/{ALPHA}", f"exomem://memory/{BETA}"]
    old = world.root / "Knowledge Base/Notes/Alpha.md"
    page(world.root, "Notes/Renamed.md", "Renamed", ALPHA)
    old.unlink()
    again = append(world, {"page": "Alpha"}, key=K2)
    assert any("Alpha" in text for text in link_warnings(again)), "the old title now names no page"
    assert stored(world)["page"] == f"exomem://memory/{ALPHA}"


def test_an_ambiguous_title_is_kept_and_the_commit_warns_with_at_most_three_candidates(world):
    for index in range(4):
        page(world.root, f"Notes/Same {index}/Twin.md", "Twin", f"{index}0000000-0000-4000-8000-000000000000")
    result = append(world, {"page": "Twin"})
    assert result["outcome"] == "committed"
    assert stored(world)["page"] == "Twin"
    [warning] = link_warnings(result)
    assert warning.startswith("LINK_AMBIGUOUS") and "'page'" in warning and "'Twin'" in warning
    assert warning.count("Notes/Same") == 3


def test_a_withheld_twin_neither_makes_a_title_ambiguous_nor_appears_in_a_warning(world):
    page(world.root, "Private/Twin.md", "Twin", HIDDEN)
    page(world.root, "Notes/Twin.md", "Twin", BETA)
    (world.root / "Knowledge Base/_access.yaml").write_text('excluded: ["Private/Twin.md"]\n')
    result = append(world, {"page": "Twin"})
    assert not link_warnings(result)
    assert stored(world)["page"] == f"exomem://memory/{BETA}"


def test_a_withheld_page_warns_exactly_like_a_missing_one(world):
    """The writer cannot tell a page it may not read from one that does not exist, so the warnings agree."""
    page(world.root, "Private/Secret.md", "Secret", HIDDEN)
    (world.root / "Knowledge Base/_access.yaml").write_text('excluded: ["Private/Secret.md"]\n')
    hidden = append(world, {"page": "Secret"}, key=K1)
    missing = append(world, {"page": "Absent"}, key=K2)
    assert [w.replace("Secret", "X") for w in link_warnings(hidden)] == [
        w.replace("Absent", "X") for w in link_warnings(missing)] != []
    assert stored(world)["page"] == "Secret", "a withheld title is stored as written, never as its ref"


@pytest.mark.parametrize("path_", ["append", "update", "bulk"])
def test_every_write_path_stores_the_ref_and_reports_it_as_supplied_and_stored(world, path_):
    ref = f"exomem://memory/{ALPHA}"
    if path_ == "append":
        result = append(world, {"page": "Alpha"})
    elif path_ == "update":
        added = append(world, {})
        guards = world.inspect_collection(LOG)["lifecycle_guards"]
        with request_scope(owner_principal()):
            result = world.update_record(LOG, item_key=K1, changes={"page": "Alpha"}, why="link",
                                         expected_item_version=added["after_item_hash"],
                                         expected_container_hash=guards["expected_container_hash"])
    else:
        _evidence(world.root)
        guards = world.inspect_collection(LOG)["lifecycle_guards"]
        with request_scope(owner_principal()):
            result = world.bulk_upsert_records(
                LOG, rows=[{"item": {"label": "01", "page": "Alpha"}}, {"item": {"label": "02", "page": "Nowhere"}}],
                why="import", source=EVIDENCE, expected_container_hash=guards["expected_container_hash"])
        assert [w for w in link_warnings(result) if "Nowhere" in w]
        keys = [row["item_key"] for row in result["rows"]]
        assert json.loads(world._item(LOG, keys[0])["values_json"])["page"] == ref
        assert json.loads(world._item(LOG, keys[1])["values_json"])["page"] == "Nowhere"
    if path_ != "bulk":
        assert stored(world)["page"] == ref
    # A value stored as written is warned about, not reported as changed.
    assert result["canonical_links"] == {"count": 1, "items": [{"field": "page", "supplied": "Alpha", "stored": ref}]}


def test_link_notes_join_the_reference_warnings_instead_of_replacing_them(store):
    """A collection with a row reference and a link reports an unresolved value of each in one warnings list."""
    page(store.root, "Notes/Alpha.md", "Alpha", ALPHA)
    fields = LINKS + f"    peer: {{type: string, references: {{collection: {LOG}}}}}\n"
    store.create_collection(path("Log"), log_text(), why="declare", scaffold=False)
    revise(store, LOG, log_text(fields))
    result = append(store, {"page": "Nowhere", "peer": "nobody"})
    assert result["outcome"] == "committed"
    assert any(w.startswith("LINK_UNRESOLVED") for w in result["warnings"])
    assert any("peer" in w and not w.startswith("LINK_") for w in result["warnings"])


# --- relation ----------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["create", "revise"])
def test_a_relation_is_refused_unless_its_origins_include_collection_link(store, how):
    plain = log_text(LINKS.replace("relation: cites", "relation: mentions"))
    if how == "revise":
        store.create_collection(path("Log"), log_text(), why="declare", scaffold=False)
    with pytest.raises(collections.CollectionError) as error:
        if how == "create":
            store.create_collection(path("Log"), plain, why="declare", scaffold=False)
        else:
            revise(store, LOG, plain)
    assert error.value.code == "INVALID_LINK_RELATION"
    details = error.value.details
    assert details["received"] == "mentions" and {"cites", "links_to"} <= set(details["allowed"])
    assert "mentions" not in details["allowed"]


# --- index and inspect -------------------------------------------------------------------------


def _entries(store):
    return sorted(store.handle.connection.execute(
        "SELECT collection_id, field_path, match, row_id FROM item_references WHERE collection_id=?",
        (LOG,)).fetchall())


def _ready(store, cid=LOG):
    store.backfill_derived(cid)
    while store.handle.connection.execute(
            "SELECT 1 FROM item_reference_state WHERE collection_id=? AND ready=0", (cid,)).fetchone():
        store.backfill_derived(cid)


def test_link_entries_rebuild_exactly_from_rows_and_inspect_counts_what_is_indexed(world):
    append(world, {"page": "Alpha", "pages": [f"exomem://record/{LOG}/{K2}", "Elsewhere"]})
    row(world, LOG, K2, {"label": "02", "page": "Alpha"})
    _ready(world)
    first = _entries(world)
    assert {(field, match) for _, field, match, _ in first} == {
        ("page", f"memory:{ALPHA}"), ("pages", f"record:{LOG}/{K2}")}, "a title is not indexed"
    reference_index.rebuild(world.handle.connection, LOG)
    assert _entries(world) == []
    _ready(world)
    assert _entries(world) == first
    with request_scope(owner_principal()):
        links = {entry["link"]: entry for entry in world.inspect_collection(LOG)["links"]}
    assert (links["page"]["indexed"], links["page"]["not_indexed"]) == (2, 0)
    assert (links["pages"]["indexed"], links["pages"]["not_indexed"]) == (1, 1)
    assert links["page"]["relation"] == "cites" and "relation" not in links["pages"]


def test_an_import_stores_refs_and_reports_the_values_it_stored_as_written(store):
    from test_collection_store_importer import CID, ndjson, run, setup, start, status
    page(store.root, "Notes/Alpha.md", "Alpha", ALPHA)
    fields = ("  natural_key: [exercise_id]\n  fields:\n"
              "    exercise_id: {type: string, required: true}\n"
              "    page: {type: link}\n")
    mapping = {"fields": {"exercise_id": "id", "page": "page"},
               "coverage": {"id": {"classification": None}, "page": {"classification": None}},
               "on_invalid": "skip"}
    setup(store, ndjson([{"id": "w1", "page": "Alpha"}, {"id": "w2", "page": "gone"}]), fields=fields)
    job = start(store, mapping=mapping)
    run(store)
    result = status(store, job)
    assert result["state"] == "complete"
    keys = [key for (key,) in store.handle.connection.execute(
        "SELECT item_key FROM items WHERE collection_id=?", (CID,)).fetchall()]
    pages = sorted(json.loads(store._item(CID, key)["values_json"])["page"] for key in keys)
    assert pages == ["exomem://memory/" + ALPHA, "gone"]
    assert [entry["not_indexed"] for entry in result["link_check"]] == [1]
    assert any(text.startswith("LINK_UNRESOLVED") and "'page'" in text for text in result["warnings"])


# --- reads: a canonical row ref -------------------------------------------------------------


def _rows_as(store, who):
    with request_scope(who):
        return {item["label"]: item for item in route.run(
            store, LOG, {"version": 1, "select": ["label", "pages"]}, facade_profile="records")["rows"]}


def _row_refs_world(world):
    """Row 01 links to rows 02 and 03 and to a row that does not exist; a reader outside the owner sees 03 withheld."""
    row(world, LOG, K2, {"label": "02"})
    row(world, LOG, K3, {"label": "03"})
    missing = collections.record_ref(LOG, "00000000-0000-4000-8000-0000000000ff")
    refs = [collections.record_ref(LOG, K2), collections.record_ref(LOG, K3), missing]
    row(world, LOG, K1, {"label": "01", "pages": refs})
    write_scope(world.root, paths="Unrelated/**")
    scope = world.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f'refs: ["{collections.record_ref(LOG, K3)}"]\n')
    write_rule(world.root, ceiling=0)
    return refs


def test_a_row_ref_in_a_link_reads_only_for_a_reader_of_that_row(world):
    """A withheld row and a missing row drop out of the link identically; a readable row stays."""
    refs = _row_refs_world(world)
    assert _rows_as(world, owner_principal())["01"]["pages"] == refs[:2]
    assert _rows_as(world, _external())["01"]["pages"] == refs[:1]


def test_a_row_ref_resolves_to_the_label_of_a_row_the_reader_may_read_and_to_nothing_else(world, monkeypatch):
    monkeypatch.setattr(connection, "store_path", lambda root: world.handle.path)
    refs = _row_refs_world(world)
    with world.read_collection(LOG, facade_profile="records") as manifest:
        pass

    def resolve(who, ref):
        with request_scope(who), runtime.read_session(world.root, world.handle.path) as session:
            operation = session._authorization
            return _LinkProjector.create(
                world.root, manifest, policy=operation.policy, authorize_path=operation.allows_file,
                row_label=reference_resolution.row_labeller(session)).resolve(ref)

    assert resolve(owner_principal(), refs[1]) == LinkResolution("row", label="03")
    assert resolve(_external(), refs[0]) == LinkResolution("row", label="02")
    assert resolve(_external(), refs[1]) == resolve(_external(), refs[2]) == LinkResolution("none")
    # A projector made without a session cannot read rows, so it withholds a row ref as it did before.
    assert _LinkProjector.create(world.root, manifest).resolve(refs[0]) == LinkResolution("none")


def test_a_heading_link_is_kept_as_written_with_a_warning_and_an_alias_alone_becomes_the_ref(world):
    """A stored page ref names a whole page, so storing one for a link to a heading would drop the heading."""
    result = append(world, {"page": "[[Alpha#Launch plan]]", "pages": ["Alpha#Launch plan", "[[Alpha|the plan]]"]})
    assert result["outcome"] == "committed"
    assert stored(world)["page"] == "[[Alpha#Launch plan]]"
    assert stored(world)["pages"] == ["Alpha#Launch plan", f"exomem://memory/{ALPHA}"]
    assert [text.split(":", 1)[0] for text in link_warnings(result)] == ["LINK_ANCHORED", "LINK_ANCHORED"]
    _ready(world)
    assert {(field, match) for _, field, match, _ in _entries(world)} == {("pages", f"memory:{ALPHA}")}
