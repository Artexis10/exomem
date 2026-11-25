"""Records writes that state when their values took effect (OpenSpec record-value-timeline 4.2-4.6).

Each case names the failure only it catches. Store collections run on the real CollectionWriter; the
file-canonical and dataset cases run on vault fixtures without a store. The lamps, shelves and dates are invented.
The dates lie in March 2026, before any write, so each case holds whenever it runs.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from test_record_timeline_schema import LAMPS, equipment_manifest

from exomem import records
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import connection, history, timeline
from exomem.collection_store.preview import preview_store
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope, owner_principal, request_scope
from exomem.record_memory import record_memory

LAMPS_PATH = "Knowledge Base/Records/Lamps/_collection.md"
SOURCE = "Knowledge Base/Sources/inventory.md"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "vault"
    (root / "Knowledge Base/Sources").mkdir(parents=True)
    (root / SOURCE).write_text("---\ntype: source\nexomem_id: 0c9a3f1e-1111-4222-8333-944455556666\n---\n\n"
                               "The March stock count.\n")
    with connection.open_writer(tmp_path / "collections.sqlite", lease_check=lambda: True) as handle, library_scope():
        writer = CollectionWriter(root, handle)
        writer.create_collection(LAMPS_PATH, equipment_manifest(LAMPS, "Lamps"), why="declare", scaffold=False)
        yield writer


def add_lamp(store):
    """Append lamp L-17 on Shelf A from 1 March and return its key."""
    return store.append_record(LAMPS, item={"asset_tag": "L-17", "location": "Shelf A"}, why="capture",
                               effective_at="2026-03-01")["item_key"]


def edit(store, key, changes, **arguments):
    container, version = store.current_guards(LAMPS, key)
    return store.update_record(LAMPS, item_key=key, changes=changes, expected_container_hash=container,
                               expected_item_version=version, why=arguments.pop("why", "move"), **arguments)


def read(store, action, **arguments):
    with request_scope(owner_principal(surface="mcp")), preview_store(store.root, store.handle):
        return record_memory(store.root, action, collection=LAMPS, **arguments)


def current(store, key):
    """The row's current values, as a query returns them."""
    [row] = read(store, "query", item_key=key)["rows"]
    return row


def points(store, key, field="location"):
    """The field's live points as (value, effective time, basis, placement), read through the span function."""
    row_id = store.connection.execute("SELECT row_id FROM item_rows WHERE item_key=?", (key,)).fetchone()[0]
    line = timeline.fold(*timeline.histories(store.connection, [row_id])[row_id])
    return [(point.value, point.effective.text, point.basis, point.placement) for point in line.live[field]]


def stored(store):
    return tuple(store.connection.iterdump())


def refusal(call, *arguments, **keywords):
    with pytest.raises(collections.CollectionError) as refused:
        call(*arguments, **keywords)
    return refused.value


def revisions_of(store, key):
    return read(store, "history", item_key=key)["revisions"]


# --- 4.2 effective_at ---------------------------------------------------------------------------------------


def test_a_move_recorded_late_keeps_its_real_date(store):
    """A writer that stamps the write time on a late edit puts the move three days late on the timeline."""
    key = add_lamp(store)

    reply = edit(store, key, {"location": "Shelf B"}, effective_at="2026-03-07")

    assert reply["timeline"] == {"effective_at": "2026-03-07", "effective_basis": "stated",
                                 "placements": {"location": "current"}}
    assert current(store, key)["location"] == "Shelf B"
    assert points(store, key) == [("Shelf A", "2026-03-01", "stated", "current"),
                                  ("Shelf B", "2026-03-07", "stated", "current")]


def test_an_unstated_effective_time_is_the_write_time_and_says_so(store):
    """A defaulted time that reads as `stated` passes a guess off as a fact; one that is stored rewrites
    history written before timeline facts existed."""
    key = add_lamp(store)

    reply = edit(store, key, {"location": "Shelf B"})

    [(written_at,)] = store.connection.execute("SELECT committed_at FROM txns ORDER BY txn_id DESC LIMIT 1")
    expected = timeline.written(written_at).text
    assert reply["timeline"] == {"effective_at": expected, "effective_basis": "written",
                                 "placements": {"location": "current"}}
    assert points(store, key)[-1] == ("Shelf B", expected, "written", "current")
    newest = revisions_of(store, key)[0]
    assert (newest["effective_at"], newest["effective_basis"]) == (expected, "written")
    assert store.connection.execute("SELECT COUNT(*) FROM version_effective").fetchone() == (1,)  # the append's


