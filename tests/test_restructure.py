"""`restructure` changes a collection's shape in one call, losslessly, and `undo` puts it back.

Cases drive the dispatcher that MCP, REST and the CLI share, on a real temporary vault of each storage mode. A
restructure commits only when applying its inverse to the result returns the prior manifest and every prior row.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing

import pytest
import yaml
from conftest import initialize_vault_state_offline
from record_fixtures import LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT
from test_automatic_collection import call as store_call
from test_collection_store_summary import summary_text
from test_collection_store_writer import CID, manifest_path, manifest_text
from test_collection_store_writer import store as store  # noqa: F401 - the fixture, by name
from test_single_purpose_tools_mcp import _call, _session
from test_single_purpose_tools_mcp import build as build  # noqa: F401 - the fixture, by name
from test_typed_history_generations import before_shape_history

from exomem import commands, restructure, vault
from exomem.cli_ops import OpError
from exomem.collection_store import chain, connection, history, schema, shape_history, typed_storage
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope

WHY = "restructure for the test"
SYSTEM_KEYS = {"collection_id", "record_id", "inferred", "ambiguous", "item_version", "id"}


class Work:
    """A two-row collection and the calls that drive it. `operate` runs a `change_records` action on its vault."""

    def __init__(self, operate, read_manifest_text, collection_id, *, seed=True):
        self.operate, self.read_manifest_text, self.collection_id = operate, read_manifest_text, collection_id
        for title, count in (("a", 3), ("b", 4)) if seed else ():
            self.append({"title": title, "count": count})

    def restructure(self, **arguments):
        return self.operate("restructure", collection=self.collection_id, why=WHY, **arguments)

    def refused(self, **arguments) -> dict:
        with pytest.raises(OpError) as error:
            self.restructure(**arguments)
        return error.value.as_public_dict()

    def append(self, item):
        return self.operate("append", collection=self.collection_id, item=item, why=WHY)

    def update(self, title, changes):
        queried = self.operate("query", collection=self.collection_id)
        row = next(row for row in queried["rows"] if row["title"] == title)
        return self.operate(
            "update", collection=self.collection_id, why=WHY, changes=changes, item_key=row["record_id"],
            expected_item_version=row["item_version"], expected_container_hash=queried["snapshot"],
        )

    def key(self, title):
        return next(row["record_id"] for row in self.operate("query", collection=self.collection_id)["rows"] if row["title"] == title)

    def rows(self) -> dict[str, dict]:
        queried = self.operate("query", collection=self.collection_id)
        return {row["title"]: {k: v for k, v in row.items() if k not in SYSTEM_KEYS} for row in queried["rows"]}

    def manifest(self) -> dict:
        front = yaml.safe_load(vault.parse_frontmatter(self.read_manifest_text(), strict=True)[2])
        front.pop("record_audit", None)  # a file-only audit head moves with every write, and is not the shape
        return front


def make_file_work(tmp_path, *, views: str = "", origin: str | None = None, automatic: bool = False) -> Work:
    """A file-only Records collection with two rows; `views` is text added to the manifest front.

    `automatic` creates it as an agent's own collection, under the automatic-creation bounds."""
    root = tmp_path / "file-vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Existing log\n")
    initialize_vault_state_offline(root, source="restructure fixture")
    text = manifest_text()
    created = commands.op_record_memory(
        root, "create", manifest_path=manifest_path(), why=WHY, origin=origin,
        manifest_text=text.replace("\n---\n", f"\n{views}---\n", 1) if views else text,
        **({"automatic": True} if automatic else {}),
    )
    return Work(
        lambda action, **arguments: commands.op_record_memory(root, action, **arguments),
        lambda: (root / manifest_path()).read_text(), created["collection_id"],
    )


def make_store_work(
    store, *, created=True, views: str = "", origin: str | None = None, collection_id=CID, automatic: bool = False
) -> Work:
    """The same collection on the collection store; `created` False drives the store's collection `collection_id`."""
    if created:
        text = manifest_text()
        collection_id = store_call(
            store, "record_memory", action="create", manifest_path=manifest_path(), why=WHY, scaffold=False,
            origin=origin, **({"automatic": True} if automatic else {}),
            manifest_text=text.replace("\n---\n", f"\n{views}---\n", 1) if views else text,
        )["collection_id"]

    def read_manifest_text():
        with store.read_snapshot():
            return store.connection.execute(
                "SELECT manifest_text FROM collection_manifests WHERE collection_id=? ORDER BY manifest_version DESC LIMIT 1",
                (collection_id,),
            ).fetchone()[0]

    return Work(
        lambda action, **arguments: store_call(store, "record_memory", action=action, **arguments),
        read_manifest_text, collection_id, seed=created,
    )


