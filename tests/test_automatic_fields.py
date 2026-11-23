"""An additive schema change follows `proactive_capture`: `new_fields` adds fields and writes the row in one call.

Adding a field to a Records collection used to be a confirmed `revise`, so a silent agent put a later fact into a
field that held something else. Under silent capture the server now adds the fields that the call names, typed from
the row's values, and says so in the same reply. Everything else about the schema stays a confirmed `revise`.
"""

from __future__ import annotations

import pytest
import yaml
from test_automatic_collection import WHY, file_create, records_manifest
from test_automatic_collection import file_vault as file_vault  # noqa: F401 - the fixture, by name
from test_automatic_collection import (
    silent_capture as silent_capture,  # noqa: F401 - autouse: capture is silent
)
from test_collection_store_s1_gate import A_PATH
from test_collection_store_service import _retitle
from test_collection_store_service import call as served_call
from test_collection_store_service import service as service  # noqa: F401 - the fixture, by name
from test_collection_store_writer import CID, manifest_path
from test_planning_mutation import _manifest as planning_manifest
from test_single_purpose_tools_mcp import _call, _session
from test_single_purpose_tools_mcp import build as build  # noqa: F401 - the fixture, by name

from exomem import automatic_collection, commands, envelope, structured_collections
from exomem.cli_ops import OpError
from exomem.plan_memory import plan_memory

# One outside claim that arrives after the collection exists, and a dated observation of it.
CLAIM = {"title": "second", "outside_claim": "the maker says 40 g", "checked_on": "2026-11-21"}


def declared(vault, collection="Ledger") -> dict[str, str]:
    """The fields the collection's manifest declares now, with their types."""
    path = vault / f"Knowledge Base/Records/{collection}/_collection.md"
    return {
        name: spec.type
        for name, spec in structured_collections.parse_manifest_bytes(vault, path, path.read_bytes()).schema.fields.items()
    }


def manifest_hash(root, collection) -> str:
    """The version of a served collection's manifest: it moves with every revision."""
    return served_call(root, "record_memory", action="inspect", collection=collection)["lifecycle_guards"][
        "expected_manifest_hash"
    ]


def rows(vault, collection="Ledger") -> list[dict]:
    return commands.op_record_memory(vault, "query", collection=collection).get("rows", [])


FIRST_ROW = {"collection": "Ledger", "item": {"title": "first"}, "why": WHY, "new_collection": {"natural_key": ["title"]}}


def add_record(build, *calls):
    """The first row's call, which creates `Ledger` with a `title` field, then each of `calls`, over MCP in one session."""

    async def scenario(client):
        await _call(client, "add_record", FIRST_ROW)
        return [await _call(client, "add_record", {"collection": "Ledger", "item": CLAIM, "why": WHY, **call}) for call in calls]

    return _session(build(), scenario)


def append(vault, **arguments):
    return commands.op_record_memory(
        vault, "append", collection=arguments.pop("collection", "Ledger"), item=arguments.pop("item", CLAIM),
        why=WHY, **arguments,
    )


def test_silent_capture_adds_the_fields_and_writes_the_row_on_a_file_vault(file_vault):
    file_create(file_vault, "Ledger")
    commands.op_record_memory(file_vault, "append", collection="Ledger", item={"title": "first"}, why=WHY)

    receipt = append(file_vault, new_fields=["outside_claim", "checked_on"])

    assert receipt["operation"] == "append" and receipt["automatic"] is True
    assert receipt["added_fields"] == {"outside_claim": "string", "checked_on": "date"}
    assert declared(file_vault) == {"title": "string", "f1": "string", "outside_claim": "string", "checked_on": "date"}
    by_title = {row["title"]: row for row in rows(file_vault)}
    assert by_title["second"]["outside_claim"] == "the maker says 40 g"
    assert by_title["first"].get("outside_claim") is None


