"""Reading a Records row's history by natural key or by row id (OpenSpec record-value-timeline 2.1).

Each case names the failure only it catches. Store collections run on the real CollectionWriter; the
file-canonical case runs on a vault fixture. The collections and keys are invented.
"""

from __future__ import annotations

import pytest
from test_collection_store_summary import create as create_summary
from test_collection_store_writer import CID, KEY, manifest_text
from test_collection_store_writer import create as create_collection
from test_collection_store_writer import store as store  # noqa: F401
from test_episode_records_leaf import COLLECTION as READINGS
from test_episode_records_leaf import READING, _collection
from test_record_history import _append as append_reading
from test_record_history import _correct as correct_reading
from test_record_history import _history as reading_history
from test_record_history import _refusal as refusal
from test_record_history import _withhold

from exomem import commands
from exomem.cli_ops import OpError
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.record_memory import record_memory

LAMP = {"title": "Lamp"}
GUEST = RequestPrincipal("principal:" + "ab" * 32, surface="mcp", issuer_family="mcp-oauth:fixture")


@pytest.fixture(autouse=True)
def owner():
    with request_scope(owner_principal(surface="mcp")):
        yield


def read(store, action="history", who=None, **arguments):
    with request_scope(who or owner_principal(surface="mcp")), preview_store(store.root, store.handle):
        return record_memory(store.root, action, collection=CID, **arguments)


def history_error(store, who=None, **selector):
    with pytest.raises(OpError) as raised:
        read(store, who=who, **selector)
    return raised.value.code, str(raised.value)


def make_typed(store):
    for _ in range(16):
        if store.migrate_typed_encoding(CID) == "ready":
            return
    raise AssertionError("typed cutover did not finish")


def lamp_row(store, create=create_collection):
    """Create the collection, append the lamp and correct its count once, so its history is not empty."""
    create(store)
    key = store.append_record(CID, item={**LAMP, "count": 1}, why="capture")["item_key"]
    container, version = store.current_guards(CID, key)
    store.update_record(CID, item_key=key, changes={"count": 2}, expected_container_hash=container,
                        expected_item_version=version, why="recount")
    return key


@pytest.mark.parametrize("encoding", ["json-v1", "typed"])
def test_history_by_natural_key_reads_the_row_whatever_its_storage_encoding(store, encoding):
    """A lookup that reads one encoding's table only answers `no such row` for a row the other one holds."""
    key = lamp_row(store)
    if encoding == "typed":
        make_typed(store)

    by_key = read(store, natural_key=LAMP)

    assert by_key["retained"] == 1
    assert by_key == read(store, item_key=key)


def test_a_retracted_row_is_found_by_its_natural_key_though_retraction_clears_the_stored_key(store):
    """The key a retraction clears is the lookup column, so a lookup that only reads it loses the row's history."""
    key = lamp_row(store, create_summary)  # only a summary collection can retract a row
    container, version = store.current_guards(CID, key)
    store.retract_record(CID, item_key=key, expected_container_hash=container,
                         expected_item_version=version, why="entered in error")
    assert store.connection.execute("SELECT natural_key FROM item_rows WHERE item_key=?", (key,)).fetchone() == (None,)

    by_key = read(store, natural_key=LAMP)

    assert [revision["operation"] for revision in by_key["revisions"]] == ["retract", "update"]
    assert by_key == read(store, item_key=key)


def test_a_natural_key_a_row_moved_off_names_no_row(store):
    """The derived item key still belongs to a row that edited its key: it must not answer for the old key."""
    key = lamp_row(store)
    container, version = store.current_guards(CID, key)
    store.update_record(CID, item_key=key, changes={"title": "Lantern"}, expected_container_hash=container,
                        expected_item_version=version, why="rename")

    assert read(store, natural_key={"title": "Lantern"}) == read(store, item_key=key)
    assert history_error(store, natural_key=LAMP) == history_error(store, item_key="0" * 32)