def test_a_future_effective_time_is_refused_and_writes_nothing(store):
    """Intended future state recorded as fact would show a lamp on a shelf it has not reached."""
    key = add_lamp(store)
    before = stored(store)
    later = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=10)).isoformat()

    refused = refusal(edit, store, key, {"location": "Shelf B"}, effective_at=later)

    assert refused.code == "EFFECTIVE_AT_INVALID" and refused.details["effective_at"] == later
    assert "Planning" in refused.details["remediation"]
    assert stored(store) == before


def test_a_writer_ahead_of_utc_may_name_its_own_today(store):
    """A bound at the UTC date refuses an owner east of UTC the date on their own calendar."""
    key = add_lamp(store)
    tomorrow = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)).isoformat()

    reply = edit(store, key, {"location": "Shelf B"}, effective_at=tomorrow)

    assert reply["outcome"] == "committed" and reply["timeline"]["effective_at"] == tomorrow


def test_a_replayed_request_returns_the_same_timeline_block(store):
    """A retry after a lost reply that answers without the block, or records the move twice."""
    key = add_lamp(store)
    container, version = store.current_guards(LAMPS, key)
    request = dict(item_key=key, changes={"location": "Shelf B"}, expected_container_hash=container,
                   expected_item_version=version, why="move", effective_at="2026-03-07", request_id="move-l17-once")

    first = store.update_record(LAMPS, **request)
    replay = store.update_record(LAMPS, **request)

    assert replay["timeline"] == first["timeline"] == {"effective_at": "2026-03-07", "effective_basis": "stated",
                                                       "placements": {"location": "current"}}
    assert len(points(store, key)) == 2


def test_a_bulk_rows_own_effective_time_wins_over_the_batchs(store):
    """A batch time that overrides a row's own time files that row's stock count on the wrong day."""
    reply = store.bulk_upsert_records(
        LAMPS, rows=[{"item": {"asset_tag": "L-17", "location": "Shelf A"}, "effective_at": "2026-03-04"},
                     {"item": {"asset_tag": "L-18", "location": "Shelf A"}}],
        why="stock count", expected_container_hash=store.inspect_collection(LAMPS)["lifecycle_guards"][
            "expected_container_hash"], source=SOURCE, effective_at="2026-03-02")

    assert [(row["outcome"], row["effective_at"], row["effective_basis"]) for row in reply["rows"]] == [
        ("inserted", "2026-03-04", "stated"), ("inserted", "2026-03-02", "stated")]


# --- 4.3 placements -----------------------------------------------------------------------------------------


def test_a_missed_move_is_filled_in_and_the_current_value_stays(store):
    """An edit dated before the latest point that overwrites current state puts the lamp back on the bench."""
    key = add_lamp(store)
    edit(store, key, {"location": "Shelf B"}, effective_at="2026-03-07")
    versions = store.connection.execute("SELECT COUNT(*) FROM version_identity").fetchone()[0]

    reply = edit(store, key, {"location": "Repair Bench"}, effective_at="2026-03-03", why="the bench visit")

    assert reply["timeline"]["placements"] == {"location": "past"}
    assert current(store, key)["location"] == "Shelf B"
    assert points(store, key) == [("Shelf A", "2026-03-01", "stated", "current"),
                                  ("Repair Bench", "2026-03-03", "stated", "past"),
                                  ("Shelf B", "2026-03-07", "stated", "current")]
    # The past point rides on a version of its own, so the chain records it.
    assert store.connection.execute("SELECT COUNT(*) FROM version_identity").fetchone()[0] == versions + 1


def test_one_edit_places_two_fields_differently(store):
    """A placement decided once per edit, not per field, either loses the location move or overwrites a
    newer status."""
    key = add_lamp(store)
    edit(store, key, {"status": "lent"}, effective_at="2026-03-08")

    reply = edit(store, key, {"location": "Shelf B", "status": "in store"}, effective_at="2026-03-05")

    assert reply["timeline"]["placements"] == {"location": "current", "status": "past"}
    row = current(store, key)
    assert (row["location"], row["status"]) == ("Shelf B", "lent")
    assert [value for value, *_ in points(store, key, "status")] == [timeline.MISSING, "in store", "lent"]