@pytest.mark.parametrize("selector", [CID, "Work"], ids=["by id", "by title"])
def test_silent_capture_adds_the_field_and_writes_the_row_on_a_served_store_vault(service, selector):
    # The vault's file collection shares the store collection's title until it is retitled, and a title must name one.
    _retitle(service.root, A_PATH, "Notebook")

    receipt = served_call(
        service.root, "record_memory", action="append", collection=selector,
        item={"title": "second", "outside_claim": "the maker says 40 g"}, why=WHY, new_fields=["outside_claim"],
    )

    stored = served_call(service.root, "record_memory", action="query", collection=CID)["rows"]
    assert receipt["operation"] == "append" and receipt["automatic"] is True
    assert receipt["added_fields"] == {"outside_claim": "string"}
    assert {row["title"]: row.get("outside_claim") for row in stored} == {
        "Canonical": None, "second": "the maker says 40 g",
    }


@pytest.mark.parametrize("level", ["advisory", "off"])
def test_a_capture_that_is_not_silent_refuses_the_new_field_and_returns_the_proposal(file_vault, level):
    file_create(file_vault, "Ledger")
    envelope.set_disposition("proactive_capture", level)

    with pytest.raises(OpError) as refused:
        append(file_vault, new_fields=["outside_claim", "checked_on"])

    error = refused.value.as_public_dict()
    assert error["code"] == "AUTOMATIC_FIELDS_REFUSED"
    assert error["bound"] == "proactive_capture" and error["disposition"] == level
    assert error["message"].split("; ")[-1] == (
        "add it with a confirmed `revise` of the collection only if the user asked for it" if level == "off"
        else "propose the field to the user, and add it with a confirmed `revise` of the collection after they confirm"
    )
    assert error["proposal"] == {"collection": "Ledger", "add_fields": {"outside_claim": "string", "checked_on": "date"}}
    assert declared(file_vault) == {"title": "string", "f1": "string"} and rows(file_vault) == []
    # What the owner confirms is the revise the refusal carries; the row then fits the collection.
    commands.op_record_memory(file_vault, **{**error["retry_call"]})
    assert append(file_vault)["operation"] == "append"
    assert "outside_claim" in declared(file_vault)


def test_an_undeclared_field_is_refused_with_the_call_that_adds_it_under_silent_capture(build, vault):
    refusal, = add_record(build, {})
    retry = refusal["error"]["retry"]["arguments"]
    retried, = add_record(build, retry)
    inspected = commands.op_record_memory(vault, "inspect", collection="Ledger")["coverage"]

    assert refusal["error"]["code"] == "SCHEMA_UNKNOWN_FIELD"
    assert sorted(retry["options"]["new_fields"]) == ["checked_on", "outside_claim"] and "held" in retry
    assert retried["operation"] == "append" and retried["added_fields"] == {"outside_claim": "string", "checked_on": "date"}
    # The retry resumed the held candidate, so no stale candidate of the written row is left behind.
    assert inspected["held"] == 0


def test_an_undeclared_field_under_advisory_capture_is_refused_with_the_confirmed_revise_and_no_new_fields_retry(build):
    async def scenario(client):
        await _call(client, "add_record", FIRST_ROW)
        envelope.set_disposition("proactive_capture", "advisory")
        return await _call(client, "add_record", {"collection": "Ledger", "item": CLAIM, "why": WHY})

    refusal = _session(build(), scenario)["error"]

    assert refusal["code"] == "SCHEMA_UNKNOWN_FIELD" and "new_fields" not in str(refusal["retry"])
    # The retry is the owner's confirmed revise, ready with its guards, and the message tells the agent to ask first.
    assert refusal["retry"]["tool"] == "manage_memory" and "outside_claim" in refusal["retry"]["arguments"]["arguments"]["manifest_text"]
    assert "outside_claim" in refusal["message"] and "confirm" in refusal["message"]


def test_an_unknown_field_next_to_another_refusal_carries_no_new_fields_retry(build):
    refusal, = add_record(build, {"item": {**CLAIM, "title": 7}})

    assert "new_fields" not in str(refusal["error"].get("retry"))