def make_encoded_store_work(store, monkeypatch, encoding) -> Work:
    """A summary collection on the store whose rows are kept in `encoding`."""
    migrate = typed_storage.migrate_batch

    def installed_encoding(conn, collection_id, fields, **kwargs):
        # A summary keeps JSON rows until its forward migration; a typed encoding is installed at create.
        if encoding == typed_storage.JSON_V1:
            return "ready"
        return migrate(conn, collection_id, fields, **{**kwargs, "encoding": encoding})

    with monkeypatch.context() as installation:
        installation.setattr(typed_storage, "migrate_batch", installed_encoding)
        store.create_collection(manifest_path(), summary_text(), why=WHY, scaffold=False)
    work = make_store_work(store, created=False)
    for title, count in (("a", 3), ("b", 4)):
        work.append({"title": title, "count": count})
    with store.read_snapshot():
        assert typed_storage.collection_encoding(store.connection, CID) == encoding
    return work


def stored(store, collection_id) -> dict[str, tuple[str, str]]:
    """Each current row's payload hash and stored values as canonical JSON: what an undo restores byte for byte."""
    with store.read_snapshot():
        hashes = dict(store.connection.execute(
            "SELECT item_key, payload_hash FROM items WHERE collection_id=?", (collection_id,)))
        return {key: (hashes[key], typed_storage.canonical_json(values))
                for key, values in typed_storage.collection_values(store.connection, collection_id)}


def verify_from_genesis(store, collection_id) -> None:
    """Verify the collection's chain and the store chain from the first transaction, with no cached proof."""
    with store.read_snapshot():
        history.verify(store.connection, collection_id)


@pytest.fixture(params=["file-only", "store"])
def mode(request):
    return request.param


@pytest.fixture
def work(mode, tmp_path, request):
    """A two-row collection of this storage mode."""
    return make_store_work(request.getfixturevalue("store")) if mode == "store" else make_file_work(tmp_path)


@pytest.fixture
def file_work(tmp_path):
    return make_file_work(tmp_path)


@pytest.mark.parametrize(
    "steps",
    [
        [{"step": "add_field", "field": "note", "type": "string"}],
        [{"step": "retire_field", "field": "count"}],
        [{"step": "retitle", "title": "Plants"}],
        [{"step": "retire"}],
    ],
    ids=lambda steps: steps[0]["step"],
)
def test_each_step_applies_and_its_undo_returns_the_manifest_and_every_row(work, steps):
    rows, manifest = work.rows(), work.manifest()

    applied = work.restructure(steps=steps)
    assert work.manifest() != manifest
    work.restructure(undo=applied["shape"]["id"])

    assert (work.rows(), work.manifest()) == (rows, manifest)


@pytest.mark.parametrize(
    "steps",
    [
        [{"step": "rename_field", "field": "count", "to": "total"}],
        [{"step": "retype_field", "field": "count", "type": "string"}],
    ],
    ids=lambda steps: steps[0]["step"],
)
def test_a_step_that_rewrites_rows_is_previewed_applied_and_undone(work, steps):
    rows, manifest = work.rows(), work.manifest()

    preview = work.restructure(steps=steps, preview=True)
    applied = work.restructure(steps=steps)
    assert preview["rows_changed"] == 2 and work.rows() != rows
    work.restructure(undo=applied["shape"]["id"])

    assert (work.rows(), work.manifest()) == (rows, manifest)


@pytest.mark.parametrize("encoding", ["json-v1", "typed-v1", "typed-v2"])
@pytest.mark.parametrize(
    ("steps", "rewritten"),
    [
        ([{"step": "rename_field", "field": "count", "to": "total"}], {"a": {"title": "a", "total": 3}}),
        ([{"step": "retype_field", "field": "count", "type": "string"}], {"a": {"title": "a", "count": "3"}}),
    ],
    ids=["rename_field", "retype_field"],
)
def test_a_store_rewrite_and_its_undo_restore_every_stored_value_and_the_chain_verifies(
    store, monkeypatch, encoding, steps, rewritten
):
    """Each encoding stores a rewritten row in its own layout; the undo must return the exact stored payload."""
    work = make_encoded_store_work(store, monkeypatch, encoding)
    before = stored(store, CID)

    applied = work.restructure(steps=steps)
    assert work.rows()["a"] == rewritten["a"]
    verify_from_genesis(store, CID)
    work.restructure(undo=applied["shape"]["id"])

    assert stored(store, CID) == before
    verify_from_genesis(store, CID)


