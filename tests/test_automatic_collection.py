"""An agent-initiated Records collection creation is bounded by the server.

`change_records` `create` takes `automatic: true`. The flag is honoured only inside the bounds of
`exomem.automatic_collection`; a refusal creates nothing and a create without the flag is unchanged.
Cases drive the dispatcher that MCP, REST and the CLI share, on a real temporary store.
"""

from __future__ import annotations

import datetime as dt
import json
import os

import pytest
from conftest import initialize_vault_state_offline
from test_collection_rollups import DAILY, FIELDS
from test_collection_rollups import manifest as rollup_manifest
from test_collection_store_s1_gate import A_PATH
from test_collection_store_service import _retitle
from test_collection_store_service import call as served_call
from test_collection_store_service import service as service  # noqa: F401 - the fixture, by name
from test_collection_store_writer import CID, manifest_path, manifest_text
from test_collection_store_writer import store as store  # noqa: F401 - the fixture, by name
from test_single_purpose_tools_mcp import _call, _session
from test_single_purpose_tools_mcp import build as build  # noqa: F401 - the fixture, by name

from exomem import automatic_collection, commands, envelope, prominence
from exomem.cli_ops import OpError
from exomem.collection_store import runtime
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import owner_principal, request_scope
from exomem.structured_collections import CollectionError
from exomem.writer_lease import invoke_command

OWNER = owner_principal(surface="mcp")
WHY = "capture recurring work"


@pytest.fixture(autouse=True)
def silent_capture(tmp_path, monkeypatch):
    """The owner's setting at balanced prominence: proactive_capture is silent, on a config of its own."""
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.delenv("EXOMEM_PROMINENCE", raising=False)
    monkeypatch.delenv("EXOMEM_SURFACE", raising=False)
    prominence.write_prominence("balanced")
    assert envelope.active()["proactive_capture"] == "silent"
    with request_scope(OWNER):
        yield


def records_manifest(title: str, index: int = 0, *, fields: int = 2, natural_key: str = "title") -> str:
    """The writer fixture's Records manifest under its own title and id, with `fields` string fields."""
    body = "".join(f"    {name}: {{type: string}}\n" for name in ["title", *(f"f{n}" for n in range(1, fields))])
    text = manifest_text().replace(
        "    title: {type: string, required: true}\n    count: {type: integer}\n", body
    )
    return text.replace(CID, f"{CID[:-2]}{index:02x}").replace("title: Work", f"title: {title}").replace(
        "natural_key: [title]", f"natural_key: [{natural_key}]"
    )


def call(store, tool_name, /, **arguments):
    command = next(command for command in commands.COMMANDS if command.name == tool_name)
    with preview_store(store.root, store.handle):
        return invoke_command(command, store.root, **arguments)


def create(store, title, text, **flags):
    return call(
        store, "record_memory", action="create", manifest_path=manifest_path().replace("Work", title),
        manifest_text=text, why=WHY, scaffold=False, **flags,
    )


def refused(store, title, text) -> dict:
    """The error object that surfaces send for an automatic create the server refuses."""
    with pytest.raises(OpError) as error:
        create(store, title, text, automatic=True)
    return error.value.as_public_dict()


def inventory(store) -> dict[str, dict]:
    return {row["title"]: row for row in call(store, "record_memory", action="inspect")["collections"]}


def stored_collections(store) -> int:
    with store.read_snapshot():
        return store.connection.execute("SELECT COUNT(*) FROM collections").fetchone()[0]


def test_a_minimal_automatic_creation_commits_and_the_inventory_reports_it(store):
    receipt = create(store, "Ledger", records_manifest("Ledger"), automatic=True)
    # A later create without the flag runs on the writer: the preview-bound dispatcher serves only a store's first create.
    plain = store.create_collection(
        manifest_path().replace("Work", "Notebook"), records_manifest("Notebook", 1), why=WHY, scaffold=False
    )

    listed = inventory(store)
    assert receipt["automatic"] is True and "automatic" not in plain
    assert listed["Ledger"]["created_automatically"] is True
    assert listed["Notebook"]["created_automatically"] is False