def test_an_automatic_collection_at_the_field_bound_refuses_one_more_field(file_vault):
    # The twelfth field is the last one a silent capture adds to a collection the agent made; the thirteenth is the owner's revise.
    file_create(file_vault, "Ledger", automatic=True)
    for name in range(automatic_collection.MAX_FIELDS - 3):
        commands.op_record_memory(
            file_vault, "append", collection="Ledger", item={"title": f"row{name}", f"extra{name}": "x"}, why=WHY,
            new_fields=[f"extra{name}"],
        )
    assert len(declared(file_vault)) == automatic_collection.MAX_FIELDS - 1
    assert append(file_vault, item={"title": "twelfth", "last": "x"}, new_fields=["last"])["added_fields"] == {
        "last": "string"
    }

    with pytest.raises(OpError) as refused:
        append(file_vault, item={"title": "thirteenth", "one_more": "x"}, new_fields=["one_more"])

    error = refused.value.as_public_dict()
    assert error["code"] == "AUTOMATIC_FIELDS_REFUSED" and error["bound"] == "fields"
    assert error["maximum"] == automatic_collection.MAX_FIELDS
    assert "one_more" not in declared(file_vault)
    assert "thirteenth" not in {row["title"] for row in rows(file_vault)}


def wide_ledger(vault, **flags):
    """`Ledger` with exactly `MAX_FIELDS` item fields, created on a file vault by `flags`."""
    return commands.op_record_memory(
        vault, "create", manifest_path=manifest_path().replace("Work", "Ledger"),
        manifest_text=records_manifest("Ledger", fields=automatic_collection.MAX_FIELDS), why=WHY, **flags,
    )


def test_a_collection_the_owner_made_takes_a_field_past_the_bound_under_silent_capture(file_vault):
    wide_ledger(file_vault)
    assert len(declared(file_vault)) == automatic_collection.MAX_FIELDS

    receipt = append(file_vault, new_fields=["outside_claim", "checked_on"])

    assert receipt["automatic"] is True and receipt["added_fields"] == {"outside_claim": "string", "checked_on": "date"}
    assert len(declared(file_vault)) == automatic_collection.MAX_FIELDS + 2
    assert [row["title"] for row in rows(file_vault)] == ["second"]


def test_a_collection_whose_create_log_cannot_be_read_takes_a_field_past_the_bound(file_vault):
    wide_ledger(file_vault, automatic=True)
    log = file_vault / "Knowledge Base/log.md"
    log.write_text(log.read_text() + automatic_collection.CREATE_LINE_PREFIX + "{not json\n")

    # An unreadable log answers "not automatic": a wrong call there costs one wide collection, never a refused write.
    receipt = append(file_vault, item={"title": "second", "outside_claim": "x"}, new_fields=["outside_claim"])

    assert receipt["added_fields"] == {"outside_claim": "string"}


def test_an_undeclared_field_of_a_collection_the_owner_made_carries_the_retry_past_the_bound(file_vault):
    wide_ledger(file_vault)

    with pytest.raises(OpError) as refused:
        append(file_vault, item={"title": "second", "outside_claim": "x"})

    error = refused.value.as_public_dict()
    assert error["code"] == "SCHEMA_UNKNOWN_FIELD" and error["retry_with"]["new_fields"] == ["outside_claim"]


def served_wide_ledger(service, **flags):
    """`Ledger` with exactly `MAX_FIELDS` item fields, created on the served store by `flags`."""
    return served_call(
        service.root, "record_memory", action="create", manifest_path=manifest_path().replace("Work", "Ledger"),
        manifest_text=records_manifest("Ledger", 1, fields=automatic_collection.MAX_FIELDS).replace(
            "strategy: markdown-items\n", "strategy: sqlite\n"  # a vault with a store keeps new rows there
        ),
        why=WHY, scaffold=False, **flags,
    )


def test_a_served_collection_the_owner_made_takes_a_field_past_the_bound_under_silent_capture(service):
    served_wide_ledger(service)

    receipt = served_call(
        service.root, "record_memory", action="append", collection="Ledger", item=CLAIM, why=WHY,
        new_fields=["outside_claim", "checked_on"],
    )

    stored = served_call(service.root, "record_memory", action="query", collection="Ledger")["rows"]
    assert receipt["automatic"] is True and receipt["added_fields"] == {"outside_claim": "string", "checked_on": "date"}
    assert [(row["title"], row["outside_claim"]) for row in stored] == [("second", "the maker says 40 g")]