def test_a_rewritten_row_that_does_not_read_back_as_planned_refuses_and_nothing_commits(store, monkeypatch):
    """The manifest revision and the row rewrite commit together, so a refused read-back leaves both as they were."""
    from exomem.collection_store import writer as writer_module

    work = make_store_work(store)
    rows, manifest, before, misread_key = work.rows(), work.manifest(), stored(store, CID), work.key("a")
    keyed = typed_storage.keyed_values

    def misread(conn, collection_id, keys):
        return [(key, {**values, "total": 99}) if key == misread_key else (key, values)
                for key, values in keyed(conn, collection_id, keys)]

    monkeypatch.setattr(writer_module.typed_storage, "keyed_values", misread)
    error = work.refused(steps=[{"step": "rename_field", "field": "count", "to": "total"}])
    monkeypatch.undo()

    assert error["code"] == "RESTRUCTURE_NOT_LOSSLESS" and error["rows"] == [misread_key]
    assert (work.rows(), work.manifest(), stored(store, CID)) == (rows, manifest, before)
    assert work.operate("inspect", collection=CID)["shape_history"]["total"] == 1  # the create entry alone
    verify_from_genesis(store, CID)


def test_a_restructure_reply_says_when_a_rewritten_row_view_waits_to_publish(store):
    """The row views were never published, so the rewrite of their rows cannot check or publish them at commit.

    The row writes' receipts say so, and the restructure's reply carries what they say.
    """
    work = make_store_work(store)

    applied = work.restructure(steps=[{"step": "rename_field", "field": "count", "to": "total"}])

    assert "projection_pending" in applied["warnings"]


def test_a_step_that_rewrites_more_rows_than_one_call_commits_is_refused(work, monkeypatch):
    monkeypatch.setattr(restructure, "MAX_REWRITTEN_ROWS", 1)
    rows, manifest = work.rows(), work.manifest()

    error = work.refused(steps=[{"step": "rename_field", "field": "count", "to": "total"}])

    assert error["code"] == "RESTRUCTURE_TOO_MANY_ROWS" and error["rows_total"] == 2 and error["remediation"]
    assert (work.rows(), work.manifest()) == (rows, manifest)


def test_a_restructure_of_a_collection_larger_than_one_call_reshapes_is_refused_before_it_changes_anything(work, monkeypatch):
    monkeypatch.setattr(restructure, "MAX_COLLECTION_ROWS", 1)
    rows, manifest = work.rows(), work.manifest()

    errors = [work.refused(steps=steps) for steps in (
        [{"step": "retitle", "title": "Other"}], [{"step": "rename_field", "field": "count", "to": "total"}])]

    for error in errors:
        assert error["code"] == "RESTRUCTURE_TOO_MANY_ROWS" and error["rows_total"] == 2 and error["maximum"] == 1
        assert "too large to reshape in one call" in error["message"] and "later change" in error["message"]
    assert (work.rows(), work.manifest()) == (rows, manifest)


def test_an_undo_of_a_collection_larger_than_one_call_reshapes_is_refused(work, monkeypatch):
    applied = work.restructure(steps=[{"step": "retitle", "title": "Other"}])
    manifest = work.manifest()
    monkeypatch.setattr(restructure, "MAX_COLLECTION_ROWS", 1)

    error = work.refused(undo=applied["shape"]["id"])

    assert error["code"] == "RESTRUCTURE_TOO_MANY_ROWS" and error["rows_total"] == 2
    assert work.manifest() == manifest


def test_a_declined_proposal_is_recorded_for_a_collection_larger_than_one_call_reshapes(work, monkeypatch):
    monkeypatch.setattr(restructure, "MAX_COLLECTION_ROWS", 1)

    declined = work.restructure(steps=[{"step": "retitle", "title": "Other"}], origin="declined")

    assert declined["shape"]["origin"] == "declined"