def test_the_inventory_reads_the_create_records_once_however_many_store_collections_it_lists(store):
    def create_record_reads() -> int:
        statements: list[str] = []
        store.connection.set_trace_callback(statements.append)
        try:
            inventory(store)
        finally:
            store.connection.set_trace_callback(None)
        return sum("$.automatic" in statement for statement in statements)

    def make(title, index, **flags):
        store.create_collection(
            manifest_path().replace("Work", title), records_manifest(title, index), why=WHY, scaffold=False, **flags
        )

    make("Ledger", 0, automatic=True)
    one_collection = create_record_reads()
    make("Notebook", 1)
    make("Roster", 2, automatic=True)
    make("Journal", 3)

    listed = inventory(store)
    assert create_record_reads() == one_collection == 1
    assert {title: row["created_automatically"] for title, row in listed.items()} == {
        "Ledger": True, "Notebook": False, "Roster": True, "Journal": False,
    }


@pytest.mark.parametrize("level", ["advisory", "off"])
def test_an_automatic_creation_needs_silent_capture(store, level):
    envelope.set_disposition("proactive_capture", level)

    error = refused(store, "Ledger", records_manifest("Ledger"))

    assert error["code"] == "AUTOMATIC_CREATION_REFUSED"
    assert error["bound"] == "proactive_capture" and error["disposition"] == level
    # Under advisory the agent proposes and creates after the user confirms; under off it creates only on the user's request.
    way = error["message"].split("; ")[-1]
    assert way == (
        "create it without `automatic` only if the user asked for it" if level == "off"
        else "propose the collection to the user, and create it without `automatic` after they confirm"
    )
    assert stored_collections(store) == 0
    # What the owner asked for stays served: the same manifest, without the flag, still creates.
    assert create(store, "Ledger", records_manifest("Ledger"))["operation"] == "create"


def test_a_schema_over_the_field_bound_is_refused_and_creates_without_the_flag(store):
    over = records_manifest("Ledger", fields=automatic_collection.MAX_FIELDS + 1)

    error = refused(store, "Ledger", over)

    assert error["bound"] == "fields"
    assert error["fields"] == automatic_collection.MAX_FIELDS + 1
    assert stored_collections(store) == 0
    assert create(store, "Ledger", over)["operation"] == "create"


def test_a_schema_at_the_field_bound_is_accepted(store):
    at_bound = records_manifest("Ledger", fields=automatic_collection.MAX_FIELDS)

    assert create(store, "Ledger", at_bound, automatic=True)["automatic"] is True


VIEW = "views:\n  recent:\n    query:\n      columns: [title]\n"


@pytest.mark.parametrize(
    ("extra", "declared"),
    [
        pytest.param(VIEW, "views", id="saved view"),
        pytest.param("governance:\n  note: kept\n", "governance", id="governance"),
        pytest.param("templates: []\n", "templates", id="templates"),
    ],
)
def test_a_manifest_key_that_the_minimal_manifest_does_not_emit_is_refused(store, extra, declared):
    error = refused(store, "Ledger", records_manifest("Ledger").replace("\n---\n", "\n" + extra + "---\n"))

    assert error["bound"] == "authored_features" and error["declared"] == [declared]
    assert stored_collections(store) == 0


def test_an_item_schema_key_that_the_minimal_manifest_does_not_emit_is_refused(store):
    error = refused(store, "Ledger", records_manifest("Ledger").replace("  fields:\n", "  dependencies: []\n  fields:\n"))

    assert error["bound"] == "authored_features" and error["declared"] == ["item_schema.dependencies"]


def test_a_manifest_that_declares_a_rollup_is_refused(store):
    with_rollup = rollup_manifest(rollups=DAILY, fields=FIELDS, summary=False, title="Ledger")

    error = refused(store, "Ledger", with_rollup)

    assert error["bound"] == "authored_features" and error["declared"] == ["rollups"]


def nested_manifest(properties: int) -> str:
    """`Ledger` with three item fields, one of them an object that declares `properties` fields of its own."""
    nested = "".join(f"        p{n}: {{type: string}}\n" for n in range(properties))
    return records_manifest("Ledger").replace(
        "    f1: {type: string}\n", "    f1: {type: string}\n    meta:\n      type: object\n      properties:\n" + nested
    )