def test_a_date_on_the_same_day_as_a_timed_change_is_current(store):
    """Comparing a date at its 00:00 files a same-day edit as past, so the lamp never reaches Shelf C."""
    key = add_lamp(store)
    edit(store, key, {"location": "Shelf B"}, effective_at="2026-03-07T09:00:00Z")

    reply = edit(store, key, {"location": "Shelf C"}, effective_at="2026-03-07")

    assert reply["timeline"]["placements"] == {"location": "current"}
    assert current(store, key)["location"] == "Shelf C"
    assert [value for value, *_ in points(store, key)] == ["Shelf A", "Shelf B", "Shelf C"]


def test_bulk_upsert_refuses_a_past_placement_and_an_abort_writes_nothing(store):
    """Bulk upsert changes current state only; a row dated before a field's latest point would overwrite it."""
    key = add_lamp(store)
    edit(store, key, {"location": "Shelf B"}, effective_at="2026-03-07")
    before = stored(store)

    reply = store.bulk_upsert_records(
        LAMPS, rows=[{"item": {"asset_tag": "L-17", "location": "Repair Bench"}, "effective_at": "2026-03-03"},
                     {"item": {"asset_tag": "L-18", "location": "Shelf A"}}],
        why="stock count", expected_container_hash=store.current_guards(LAMPS, key)[0], source=SOURCE,
        on_reject="abort")

    [rejected] = [row for row in reply["rows"] if row["outcome"] == "rejected"]
    assert rejected["code"] == "EFFECTIVE_BEFORE_CURRENT"
    assert rejected["fields"] == [{"field": "location", "latest_effective_at": "2026-03-07"}]
    assert "edit_record" in rejected["remediation"]
    assert stored(store) == before


def test_reviving_a_retracted_row_before_its_latest_point_writes_nothing(store):
    """Adding a retracted row back revives it. Placed before the location it last held, the revived location
    would be current yet older than that point, and a timeline would show the row moving backward."""
    # A collection with one view file per row cannot retract, so this one keeps a summary view only.
    spares = "9d5b3c21-6e7f-4a80-8b1c-2d3e4f5a6b7c"
    text = equipment_manifest(spares, "Spares").replace("strategy: markdown-items\n", "strategy: sqlite\n").replace(
        "lifecycle: active\n", "lifecycle: active\nview_mode: summary\n")
    store.create_collection("Knowledge Base/Records/Spares/_collection.md", text, why="declare", scaffold=False)
    key = store.append_record(spares, item={"asset_tag": "L-17", "location": "Shelf A"}, why="capture",
                              effective_at="2026-03-01")["item_key"]
    container, version = store.current_guards(spares, key)
    store.update_record(spares, item_key=key, changes={"location": "Shelf B"}, expected_container_hash=container,
                        expected_item_version=version, why="move", effective_at="2026-03-07")
    container, version = store.current_guards(spares, key)
    store.retract_record(spares, item_key=key, expected_container_hash=container, expected_item_version=version,
                         why="sold")
    before = stored(store)

    refused = refusal(store.append_record, spares, item={"asset_tag": "L-17", "location": "Shelf D"},
                      why="bought back", effective_at="2026-03-05")

    assert refused.code == "EFFECTIVE_BEFORE_CURRENT"
    assert refused.details["fields"] == [{"field": "location", "latest_effective_at": "2026-03-07"}]
    assert stored(store) == before


# --- 4.4 corrections ----------------------------------------------------------------------------------------


def moved_to_shelf_b(store):
    """Lamp L-17 on Shelf A from 1 March and on Shelf B from 7 March; the key and the move's transition id
    (a write reply names it `audit_correlation`)."""
    key = add_lamp(store)
    return key, edit(store, key, {"location": "Shelf B"}, effective_at="2026-03-07")["audit_correlation"]


def test_a_wrong_place_is_corrected_with_no_change_between_the_two_values(store):
    """A correction recorded as a new move shows the lamp going from Shelf B to Shelf C, which it never did."""
    key, move = moved_to_shelf_b(store)

    reply = edit(store, key, {"location": "Shelf C"}, corrects=move, why="it was Shelf C")

    assert reply["timeline"] == {"effective_at": "2026-03-07", "effective_basis": "stated",
                                 "placements": {"location": "current"}, "corrects": move}
    assert current(store, key)["location"] == "Shelf C"
    assert points(store, key) == [("Shelf A", "2026-03-01", "stated", "current"),
                                  ("Shelf C", "2026-03-07", "stated", "current")]