def test_a_served_automatic_collection_at_the_field_bound_refuses_one_more_field(service):
    served_wide_ledger(service, automatic=True)

    with pytest.raises(OpError) as refused:
        served_call(
            service.root, "record_memory", action="append", collection="Ledger", item=CLAIM, why=WHY,
            new_fields=["outside_claim", "checked_on"],
        )

    error = refused.value.as_public_dict()
    assert error["code"] == "AUTOMATIC_FIELDS_REFUSED" and error["bound"] == "fields"
    assert served_call(service.root, "record_memory", action="query", collection="Ledger").get("rows", []) == []


def test_an_undeclared_field_of_a_served_automatic_collection_at_the_bound_carries_the_proposal_and_no_retry(service):
    served_wide_ledger(service, automatic=True)

    with pytest.raises(OpError) as refused:
        served_call(
            service.root, "record_memory", action="append", collection="Ledger",
            item={"title": "second", "outside_claim": "x"}, why=WHY,
        )

    error = refused.value.as_public_dict()
    assert error["code"] == "SCHEMA_UNKNOWN_FIELD" and error["bound"] == "fields"
    assert error["proposal"] == {"collection": "Ledger", "add_fields": {"outside_claim": "string"}}
    assert "retry_with" not in error


def test_a_declared_field_adds_nothing_and_its_value_is_judged_by_the_row(file_vault):
    file_create(file_vault, "Ledger")

    # A date-shaped string for a string field is a valid row, whatever type the value would have given a new field.
    again = append(file_vault, item={"title": "second", "f1": "2026-11-20"}, new_fields=["f1"])
    with pytest.raises(OpError) as refused:
        append(file_vault, item={"title": "third", "f1": 5}, new_fields=["f1"])

    assert again["operation"] == "append" and "added_fields" not in again and "automatic" not in again
    # The row's own validation refuses a value the declared type does not take, and the field stays as declared.
    assert refused.value.code == "SCHEMA_FIELD_TYPE" and declared(file_vault)["f1"] == "string"


def test_a_field_typed_from_a_whole_number_takes_a_later_fraction(file_vault):
    file_create(file_vault, "Ledger")

    first = append(file_vault, item={"title": "first", "score": 4}, new_fields=["score"])
    second = append(file_vault, item={"title": "second", "score": 4.5})

    assert first["added_fields"] == {"score": "number"} and declared(file_vault)["score"] == "number"
    assert second["operation"] == "append"
    assert {row["title"]: row["score"] for row in rows(file_vault)} == {"first": 4, "second": 4.5}


def test_a_field_that_new_fields_names_with_no_scalar_value_is_refused(file_vault):
    file_create(file_vault, "Ledger")

    with pytest.raises(OpError) as refused:
        append(file_vault, item={"title": "second", "tags": ["a", "b"]}, new_fields=["tags"])

    assert refused.value.as_public_dict()["bound"] == "field_type"
    assert "tags" not in declared(file_vault)


BLOCK_ENUM = "    kind:\n      type: enum\n      enum:\n        - open\n        - closed\n"
BLOCK_SCALAR = "    kind:\n      type: string\n      description: >\n        what the kind\n        means\n"
NATURAL_KEY_AFTER = "  natural_key: [title]\n"
LINKS_AFTER = NATURAL_KEY_AFTER + "links:\n    note: kept\n"


def layout_ledger(vault, last_field: str, after_fields: str):
    """`Ledger` whose `fields` end with `last_field`, followed by `after_fields`, both written as given."""
    text = records_manifest("Ledger").replace("  natural_key: [title]\n", "")
    text = text.replace("    f1: {type: string}\n---\n", last_field + after_fields + "---\n")
    return commands.op_record_memory(
        vault, "create", manifest_path=manifest_path().replace("Work", "Ledger"), manifest_text=text, why=WHY
    )