def test_the_properties_of_an_object_field_count_toward_the_field_bound(store):
    error = refused(store, "Ledger", nested_manifest(automatic_collection.MAX_FIELDS - 2))

    assert error["bound"] == "fields" and error["fields"] == automatic_collection.MAX_FIELDS + 1
    assert create(store, "Ledger", nested_manifest(automatic_collection.MAX_FIELDS - 3), automatic=True)["automatic"] is True


def test_a_collection_that_is_not_records_is_refused(store):
    planning = manifest_text("planning").replace("title: Work", "title: Ledger")

    with pytest.raises(OpError) as error:
        call(store, "record_memory", action="create", manifest_path=manifest_path("planning"), manifest_text=planning,
             why=WHY, scaffold=False, automatic=True)

    assert error.value.as_public_dict()["bound"] == "profile"


def test_the_fourth_automatic_creation_in_a_day_is_refused_and_a_plain_create_is_not(store):
    def make(title, index, **flags):
        return store.create_collection(
            manifest_path().replace("Work", title), records_manifest(title, index), why=WHY, scaffold=False, **flags
        )

    for index, title in enumerate(["Ledger", "Notebook", "Roster"][: automatic_collection.MAX_PER_WINDOW]):
        make(title, index, automatic=True)

    with pytest.raises(CollectionError) as error:
        make("Journal", 3, automatic=True)

    assert error.value.code == "AUTOMATIC_CREATION_REFUSED"
    assert error.value.details["bound"] == "daily" and error.value.details["created"] == 3
    assert make("Journal", 3)["operation"] == "create"


def test_replaying_the_last_allowed_creation_returns_its_receipt_instead_of_the_daily_refusal(store):
    for index, title in enumerate(["Ledger", "Notebook"]):
        store.create_collection(manifest_path().replace("Work", title), records_manifest(title, index), why=WHY,
                                scaffold=False, automatic=True)
    last = records_manifest("Roster", 2)
    first = store.create_collection(manifest_path().replace("Work", "Roster"), last, why=WHY, scaffold=False,
                                    request_id="req-roster", automatic=True)

    again = store.create_collection(manifest_path().replace("Work", "Roster"), last, why=WHY, scaffold=False,
                                    request_id="req-roster", automatic=True)

    assert again["collection_id"] == first["collection_id"] and again["automatic"] is True


@pytest.fixture
def file_vault(tmp_path):
    """A vault that has enrolled no collection store: the state of every new install."""
    vault = tmp_path / "file-vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    (vault / "Knowledge Base/log.md").write_text("# Existing log\n")
    initialize_vault_state_offline(vault, source="automatic collection fixture")
    return vault


def file_create(vault, title, index=0, **flags):
    return commands.op_record_memory(
        vault, "create", manifest_path=manifest_path().replace("Work", title),
        manifest_text=records_manifest(title, index), why=WHY, **flags,
    )


def test_a_file_only_vault_creates_automatically_and_the_inventory_reports_it(file_vault):
    receipt = file_create(file_vault, "Ledger", automatic=True)
    file_create(file_vault, "Notebook", 1)
    # The create's log entry carries an extra line; the audit reader that every later write runs must still accept it.
    row = commands.op_record_memory(
        file_vault, "append", collection=receipt["collection_id"], item={"title": "first"}, why=WHY
    )

    listed = {row["title"]: row for row in commands.op_record_memory(file_vault, "inspect")["collections"]}

    assert receipt["operation"] == "create" and receipt["automatic"] is True and row["operation"] == "append"
    assert (file_vault / manifest_path().replace("Work", "Ledger")).exists()
    assert listed["Ledger"]["created_automatically"] is True
    assert listed["Notebook"]["created_automatically"] is False


def test_the_fourth_automatic_creation_in_a_day_is_refused_on_a_file_only_vault(file_vault):
    for index, title in enumerate(["Ledger", "Notebook", "Roster"][: automatic_collection.MAX_PER_WINDOW]):
        file_create(file_vault, title, index, automatic=True)

    with pytest.raises(OpError) as error:
        file_create(file_vault, "Journal", 3, automatic=True)

    public = error.value.as_public_dict()
    assert public["code"] == "AUTOMATIC_CREATION_REFUSED" and public["bound"] == "daily" and public["created"] == 3
    assert not (file_vault / manifest_path().replace("Work", "Journal")).exists()
    assert file_create(file_vault, "Journal", 3)["operation"] == "create"