def test_a_wrong_date_is_corrected_without_new_values(store):
    """A re-date that needs a value, or that adds a point, leaves the move on its wrong day."""
    key, move = moved_to_shelf_b(store)

    edit(store, key, {}, corrects=move, effective_at="2026-03-09", why="it moved on the 9th")

    assert current(store, key)["location"] == "Shelf B"
    assert points(store, key) == [("Shelf A", "2026-03-01", "stated", "current"),
                                  ("Shelf B", "2026-03-09", "stated", "current")]


def test_a_past_value_is_corrected_and_the_current_value_stays(store):
    """A corrected past value that lands in current state moves the lamp off the shelf it is on."""
    key, _ = moved_to_shelf_b(store)
    bench = edit(store, key, {"location": "Repair Bench"}, effective_at="2026-03-03")["audit_correlation"]

    reply = edit(store, key, {"location": "Paint Room"}, corrects=bench, why="it was the paint room")

    assert reply["timeline"]["placements"] == {"location": "past"}
    assert current(store, key)["location"] == "Shelf B"
    assert points(store, key) == [("Shelf A", "2026-03-01", "stated", "current"),
                                  ("Paint Room", "2026-03-03", "stated", "past"),
                                  ("Shelf B", "2026-03-07", "stated", "current")]


def test_a_correction_can_itself_be_corrected(store):
    """A correction whose point cannot be named again leaves a second mistake in place for good."""
    key, move = moved_to_shelf_b(store)
    first = edit(store, key, {"location": "Shelf C"}, corrects=move)["audit_correlation"]

    edit(store, key, {"location": "Shelf D"}, corrects=first, why="Shelf D, on a second look")

    assert points(store, key) == [("Shelf A", "2026-03-01", "stated", "current"),
                                  ("Shelf D", "2026-03-07", "stated", "current")]


@pytest.mark.parametrize(("arguments", "code"), [
    ({"changes": {"location": "Shelf C"}, "corrects": "00000000-0000-4000-8000-000000000000"},
     "TIMELINE_POINT_NOT_FOUND"),
    ({"changes": {"status": "lent"}, "corrects": "move"}, "TIMELINE_POINT_NOT_FOUND"),  # the move set no status
    ({"changes": {}, "corrects": "move", "effective_at": "2026-02-20"}, "TIMELINE_ORDER_CONFLICT"),
    ({"changes": {}, "corrects": "move"}, "INVALID_RECORD_CHANGES"),
])
def test_a_refused_correction_writes_nothing(store, arguments, code):
    """A refusal that lands half a correction leaves a timeline no request asked for."""
    key, move = moved_to_shelf_b(store)
    arguments = {**arguments, **({"corrects": move} if arguments["corrects"] == "move" else {})}
    before = stored(store)

    refused = refusal(edit, store, key, arguments.pop("changes"), **arguments)

    assert refused.code == code and refused.details["remediation"]
    if code == "TIMELINE_ORDER_CONFLICT":  # it names the field and the point the correction would pass
        assert refused.details["field"] == "location"
        assert refused.details["point"]["effective_at"] == "2026-03-01"
    assert stored(store) == before


# --- 4.5 history --------------------------------------------------------------------------------------------


def test_history_shows_a_corrected_move_and_its_correction_each_naming_the_other(store):
    """History that drops the replaced point, or does not link the two writes, loses what was believed when."""
    key, move = moved_to_shelf_b(store)
    bench = edit(store, key, {"location": "Repair Bench"}, effective_at="2026-03-03", why="the bench visit")
    fix = edit(store, key, {"location": "Shelf C"}, corrects=move, why="it was Shelf C")

    revisions = {revision["transition_id"]: revision for revision in revisions_of(store, key)}

    [(written_at,)] = store.connection.execute("SELECT committed_at FROM txns WHERE transition_id=?",
                                               (fix["audit_correlation"],))
    assert revisions[move]["replaced_by"] == [{"transition_id": fix["audit_correlation"], "written_at": written_at,
                                               "fields": ["location"]}]
    assert (revisions[move]["effective_at"], revisions[move]["corrects"]) == ("2026-03-07", None)
    correction = revisions[fix["audit_correlation"]]
    assert (correction["corrects"], correction["effective_at"], correction["effective_basis"]) == (
        move, "2026-03-07", "stated")
    assert correction["placements"] == {"location": "current"} and correction["replaced_by"] == []
    # A write that changed only a past point is a revision of its own, with its why.
    past = revisions[bench["audit_correlation"]]
    assert (past["why"], past["placements"], past["past_values"]) == (
        "the bench visit", {"location": "past"}, {"location": "Repair Bench"})
    # The generated page shows each stated time: the append's, the move's, the bench visit's and the one the
    # correction inherits from the move.
    manifest = store._projection_manifest(LAMPS)
    page = history.render_page(store.connection, store.root, manifest, history.main_path(manifest))[1]
    assert sorted(line for line in page.splitlines() if line.startswith("Effective: ")) == [
        "Effective: 2026-03-01", "Effective: 2026-03-03", "Effective: 2026-03-07", "Effective: 2026-03-07"]