def test_a_store_refuses_a_collection_larger_than_one_call_reshapes_without_loading_a_row(store, monkeypatch):
    work = make_store_work(store)
    monkeypatch.setattr(restructure, "MAX_COLLECTION_ROWS", 1)
    monkeypatch.setattr(typed_storage, "collection_values", lambda *_: pytest.fail("a refused restructure loaded rows"))

    error = work.refused(steps=[{"step": "rename_field", "field": "count", "to": "total"}])

    assert error["code"] == "RESTRUCTURE_TOO_MANY_ROWS"


def test_a_retype_that_would_turn_a_stored_fraction_into_a_whole_number_is_refused(work):
    """2.0 and 2 are equal numbers but different stored values, so the undo could not return the row as it was."""
    work.restructure(steps=[{"step": "add_field", "field": "weight", "type": "number"}])
    work.update("a", {"weight": 2.0})
    work.update("b", {"weight": 7})
    rows, manifest = work.rows(), work.manifest()

    error = work.refused(steps=[{"step": "retype_field", "field": "weight", "type": "integer"}])

    assert error["code"] == "RESTRUCTURE_NOT_LOSSLESS" and error["rows"] == [{"row": work.key("a"), "value": 2.0}]
    assert (work.rows(), work.manifest()) == (rows, manifest)


def test_a_retried_store_restructure_returns_the_same_entry_and_commits_nothing_more(store):
    """A transport retry replays the stored receipt; the entry lives in its own table, and the reply still names it."""
    make_store_work(store)
    steps = [{"step": "rename_field", "field": "count", "to": "total"}]
    applied = store.restructure_collection(CID, why=WHY, steps=steps, request_id="retry-of-one-call")
    with store.read_snapshot():
        committed = store.connection.execute("SELECT COUNT(*) FROM txns").fetchone()[0]

    retried = store.restructure_collection(CID, why=WHY, steps=steps, request_id="retry-of-one-call")

    assert retried["shape"] == applied["shape"]
    with store.read_snapshot():
        assert store.connection.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == committed


@pytest.mark.parametrize(
    "tamper",
    ["DELETE FROM collection_shape_history",
     "UPDATE collection_shape_history SET entry_json=json_set(entry_json, '$.why', 'another reason')"],
    ids=["dropped", "altered"],
)
def test_a_dropped_or_altered_shape_entry_fails_verification(store, tmp_path, tamper):
    """The entry is chain-bound: its revise event carries the entry's digest."""
    work = make_store_work(store)
    work.restructure(steps=[{"step": "retitle", "title": "Plants"}])
    verify_from_genesis(store, CID)

    with closing(sqlite3.connect(tmp_path / "collections.sqlite")) as raw:
        for name in ("update", "delete"):
            raw.execute(f"DROP TRIGGER collection_shape_history_append_only_{name}")
        raw.execute(tamper)
        raw.commit()

    with pytest.raises(chain.StoreChainError, match="event hash differs"):
        verify_from_genesis(store, CID)


def _collections(store, count) -> list[str]:
    """`count` empty collections on the store, each with a title of its own."""
    ids = []
    for index in range(count):
        collection_id = str(uuid.uuid4())
        text = manifest_text().replace(CID, collection_id).replace("title: Work", f"title: Work {index}")
        store.create_collection(manifest_path().replace("Work", f"Work{index}"), text, why=WHY, scaffold=False)
        ids.append(collection_id)
    return ids