def test_a_file_create_older_than_a_day_leaves_room(file_vault):
    log = file_vault / "Knowledge Base/log.md"
    two_days_ago = (dt.datetime.now(dt.UTC) - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = automatic_collection.CREATE_LINE_PREFIX + json.dumps(
        {"collection_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "at": two_days_ago}
    )
    log.write_text(log.read_text() + "\n".join([stale] * automatic_collection.MAX_PER_WINDOW) + "\n")

    assert file_create(file_vault, "Ledger", automatic=True)["automatic"] is True


def test_a_file_create_rotated_into_a_recent_archive_still_counts(file_vault):
    archive = file_vault / "Knowledge Base/_archive/logs/log-recent.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("\n".join(
        automatic_collection.create_line(f"cccccccc-cccc-4ccc-8ccc-{n:012d}")
        for n in range(automatic_collection.MAX_PER_WINDOW)
    ) + "\n")

    with pytest.raises(OpError) as error:
        file_create(file_vault, "Ledger", automatic=True)

    assert error.value.as_public_dict()["bound"] == "daily"


def test_an_archive_rotated_before_the_window_is_not_read(file_vault):
    """Rotation writes an archive after every entry it holds, so an old one cannot hold a create from the last day."""
    archive = file_vault / "Knowledge Base/_archive/logs/log-old.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("\n".join(
        automatic_collection.create_line(f"cccccccc-cccc-4ccc-8ccc-{n:012d}")
        for n in range(automatic_collection.MAX_PER_WINDOW)
    ) + "\n")
    two_days_ago = (dt.datetime.now(dt.UTC) - dt.timedelta(days=2)).timestamp()
    os.utime(archive, (two_days_ago, two_days_ago))

    assert file_create(file_vault, "Ledger", automatic=True)["automatic"] is True


def test_an_inventory_that_cannot_read_all_history_reports_unknown_not_false(file_vault):
    file_create(file_vault, "Ledger", automatic=True)
    file_create(file_vault, "Notebook", 1)
    archives = file_vault / "Knowledge Base/_archive/logs"
    archives.mkdir(parents=True)
    for n in range(automatic_collection.INVENTORY_ARCHIVES + 1):
        (archives / f"log-{n}.md").write_text("# rotated\n")

    listed = {row["title"]: row for row in commands.op_record_memory(file_vault, "inspect")["collections"]}

    assert listed["Ledger"]["created_automatically"] is True
    assert listed["Notebook"]["created_automatically"] is None


def test_a_create_line_that_cannot_be_read_refuses_instead_of_lifting_the_bound(file_vault):
    log = file_vault / "Knowledge Base/log.md"
    log.write_text(log.read_text() + automatic_collection.CREATE_LINE_PREFIX + "{not json\n")

    with pytest.raises(OpError) as error:
        file_create(file_vault, "Ledger", automatic=True)

    assert error.value.as_public_dict()["bound"] == "daily"
    assert file_create(file_vault, "Ledger")["operation"] == "create"


def test_a_store_vault_counts_the_file_creates_it_had_before_its_store(store):
    log = store.root / "Knowledge Base/log.md"
    earlier = [automatic_collection.create_line(f"cccccccc-cccc-4ccc-8ccc-{n:012d}") for n in range(2)]
    log.write_text(log.read_text() + "\n".join(earlier) + "\n")
    store.create_collection(manifest_path().replace("Work", "Ledger"), records_manifest("Ledger"), why=WHY,
                            scaffold=False, automatic=True)

    with pytest.raises(CollectionError) as error:
        store.create_collection(manifest_path().replace("Work", "Notebook"), records_manifest("Notebook", 1), why=WHY,
                                scaffold=False, automatic=True)

    assert error.value.details["bound"] == "daily" and error.value.details["created"] == 3


# --- add_record `new_collection`: one call creates the collection and adds the row ---------------------