def front(vault) -> dict:
    return yaml.safe_load((vault / manifest_path().replace("Work", "Ledger")).read_text().split("---\n")[1])


LAYOUTS = [
    pytest.param(BLOCK_ENUM, NATURAL_KEY_AFTER, id="block enum, then natural_key"),
    pytest.param(BLOCK_SCALAR, NATURAL_KEY_AFTER, id="block scalar, then natural_key"),
    pytest.param(BLOCK_ENUM, LINKS_AFTER, id="block enum, then a mapping key"),
]


@pytest.mark.parametrize(("last_field", "after_fields"), LAYOUTS)
def test_a_field_is_added_after_a_last_field_written_as_a_block_and_nothing_else_moves(file_vault, last_field, after_fields):
    layout_ledger(file_vault, last_field, after_fields)
    before = front(file_vault)

    receipt = append(file_vault, new_fields=["outside_claim", "checked_on"])

    after = front(file_vault)
    assert receipt["added_fields"] == {"outside_claim": "string", "checked_on": "date"}
    assert after["item_schema"]["fields"] == {
        **before["item_schema"]["fields"], "outside_claim": {"type": "string"}, "checked_on": {"type": "date"}
    }
    assert {key: value for key, value in after.items() if key not in {"item_schema", "record_audit"}} == {
        key: value for key, value in before.items() if key not in {"item_schema", "record_audit"}
    }
    assert after["item_schema"]["natural_key"] == ["title"]
    assert [row["title"] for row in rows(file_vault)] == ["second"]


def test_a_refused_field_keeps_its_proposal_and_its_confirmed_revise_after_a_block_last_field(file_vault):
    layout_ledger(file_vault, BLOCK_ENUM, NATURAL_KEY_AFTER)
    envelope.set_disposition("proactive_capture", "advisory")

    with pytest.raises(OpError) as refused:
        append(file_vault)

    error = refused.value.as_public_dict()
    assert error["code"] == "SCHEMA_UNKNOWN_FIELD" and error["bound"] == "proactive_capture"
    assert error["proposal"] == {"collection": "Ledger", "add_fields": {"outside_claim": "string", "checked_on": "date"}}
    commands.op_record_memory(file_vault, **{**error["retry_call"]})
    assert append(file_vault)["operation"] == "append"


def test_a_refused_field_of_a_collection_whose_fields_cannot_be_extended_in_place_says_why_it_has_no_revise(file_vault):
    flow = records_manifest("Ledger").replace(
        "  fields:\n    title: {type: string}\n    f1: {type: string}\n", "  fields: {title: {type: string}}\n"
    )
    commands.op_record_memory(
        file_vault, "create", manifest_path=manifest_path().replace("Work", "Ledger"), manifest_text=flow, why=WHY
    )
    envelope.set_disposition("proactive_capture", "advisory")

    with pytest.raises(OpError) as refused:
        append(file_vault)

    error = refused.value.as_public_dict()
    # The proposal survives, and the missing revise names its reason instead of vanishing.
    assert error["proposal"]["add_fields"] == {"outside_claim": "string", "checked_on": "date"}
    assert "retry_call" not in error and "one per line" in error["retry_unavailable"]


def test_under_silent_capture_a_collection_whose_fields_cannot_be_extended_offers_no_new_fields_retry(file_vault):
    # Offering `new_fields` here would send the agent into a second refusal with no way forward.
    flow = records_manifest("Ledger").replace(
        "  fields:\n    title: {type: string}\n    f1: {type: string}\n", "  fields: {title: {type: string}}\n"
    )
    commands.op_record_memory(
        file_vault, "create", manifest_path=manifest_path().replace("Work", "Ledger"), manifest_text=flow, why=WHY
    )

    with pytest.raises(OpError) as refused:
        append(file_vault)

    error = refused.value.as_public_dict()
    assert error["code"] == "SCHEMA_UNKNOWN_FIELD" and "retry_with" not in error
    assert error["proposal"]["add_fields"] == {"outside_claim": "string", "checked_on": "date"}
    assert "one per line" in error["retry_unavailable"]