def test_the_newest_entries_and_the_daily_count_read_every_collection_in_one_statement(store):
    """An inventory names many collections, so a read per collection would grow with them."""
    ids = _collections(store, 4)
    for collection_id in ids:
        retitled = store_call(store, "record_memory", action="restructure", collection=collection_id, why=WHY,
                              steps=[{"step": "retitle", "title": f"Moved {collection_id[:8]}"}])
        store_call(store, "record_memory", action="restructure", collection=collection_id, why=WHY,
                   undo=retitled["shape"]["id"])
        store_call(store, "record_memory", action="restructure", collection=collection_id, why=WHY,
                   steps=[{"step": "add_field", "field": "note", "type": "string"}])
    for title in ("One", "Two", "Three", "Four"):
        store_call(store, "record_memory", action="restructure", collection=ids[0], why=WHY,
                   steps=[{"step": "retitle", "title": title}])

    def statements(read, wanted):
        executed = []
        with store.read_snapshot():
            store.connection.set_trace_callback(executed.append)
            try:
                result = read(store.connection, wanted)
            finally:
                store.connection.set_trace_callback(None)
        return result, len(executed)

    newest = lambda conn, wanted: shape_history.newest(conn, wanted, limit=5)  # noqa: E731
    counted = lambda conn, wanted: shape_history.counted_since(  # noqa: E731
        conn, wanted, since="2000-01-01T00:00:00Z", origin="user_choice")
    (few, few_reads), (many, many_reads) = statements(newest, ids[:1]), statements(newest, ids)
    (few_counts, few_count_reads), (many_counts, many_count_reads) = statements(counted, ids[:1]), statements(counted, ids)

    assert few_reads == many_reads == 1 and few_count_reads == many_count_reads == 1
    assert few == {ids[0]: many[ids[0]]}
    assert many[ids[0]]["total"] == 8 and [entry["steps"][0].get("title") for entry in many[ids[0]]["entries"]] == [
        "Four", "Three", "Two", "One", None]
    assert {cid: found["total"] for cid, found in many.items()} == {ids[0]: 8, **{cid: 4 for cid in ids[1:]}}
    # Each collection's create, undo and field addition are not counted; its retitles are.
    assert few_counts == {ids[0]: 5} and many_counts == {ids[0]: 5, **{cid: 1 for cid in ids[1:]}}


HISTORY_TABLES = ("txns", "audit_effects", "item_versions", "version_identity", "collection_manifests")


def test_a_schema_19_store_upgrades_without_losing_history_and_then_restructures(tmp_path, monkeypatch):
    """The migration adds the shape-history table to a store that already holds rows and history."""
    root, path = tmp_path / "vault", tmp_path / "collections.sqlite"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Existing log\n")
    with monkeypatch.context() as earlier:
        earlier.setattr(schema, "SCHEMA_VERSION", 19)
        before_shape_history(earlier)
        with connection.open_writer(path, lease_check=lambda: True) as handle, library_scope():
            make_store_work(CollectionWriter(root, handle)).update("a", {"count": 5})
    with closing(sqlite3.connect(path)) as raw:
        assert schema.schema_version(raw) == 19
        assert raw.execute("SELECT 1 FROM sqlite_master WHERE name='collection_shape_history'").fetchone() is None
        before = {name: raw.execute(f"SELECT * FROM {name} ORDER BY 1, 2").fetchall() for name in HISTORY_TABLES}  # noqa: S608

    with connection.open_writer(path, lease_check=lambda: True) as handle, library_scope():
        conn = handle.connection
        assert schema.schema_version(conn) == schema.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM collection_shape_history").fetchone() == (0,)
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {name: conn.execute(f"SELECT * FROM {name} ORDER BY 1, 2").fetchall() for name in HISTORY_TABLES} == before  # noqa: S608
        assert "collections-store-schema-21" in schema.compatibility_ids()
        store = CollectionWriter(root, handle)
        verify_from_genesis(store, CID)
        work = make_store_work(store, created=False)
        rows = work.rows()

        applied = work.restructure(steps=[{"step": "rename_field", "field": "count", "to": "total"}])
        work.restructure(undo=applied["shape"]["id"])

        assert work.rows() == rows and work.operate("inspect", collection=CID)["shape_history"]["total"] == 2
        verify_from_genesis(store, CID)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"), handle.transaction() as tx:
            tx.execute("DELETE FROM collection_shape_history")


def test_restoring_a_retired_field_undoes_its_retirement(work):
    rows, manifest = work.rows(), work.manifest()
    work.restructure(steps=[{"step": "retire_field", "field": "count"}])

    work.restructure(steps=[{"step": "restore_field", "field": "count"}])

    assert (work.rows(), work.manifest()) == (rows, manifest)


def test_a_retype_that_loses_a_value_is_refused_naming_the_rows_and_changes_nothing(work):
    work.restructure(steps=[{"step": "add_field", "field": "note", "type": "string"}])
    work.update("a", {"note": "x7"})
    rows, manifest = work.rows(), work.manifest()

    error = work.refused(steps=[{"step": "retype_field", "field": "note", "type": "integer"}])

    assert error["code"] == "RESTRUCTURE_NOT_LOSSLESS"
    assert error["rows"] == [{"row": work.key("a"), "value": "x7"}]
    assert (work.rows(), work.manifest()) == (rows, manifest)