# One value of each JSON shape the draft types a field from.
ROW = {"title": "first", "count": 2, "seen_on": "2026-11-20", "score": 4.5, "done": False}
NEW = {"natural_key": ["title"]}


def add_record(build, **arguments):
    """One `add_record` call over MCP, which is what an agent's client sends."""
    arguments = {"collection": "Ledger", "item": ROW, "why": WHY, "new_collection": NEW, **arguments}

    async def scenario(client):
        return await _call(client, "add_record", arguments, key=arguments.pop("key", None))

    return _session(build(), scenario)


def test_one_add_record_call_creates_the_collection_and_adds_the_row_on_a_file_only_vault(build, vault):
    receipt = add_record(build)

    listed = {row["title"]: row for row in commands.op_record_memory(vault, "inspect")["collections"]}
    rows = commands.op_record_memory(vault, "query", collection="Ledger")["rows"]
    assert receipt["operation"] == "append" and receipt["automatic"] is True
    assert receipt["created_collection"]["natural_key"] == ["title"]
    assert receipt["created_collection"]["fields"] == list(ROW)
    assert listed["Ledger"]["created_automatically"] is True
    assert [(row["title"], row["count"], row["seen_on"], row["score"], row["done"]) for row in rows] == [
        ("first", 2, "2026-11-20", 4.5, False)
    ]


# A store vault is served: its collections are written only on the service's store thread, which is where `route`
# sends a request. These tests go through that route (`served_call`), as an MCP client's call does; `call(store, ...)`
# binds a writer to the test thread and so never reaches `route`.


def served_ledger(service, **arguments):
    """One `append` that names no collection and carries `new_collection`, on a vault served by its store thread."""
    return served_call(
        service.root, "record_memory", action="append", collection="Ledger", item=ROW, why=WHY, new_collection=NEW,
        **arguments,
    )


def served_inventory(service) -> dict[str, dict]:
    return {row["title"]: row for row in served_call(service.root, "record_memory", action="inspect")["collections"]}


def test_one_call_creates_the_collection_and_appends_the_row_on_a_served_store_vault(service):
    receipt = served_ledger(service)

    rows = served_call(service.root, "record_memory", action="query", collection="Ledger")["rows"]
    assert receipt["operation"] == "append" and receipt["automatic"] is True
    assert served_inventory(service)["Ledger"]["created_automatically"] is True
    assert [(row["title"], row["count"], row["seen_on"], row["score"], row["done"]) for row in rows] == [
        ("first", 2, "2026-11-20", 4.5, False)
    ]


def test_a_refused_creation_on_a_served_store_vault_creates_nothing_and_returns_the_collection_it_would_have_created(
    service,
):
    envelope.set_disposition("proactive_capture", "advisory")

    with pytest.raises(OpError) as error:
        served_ledger(service)

    assert error.value.code == "AUTOMATIC_CREATION_REFUSED"
    assert "title: Ledger" in error.value.details["retry_call"]["manifest_text"]
    assert "Ledger" not in served_inventory(service)


def test_a_refused_automatic_creation_creates_nothing_and_returns_the_collection_it_would_have_created(build, vault):
    envelope.set_disposition("proactive_capture", "advisory")

    refusal = add_record(build)["error"]

    assert refusal["code"] == "AUTOMATIC_CREATION_REFUSED"
    assert commands.op_record_memory(vault, "inspect")["collections"] == []
    # The retry is the confirmed create, from the manifest this call would have created; the row then fits it.
    retry = refusal["retry"]
    assert retry["tool"] == "manage_memory" and "title: Ledger" in retry["arguments"]["arguments"]["manifest_text"]

    async def confirmed(client):
        created = await _call(client, retry["tool"], retry["arguments"])
        return created, await _call(client, "add_record", {"collection": "Ledger", "item": ROW, "why": WHY})

    created, appended = _session(build(), confirmed)
    assert "automatic" not in created and appended["operation"] == "append"
    assert [row["title"] for row in commands.op_record_memory(vault, "query", collection="Ledger")["rows"]] == ["first"]