def test_a_new_field_on_a_planning_collection_is_refused_and_nothing_changes(file_vault):
    plan_memory(
        file_vault, "create", manifest_path="Knowledge Base/Planning/Roadmap/_collection.md",
        manifest_text=planning_manifest(), why=WHY,
    )

    with pytest.raises(OpError) as refused:
        commands.op_record_memory(
            file_vault, "append", collection="Knowledge Base/Planning/Roadmap/_collection.md",
            item={"title": "launch", "note": "x"}, why=WHY, new_fields=["note"],
        )

    assert refused.value.code == "RECORDS_PROFILE_REQUIRED"
    path = file_vault / "Knowledge Base/Planning/Roadmap/_collection.md"
    manifest = structured_collections.parse_manifest_bytes(file_vault, path, path.read_bytes())
    assert "note" not in manifest.schema.fields


def test_a_replayed_call_adds_the_field_once_on_a_file_vault(file_vault):
    file_create(file_vault, "Ledger")

    first = append(file_vault, new_fields=["outside_claim", "checked_on"])
    same_item = append(file_vault, new_fields=["outside_claim", "checked_on"])

    assert first["added_fields"] and "added_fields" not in same_item
    assert same_item["outcome"] == "replayed" and same_item["item_key"] == first["item_key"]
    assert list(declared(file_vault)).count("outside_claim") == 1
    assert [row["title"] for row in rows(file_vault)] == ["second"]


def test_a_replayed_call_adds_the_field_once_on_a_served_store_vault(service):
    item = {"title": "second", "outside_claim": "the maker says 40 g"}

    def send(key):
        return served_call(
            service.root, "record_memory", action="append", collection=CID, item=item, why=WHY,
            new_fields=["outside_claim"], idempotency_key=key,
        )

    before = manifest_hash(service.root, CID)
    first = send("one-call")
    revised = manifest_hash(service.root, CID)
    same_key = send("one-call")
    new_key = send("another-call")

    assert first["added_fields"] == {"outside_claim": "string"}
    assert same_key["replayed"] is True and same_key["item_key"] == first["item_key"]
    assert new_key["outcome"] == "replayed" and "added_fields" not in new_key
    assert before != revised == manifest_hash(service.root, CID)


def test_update_adds_the_field_its_changes_name(file_vault):
    file_create(file_vault, "Ledger")
    commands.op_record_memory(file_vault, "append", collection="Ledger", item={"title": "first"}, why=WHY)
    row = rows(file_vault)[0]

    receipt = commands.op_record_memory(
        file_vault, "update", collection="Ledger", item_id=row["id"], changes={"outside_claim": "the maker says 40 g"},
        why=WHY, new_fields=["outside_claim"],
    )

    assert receipt["operation"] == "update" and receipt["added_fields"] == {"outside_claim": "string"}
    assert rows(file_vault)[0]["outside_claim"] == "the maker says 40 g"


def test_new_fields_names_only_fields_the_call_carries(file_vault):
    file_create(file_vault, "Ledger")

    with pytest.raises(OpError) as refused:
        append(file_vault, item={"title": "second"}, new_fields=["outside_claim"])

    assert refused.value.code == "INVALID_RECORD_ARGUMENTS"
    assert "outside_claim" not in declared(file_vault)


def test_a_file_collection_of_a_served_store_vault_takes_new_fields_in_process(service):
    _retitle(service.root, A_PATH, "Notebook")

    receipt = served_call(
        service.root, "record_memory", action="append", collection="Notebook",
        item={"title": "first", "outside_claim": "the maker says 40 g"}, why=WHY, new_fields=["outside_claim"],
    )

    rows_now = served_call(service.root, "record_memory", action="query", collection="Notebook")["rows"]
    assert receipt["added_fields"] == {"outside_claim": "string"}
    assert [(row["title"], row["outside_claim"]) for row in rows_now] == [("first", "the maker says 40 g")]