def test_an_undo_that_a_later_row_makes_lossy_is_refused_naming_the_row_and_changes_nothing(work):
    applied = work.restructure(steps=[{"step": "retype_field", "field": "count", "type": "string"}])
    work.append({"title": "c", "count": "three"})
    rows, manifest = work.rows(), work.manifest()

    error = work.refused(undo=applied["shape"]["id"])

    assert error["code"] == "RESTRUCTURE_NOT_LOSSLESS"
    assert error["rows"] == [{"row": work.key("c"), "value": "three"}]
    assert (work.rows(), work.manifest()) == (rows, manifest)


def test_a_step_that_breaks_a_saved_view_is_refused_naming_the_view(mode, tmp_path, request):
    views = "views:\n  by_count: {sort: [count, desc]}\n"
    work = (make_store_work(request.getfixturevalue("store"), views=views) if mode == "store"
            else make_file_work(tmp_path, views=views))
    rows, manifest = work.rows(), work.manifest()

    error = work.refused(steps=[{"step": "rename_field", "field": "count", "to": "total"}])

    assert error["code"] == "RESTRUCTURE_DEPENDENT_BROKEN"
    assert "by_count" in error["message"]
    assert (work.rows(), work.manifest()) == (rows, manifest)


def test_a_step_that_breaks_a_rollup_is_refused_in_its_preview_naming_the_rollup(store):
    text = manifest_text().replace("    count: {type: integer}\n", "    count: {type: integer}\n    day: {type: date}\n")
    rollup = "rollups:\n  daily:\n    bucket: day\n    timestamp: day\n    values:\n      count: [sum]\n"
    store.create_collection(manifest_path(), text.replace("item_schema:\n", f"{rollup}item_schema:\n"),
                            why=WHY, scaffold=False)
    work = make_store_work(store, created=False)

    error = work.refused(steps=[{"step": "rename_field", "field": "count", "to": "total"}], preview=True)

    assert error["code"] == "RESTRUCTURE_DEPENDENT_BROKEN"
    assert "rollups.daily.values.count" in error["message"]


def test_an_entry_is_undone_once(work):
    applied = work.restructure(steps=[{"step": "retitle", "title": "Plants"}])
    work.restructure(undo=applied["shape"]["id"])

    error = work.refused(undo=applied["shape"]["id"])

    assert error["code"] == "RESTRUCTURE_UNDO_REFUSED"


def test_a_step_on_a_natural_key_field_is_refused_because_it_would_move_every_row(work):
    error = work.refused(steps=[{"step": "rename_field", "field": "title", "to": "name"}])

    assert error["code"] == "RESTRUCTURE_STEP_REFUSED" and error["field"] == "title"


def test_a_preview_returns_the_result_and_writes_nothing(work):
    rows, manifest = work.rows(), work.manifest()

    preview = work.restructure(steps=[{"step": "retitle", "title": "Plants"}], preview=True)

    assert preview["inverse"] == [{"step": "retitle", "title": "Work"}] and preview["rows_changed"] == 0
    assert (work.rows(), work.manifest()) == (rows, manifest)


def test_a_declined_proposal_is_recorded_and_changes_nothing(work):
    rows, manifest = work.rows(), work.manifest()

    declined = work.restructure(steps=[{"step": "retire"}], origin="declined")

    assert declined["shape"]["origin"] == "declined" and declined["shape"]["steps"] == [{"step": "retire"}]
    assert (work.rows(), work.manifest()) == (rows, manifest)
    assert work.refused(undo=declined["shape"]["id"])["code"] == "RESTRUCTURE_UNDO_REFUSED"
    # The undo always refuses, so the inspected entry offers no call that would.
    newest = work.operate("inspect", collection=work.collection_id)["shape_history"]["entries"][0]
    assert newest["id"] == declined["shape"]["id"] and "undo_call" not in newest


def test_a_caller_cannot_label_its_own_restructure_automatic(work):
    assert work.refused(steps=[{"step": "retire"}], origin="automatic")["code"] == "INVALID_RESTRUCTURE"