def test_a_row_without_its_natural_key_is_refused_before_anything_is_created(build, vault):
    refusal = add_record(build, item={"count": 2})

    assert refusal["error"]["code"] == "SCHEMA_REQUIRED_FIELD" and "title" in refusal["error"]["message"]
    assert commands.op_record_memory(vault, "inspect")["collections"] == []


def test_a_title_with_a_line_break_is_refused_and_creates_nothing(file_vault):
    with pytest.raises(OpError) as refused:
        commands.op_record_memory(
            file_vault, "append", collection="Ledger\nRecords automatic-create-v1 {}", item=ROW, why=WHY,
            new_collection=NEW,
        )

    assert refused.value.code == "INVALID_COLLECTION_REFERENCE"
    assert commands.op_record_memory(file_vault, "inspect")["collections"] == []


def test_a_title_with_a_pasted_no_break_space_creates_its_collection(file_vault):
    # Only a character that ends a line is refused; a no-break space pasted from a document is ordinary text.
    created = commands.op_record_memory(
        file_vault, "append", collection="Espresso\u00a0trials", item=ROW, why=WHY, new_collection=NEW
    )

    assert created["operation"] == "append"
    assert [entry["title"] for entry in commands.op_record_memory(file_vault, "inspect")["collections"]] == [
        "Espresso\u00a0trials"
    ]


def test_new_collection_for_a_title_that_names_a_collection_is_a_plain_append(file_vault):
    existing = file_create(file_vault, "Ledger")

    receipt = commands.op_record_memory(
        file_vault, "append", collection="Ledger", item={"title": "first"}, why=WHY, new_collection=NEW
    )

    inventory_rows = commands.op_record_memory(file_vault, "inspect")["collections"]
    assert receipt["operation"] == "append" and receipt["collection_id"] == existing["collection_id"]
    assert "automatic" not in receipt and "created_collection" not in receipt
    assert [row["title"] for row in inventory_rows] == ["Ledger"]


def test_a_repeated_call_adds_no_second_collection_on_a_file_only_vault(build, vault):
    first = add_record(build, key="one-call")

    same_key = add_record(build, key="one-call")
    new_key = add_record(build, key="another-call")

    assert same_key["replayed"] is True and same_key["item_key"] == first["item_key"]
    assert new_key["outcome"] == "replayed" and new_key["item_key"] == first["item_key"]
    assert [row["title"] for row in commands.op_record_memory(vault, "inspect")["collections"]] == ["Ledger"]


def test_new_collection_for_a_title_that_names_a_file_collection_on_a_served_store_vault_is_a_plain_append(service):
    _retitle(service.root, A_PATH, "Notebook")
    before = set(served_inventory(service))

    receipt = served_call(
        service.root, "record_memory", action="append", collection="Notebook", item={"title": "first"}, why=WHY,
        new_collection=NEW,
    )

    assert receipt["operation"] == "append" and "automatic" not in receipt and "created_collection" not in receipt
    assert set(served_inventory(service)) == before
    rows = served_call(service.root, "record_memory", action="query", collection="Notebook")["rows"]
    assert [row["title"] for row in rows] == ["first"]


def test_without_a_service_new_collection_still_appends_to_a_file_collection_and_refuses_a_new_title(service):
    root = service.root
    _retitle(root, A_PATH, "Notebook")
    service.stop()
    runtime._SERVERS.pop(root, None)
    runtime._SERVED.pop(root, None)

    receipt = served_call(
        root, "record_memory", action="append", collection="Notebook", item={"title": "first"}, why=WHY,
        new_collection=NEW,
    )
    with pytest.raises(OpError) as error:
        served_ledger(service)

    assert receipt["operation"] == "append" and "created_collection" not in receipt
    assert error.value.code == "COLLECTION_STORE_UNAVAILABLE"


def test_a_repeated_call_adds_no_second_collection_on_a_served_store_vault(service):
    first = served_ledger(service, idempotency_key="one-call")

    same_key = served_ledger(service, idempotency_key="one-call")
    new_key = served_ledger(service, idempotency_key="another-call")

    assert same_key["replayed"] is True and same_key["item_key"] == first["item_key"]
    assert new_key["outcome"] == "replayed" and new_key["item_key"] == first["item_key"]
    assert [title for title in served_inventory(service) if title == "Ledger"] == ["Ledger"]