# --- 4.6 collections without a timeline ---------------------------------------------------------------------


def file_canonical(vault):
    records.create_collection(vault, LAMPS_PATH, equipment_manifest(LAMPS, "Lamps"), why="file collection",
                              scaffold=True)
    return LAMPS


def dataset(vault):
    folder = vault / "Knowledge Base/Records/Meters"
    folder.mkdir(parents=True)
    (folder / "readings.json").write_text('[{"reading_id": "m-1", "value": 1}]', encoding="utf-8")
    (folder / "_collection.md").write_text(
        "---\ntype: collection\nexomem_id: 6a1f0c3e-52b4-4f0e-8d7a-2c9e1b5d4a70\ntitle: Meter readings\n"
        "semantic_profile: records\ncollection_version: 1\nschema_version: 1\nlifecycle: active\nstorage:\n"
        "  strategy: dataset\n  source: readings.json\n  format_version: 1\nitem_schema:\n  natural_key: [reading_id]\n"
        "  fields:\n    reading_id: {type: string, required: true}\n    value: {type: number, required: true}\n---\n",
        encoding="utf-8")
    return "Knowledge Base/Records/Meters/_collection.md"


def vault_bytes(vault: Path):
    return {path: path.read_bytes() for path in sorted(vault.rglob("*")) if path.is_file()}


@pytest.mark.parametrize("make", [file_canonical, dataset])
@pytest.mark.parametrize("arguments", [
    {"action": "append", "item": {"asset_tag": "L-17"}, "effective_at": "2026-03-07", "why": "try"},
    {"action": "update", "item_key": "44444444-4444-4444-8444-444444444444", "changes": {"asset_tag": "L-17"},
     "corrects": "00000000-0000-4000-8000-000000000000", "expected_container_hash": "0" * 64,
     "expected_item_version": "0" * 64, "why": "try"},
    {"action": "query", "timeline": {"field": "asset_tag"}},
])
def test_a_file_collection_refuses_a_timeline_argument_and_writes_nothing(tmp_path, make, arguments):
    """A file keeps no past versions, so a stated time there would be dropped while the reply claimed it."""
    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base/log.md").write_text("# Log\n")
    collection = make(tmp_path)
    before = vault_bytes(tmp_path)

    with pytest.raises(OpError) as refused:
        record_memory(tmp_path, collection=collection, **arguments)

    assert refused.value.code == "TIMELINE_UNAVAILABLE" and "effective_at" in refused.value.details["remediation"]
    assert vault_bytes(tmp_path) == before


def test_a_derived_collection_refuses_a_timeline_argument_and_writes_nothing(store):
    """A derived collection's rows change only through its imports, which state no effective time."""
    derived = "8c4f2a10-5b6d-4e7f-9a0b-1c2d3e4f5a6b"
    text = equipment_manifest(derived, "Readings").replace("strategy: markdown-items\n", "strategy: sqlite\n").replace(
        "lifecycle: active\n", "lifecycle: active\nview_mode: summary\nderived: true\n")
    store.create_collection("Knowledge Base/Records/Readings/_collection.md", text, why="declare", scaffold=False)
    before = stored(store)

    refused = refusal(store.append_record, derived, item={"asset_tag": "L-17"}, why="a hand edit",
                      effective_at="2026-03-07")
    with request_scope(owner_principal(surface="mcp")), preview_store(store.root, store.handle), \
            pytest.raises(OpError) as queried:
        record_memory(store.root, "query", collection=derived, timeline={"field": "location"})

    assert refused.code == queried.value.code == "TIMELINE_UNAVAILABLE"
    assert stored(store) == before