def test_inspect_pages_the_whole_shape_history_newest_first_with_the_call_that_undoes_each_entry(work):
    first = work.restructure(steps=[{"step": "retitle", "title": "Plants"}])
    second = work.restructure(steps=[{"step": "add_field", "field": "note", "type": "string"}])
    work.restructure(undo=second["shape"]["id"])

    page = work.operate("inspect", collection=work.collection_id, limit=2)["shape_history"]
    rest = work.operate("inspect", collection=work.collection_id, limit=2, continuation=page["continuation"])["shape_history"]

    assert [entry["steps"][0]["step"] for entry in page["entries"]] == ["remove_field", "add_field"]
    assert page["entries"][0]["undoes"] == second["shape"]["id"] and page["total"] == 4
    # The collection's create is the oldest entry; it has no steps to undo.
    assert [entry["id"] for entry in rest["entries"]][0] == first["shape"]["id"] and len(rest["entries"]) == 2
    assert rest["entries"][1]["steps"] == [] and "continuation" not in rest
    assert rest["entries"][0]["undo_call"] == {"collection": work.collection_id, "undo": first["shape"]["id"]}
    # An undo of the creation always refuses, so its entry offers no call.
    assert "undo_call" not in rest["entries"][1]


def test_a_row_that_adds_a_field_with_new_fields_logs_an_automatic_entry_that_undoes_it(work):
    work.operate(
        "append", collection=work.collection_id, item={"title": "c", "count": 5, "note": "x"}, why=WHY, new_fields=["note"],
    )

    entry, creation = work.operate("inspect", collection=work.collection_id)["shape_history"]["entries"]

    assert creation["steps"] == [] and entry["origin"] == "automatic" and entry["steps"] == [{"step": "add_field", "field": "note", "type": "string"}]
    assert entry["inverse"] == [{"step": "remove_field", "field": "note"}]


def test_a_new_collection_inspects_with_its_creation_as_the_first_shape_history_entry(work):
    history = work.operate("inspect", collection=work.collection_id)["shape_history"]

    (entry,) = history["entries"]
    assert history["total"] == 1 and entry["origin"] == "user_choice" and entry["steps"] == [] and entry["inverse"] == []


def test_a_planning_collection_restructures_and_undoes_through_change_plans(tmp_path):
    from test_planning_mutation import _manifest

    from exomem.plan_memory import plan_memory

    root = tmp_path / "planning-vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Existing log\n")
    initialize_vault_state_offline(root, source="restructure planning fixture")
    collection = "Knowledge Base/Planning/Work/_collection.md"
    plan_memory(root, "create", manifest_path=collection, manifest_text=_manifest(), why=WHY)
    before = yaml.safe_load(vault.parse_frontmatter((root / collection).read_text(), strict=True)[2])
    before.pop("plan_audit", None)  # an audit head moves with every write, and is not the shape

    applied = plan_memory(
        root, "restructure", collection=collection, why=WHY, steps=[{"step": "retitle", "title": "Roadmap"}]
    )
    plan_memory(root, "restructure", collection=collection, why=WHY, undo=applied["shape"]["id"])

    after = yaml.safe_load(vault.parse_frontmatter((root / collection).read_text(), strict=True)[2])
    after.pop("plan_audit", None)
    assert applied["shape"]["origin"] == "user_choice" and after == before
    assert plan_memory(root, "inspect", collection=collection)["shape_history"]["total"] == 3  # create, restructure, undo


def test_the_public_door_returns_a_receipt_with_the_entry_that_undoes_the_restructure(build, vault):
    arguments = {"collection": LEDGER_COLLECTION_PATH, "why": WHY}

    async def scenario(client):
        await _call(client, "manage_memory", {"operation": "records.create", "arguments": {
            "manifest_path": LEDGER_COLLECTION_PATH, "manifest_text": LEDGER_MANIFEST_TEXT, "why": WHY}})
        steps = [{"step": "add_field", "field": "note", "type": "string"}]
        applied = await _call(client, "manage_memory", {"operation": "records.restructure", "arguments": {**arguments, "steps": steps}})
        undone = await _call(client, "manage_memory", {"operation": "records.restructure", "arguments": {
            **arguments, "undo": applied["shape"]["id"]}})
        return applied, undone

    applied, undone = _session(build(), scenario)

    assert applied["outcome"] == "committed" and applied["shape"]["steps"][0]["field"] == "note"
    assert undone["outcome"] == "committed" and undone["shape"]["undoes"] == applied["shape"]["id"]