def test_an_absent_key_and_a_withheld_natural_key_field_refuse_identically(store):
    """A refusal that tells a withheld key field from an absent key lets a caller probe a hidden value."""
    create_collection(store)
    store.revise_collection(
        CID,
        manifest_text=manifest_text().replace("title: {type: string, required: true}",
                                              "title: {type: string, required: true, classification: location}"),
        why="classify the title", **store.inspect_collection(CID)["lifecycle_guards"])
    store.append_record(CID, item={**LAMP, "count": 1}, why="capture")

    probes = (LAMP, {"title": "Absent"}, {"count": 1}, {})
    refused = {history_error(store, who=GUEST, natural_key=probe) for probe in probes}

    assert refused == {history_error(store, who=GUEST, item_key="0" * 32)}
    assert {code for code, _ in refused} == {"RECORD_NOT_FOUND"}
    # The owner reads the field, so a misshapen key is the owner's argument error: the guest's refusal is not a shared one.
    assert history_error(store, natural_key={"count": 1})[0] == "INVALID_RECORD_ARGUMENTS"


def test_a_row_the_caller_cannot_read_refuses_like_an_absent_key(store):
    """A natural key that resolves to a row the caller may not read must not answer differently from one that resolves to none."""
    create_collection(store)
    store.append_record(CID, item={**LAMP, "count": 1}, item_key=KEY, why="capture")
    _withhold(store.root, store.root / "Knowledge Base/Records/Work/Items" / f"{KEY}.md")
    outsider = RequestPrincipal(audience_id="external", surface="mcp")

    assert read(store, natural_key=LAMP)["retained"] == 0
    assert history_error(store, who=outsider, natural_key=LAMP) == history_error(
        store, who=outsider, natural_key={"title": "Absent"})


def test_a_stale_item_id_names_the_same_row_for_history(store):
    """History guards no write: an id from before an edit still names its row, where an edit refuses it."""
    create_collection(store)
    key = store.append_record(CID, item={**LAMP, "count": 1}, why="capture")["item_key"]
    stale_id = read(store, "query")["rows"][0]["id"]
    container, version = store.current_guards(CID, key)
    store.update_record(CID, item_key=key, changes={"count": 2}, expected_container_hash=container,
                        expected_item_version=version, why="recount")
    assert read(store, "query")["rows"][0]["id"] != stale_id

    assert read(store, item_id=stale_id) == read(store, item_key=key)
    assert read(store, item_id=stale_id)["retained"] == 1


def test_history_takes_exactly_one_selector_and_a_natural_key_of_the_declared_fields(store):
    """Two selectors that disagree pick a row by call order; a partial key picks one by what the caller left out."""
    key = lamp_row(store)
    row_id = read(store, "query")["rows"][0]["id"]

    refused = [
        history_error(store, **selectors)
        for selectors in (
            {},
            {"item_key": key, "natural_key": LAMP},
            {"item_id": row_id, "natural_key": LAMP},
            {"item_key": key, "item_id": row_id, "natural_key": LAMP},
            {"natural_key": {"title": "Lamp", "count": 1}},
            {"natural_key": {}},
            {"natural_key": {"title": None}},
        )
    ]

    assert {code for code, _ in refused} == {"INVALID_RECORD_ARGUMENTS"}
    assert "exactly one of" in refused[0][1] and "title" in refused[-1][1]


def test_history_by_natural_key_reads_a_file_canonical_collection(vault):
    """A file collection holds no store row, so its lookup must still resolve a key to the one row that holds it."""
    _collection(vault)
    key = append_reading(vault)
    correct_reading(vault, key, 14, "The probe read the wrong vat.")
    natural_key = {"read_on": READING["read_on"], "vat": READING["vat"]}

    by_key = commands.op_record_memory(vault, action="history", collection=READINGS, natural_key=natural_key)

    assert by_key["retained"] == 1
    assert by_key == reading_history(vault, key)
    absent = lambda: commands.op_record_memory(  # noqa: E731
        vault, action="history", collection=READINGS, natural_key={**natural_key, "vat": "south"})
    assert refusal(absent) == refusal(lambda: reading_history(vault, "0" * 32))
