"""The public Records and Planning tools through an in-process MCP client.

The workflow: a client lists tools and calls `add_record`, `query_records`, `edit_plan_item`
or a door operation. Middleware translates each call to the hidden grouped command, so these
tests drive a real server and observe the seams a grouped call also crosses: the credential
rule, the writer lease dispatcher, the egress pass and the call ledger. Records fixtures give
a vault; nothing here calls a leaf directly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest
import test_authorization_session_wire_reconnect as wire
from _public_call import public_call
from fastmcp import Client
from record_fixtures import LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT

from exomem import call_ledger, command_surface, commands, guards, writer_lease
from exomem import server as server_module
from exomem.governance import authorization_transport, egress

# The tools these tests call, and the grouped names and retired spellings no client can call.
PUBLIC = {
    "add_record", "query_records", "edit_record",
    "add_plan_item", "query_plan_items", "edit_plan_item",
    "inspect_memory", "manage_memory",
}
GROUPED = {"record_memory", "plan_memory"}
RETIRED = {"read_records", "change_records", "read_plans", "change_plans"}

# Per-call values that no two calls share, in a ledger row and in a refusal envelope.
_VOLATILE_ROW_KEYS = {
    "request_id", "session_id", "caller_principal_hash", "ts_utc", "duration_ms", "total_ms", "budget",
    "sequence", "prev_hash", "row_hash", "spans",
}
_VOLATILE_ERROR_KEYS = {"request_id", "receipt_id"}
# Named parameters of `invoke_command`; the rest of its keywords are the command's arguments.
_LEASE_CONTROL_KEYS = {
    "idempotency_key", "public_idempotency_key", "idempotency_principal_scope",
    "implicit_idempotency_scope", "mutation_request_id",
}


@pytest.fixture
def build(vault, monkeypatch, tmp_path):
    """Return a function that builds a server over the vault with a private ledger."""
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    call_ledger.reset_chain_cache()

    def make():
        return server_module.build_server(require_auth=False)

    yield make
    writer_lease.reset_managers_for_tests()


@pytest.fixture
def seams(monkeypatch):
    """Record what crosses the credential rule, the lease dispatcher, the egress pass and the lease."""
    seen = SimpleNamespace(rules=[], dispatched=[], egress=[], coordinator=[])

    real_rule = authorization_transport.credential_rule
    real_invoke = writer_lease.invoke_command
    real_postfilter = egress.postfilter
    real_postfilter_error = egress.postfilter_error
    real_manager = writer_lease.get_manager

    def rule(name, arguments):
        decision = real_rule(name, arguments)
        seen.rules.append((name, dict(arguments), decision))
        return decision

    def invoke(command, *injected, **kwargs):
        arguments = {k: v for k, v in kwargs.items() if k not in _LEASE_CONTROL_KEYS}
        seen.dispatched.append(
            {
                "command": command.name,
                "kwargs": arguments,
                "digest": writer_lease._command_digest(command, arguments),
                "idempotency_key": kwargs.get("idempotency_key"),
            }
        )
        return real_invoke(command, *injected, **kwargs)

    def postfilter(name, result, root, *, request_kwargs=None, checkpoint=None, reprove_aggregate=False):
        seen.egress.append((name, dict(request_kwargs or {})))
        return real_postfilter(
            name, result, root, request_kwargs=request_kwargs, checkpoint=checkpoint,
            reprove_aggregate=reprove_aggregate,
        )

    def postfilter_error(name, error, root, *, request_kwargs=None):
        seen.egress.append((name, dict(request_kwargs or {})))
        return real_postfilter_error(name, error, root, request_kwargs=request_kwargs)

    def manager():
        seen.coordinator.append(True)
        return real_manager()

    monkeypatch.setattr(authorization_transport, "credential_rule", rule)
    monkeypatch.setattr(writer_lease, "invoke_command", invoke)
    monkeypatch.setattr(egress, "postfilter", postfilter)
    monkeypatch.setattr(egress, "postfilter_error", postfilter_error)
    monkeypatch.setattr(writer_lease, "get_manager", manager)
    return seen


def _ledger_rows() -> list[dict]:
    path = call_ledger.ledger_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _stable(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in _VOLATILE_ROW_KEYS | {"rendering"}}


def _envelope(result: dict) -> dict:
    """A refusal envelope without the ids that every call mints afresh."""
    error = result.get("error")
    if not isinstance(error, dict):
        return result
    return {**result, "error": {k: v for k, v in error.items() if k not in _VOLATILE_ERROR_KEYS}}


def _session(mcp, scenario):
    async def run():
        async with Client(mcp) as client:
            return await scenario(client)

    return asyncio.run(run())


async def _call(client, name, arguments, *, key=None):
    """Call a tool as an agent does; a retired or grouped name is sent as its public call."""
    meta = {command_surface.MCP_IDEMPOTENCY_META_KEY: key} if key else None
    name, arguments = public_call(name, arguments)
    result = await client.call_tool(name, arguments, raise_on_error=False, meta=meta)
    return result.structured_content if result.structured_content is not None else {
        "is_error": result.is_error,
        "text": " ".join(getattr(block, "text", "") for block in result.content),
    }


# --- (a) discovery ------------------------------------------------------------------------


def test_the_surface_lists_the_public_tools_and_hides_every_other_name(build) -> None:
    async def scenario(client):
        return {tool.name: tool for tool in await client.list_tools()}

    tools = _session(build(), scenario)

    assert PUBLIC <= set(tools)
    assert not (GROUPED | RETIRED) & set(tools)
    for name in ("inspect_memory", "manage_memory"):
        assert "operation" in tools[name].input_schema["required"]


def test_annotations_follow_the_effect_of_each_tool(build) -> None:
    async def scenario(client):
        return {tool.name: tool.annotations for tool in await client.list_tools()}

    annotations = _session(build(), scenario)

    for name in ("query_records", "query_plan_items", "inspect_memory"):
        hints = annotations[name]
        assert (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint) == (True, False, True)
        assert hints.open_world_hint is False
    for name in ("edit_record", "edit_plan_item", "manage_memory"):
        hints = annotations[name]
        assert (hints.read_only_hint, hints.destructive_hint) == (False, True)
        assert hints.open_world_hint is False


@pytest.mark.parametrize("name", sorted(GROUPED | RETIRED))
def test_a_grouped_or_retired_name_is_refused_and_lists_what_the_server_has(build, name) -> None:
    async def scenario(client):
        with pytest.raises(Exception, match="Unknown tool") as refused:
            await client.call_tool(name, {})
        return str(refused.value)

    message = _session(build(), scenario)

    assert "add_record" in message


# --- (b) one call, one grouped dispatch -------------------------------------------------

_APPEND = {"collection": "Knowledge Base/Records/Missing/_collection.md", "why": "parity", "item": {"a": 1}}


def test_a_public_write_crosses_every_seam_as_the_grouped_call_does(build, seams) -> None:
    async def scenario(client):
        return await _call(client, "add_record", _APPEND)

    refused = _session(build(), scenario)

    assert "error" in refused
    [dispatched] = seams.dispatched
    assert dispatched["command"] == "record_memory"
    assert dispatched["kwargs"]["action"] == "append"
    assert seams.egress and all(name == "record_memory" for name, _ in seams.egress)
    assert {name for name, _, _ in seams.rules} == {"record_memory"}
    [row] = [row for row in _ledger_rows() if row["tool"] == "record_memory"]
    assert row["rendering"] == "add_record"
    assert call_ledger.verify() == []


def test_a_read_returns_what_the_leaf_returns(build) -> None:
    async def scenario(client):
        return await _call(client, "inspect_memory", {"operation": "records.describe"})

    described = _session(build(), scenario)

    assert "contract_version" in described


def test_a_retry_with_one_key_replays_one_write(build, vault) -> None:
    fields = {"manifest_path": LEDGER_COLLECTION_PATH, "manifest_text": LEDGER_MANIFEST_TEXT, "why": "key"}

    async def scenario(client):
        first = await _call(
            client, "manage_memory", {"operation": "records.create", "arguments": fields}, key="k-1"
        )
        written = (vault / LEDGER_COLLECTION_PATH).read_bytes()
        retry = await _call(
            client, "manage_memory", {"operation": "records.create", "arguments": fields}, key="k-1"
        )
        assert (vault / LEDGER_COLLECTION_PATH).read_bytes() == written
        changed = await _call(
            client,
            "manage_memory",
            {"operation": "records.create", "arguments": {**fields, "why": "another reason"}},
            key="k-1",
        )
        return first, retry, changed

    first, retry, changed = _session(build(), scenario)

    assert "error" not in first and retry == {**first, "replayed": True}
    assert "IDEMPOTENCY_KEY_REUSED" in json.dumps(changed)


def test_a_session_credential_in_the_arguments_binds_to_a_public_call(
    vault, tmp_path, monkeypatch, build
) -> None:
    """On stdio the credential travels as an argument: translation must carry it untouched."""
    credential = authorization_transport.MCP_CREDENTIAL_PARAMETER
    wire._configure_v4_authority(vault, tmp_path / "custody", monkeypatch)

    async def scenario(client):
        opened = await _call(
            client,
            "manage_memory",
            {"operation": "session.open", "arguments": {"ttl_seconds": 600}, "detail": "compact"},
        )
        bearer = opened["issued_credential"]["bearer"]
        accepted = await _call(
            client, "inspect_memory", {"operation": "records.describe", credential: bearer}
        )
        refused = await _call(
            client,
            "inspect_memory",
            {"operation": "records.describe", credential: wire.UNKNOWN_SESSION_BEARER},
        )
        return accepted, refused

    accepted, refused = _session(build(), scenario)

    assert "contract_version" in accepted
    assert refused.get("is_error")


def test_the_blob_guard_fires_for_a_public_write(build, seams) -> None:
    oversize = "x" * (guards.MAX_TEXT_CONTENT_BYTES + 1)
    fields = {"manifest_path": LEDGER_COLLECTION_PATH, "manifest_text": oversize, "why": "guard"}

    mcp = build()

    async def attempt():
        with pytest.raises(Exception, match="BINARY_BLOB_REJECTED"):
            await mcp.call_tool(
                "manage_memory", {"operation": "records.create", "arguments": fields}, run_middleware=True
            )

    asyncio.run(attempt())

    assert seams.dispatched == []
    [row] = [row for row in _ledger_rows() if row["tool"] == "record_memory"]
    assert (row["outcome"], row["error_code"]) == ("refused", "BINARY_BLOB_REJECTED")
    assert row["rendering"] == "manage_memory"


# --- (c) refusal before any lease ---------------------------------------------------------

_REFUSED_BEFORE_ANY_LEASE = [
    ("inspect_memory", {"operation": "records.append", "arguments": {"collection": "c"}}, "manage_memory"),
    ("inspect_memory", {"operation": "plans.triage", "arguments": {"collection": "c"}}, "manage_memory"),
    ("manage_memory", {"operation": "records.query", "arguments": {"collection": "c"}}, "inspect_memory"),
    ("manage_memory", {"operation": "plans.history", "arguments": {"collection": "c"}}, "inspect_memory"),
]


@pytest.mark.parametrize("tool,call,right_door", _REFUSED_BEFORE_ANY_LEASE)
def test_an_operation_the_door_does_not_carry_is_refused_before_the_writer_is_contacted(
    build, seams, tool, call, right_door
) -> None:
    mcp = build()
    coordinator_after_build = len(seams.coordinator)

    async def scenario(client):
        return await _call(client, tool, call)

    refusal = _session(mcp, scenario)

    assert set(refusal) == {"error"}, refusal
    assert right_door in refusal["error"]["message"] or "operation" in refusal["error"]["message"]
    assert seams.dispatched == []
    assert len(seams.coordinator) == coordinator_after_build


_TYPE_WRONG = [
    ("query_records", {"collection": "c", "limit": "ten"}, "limit"),
    ("add_record", {"collection": "c", "why": "w", "item": "x"}, "item"),
    ("query_plan_items", {"collection": "c", "descending": "sometimes"}, "descending"),
    ("edit_plan_item", {"collection": "c", "plan_item": "p", "why": "w", "status": 3}, "status"),
]


@pytest.mark.parametrize("tool,arguments,field", _TYPE_WRONG)
def test_a_wrong_type_is_refused_without_naming_a_hidden_tool(
    build, seams, tool, arguments, field
) -> None:
    """The grouped tool's own validation error names that tool, which the host cannot call."""

    async def scenario(client):
        return await _call(client, tool, arguments)

    refusal = _session(build(), scenario)

    text = json.dumps(refusal)
    assert field in text
    assert not {name for name in GROUPED if name in text}
    assert seams.dispatched == []


def test_the_contact_spy_sees_a_write_that_is_allowed(build, seams) -> None:
    """The refusal tests above prove absence, so show the same spy detects presence."""

    async def scenario(client):
        return await _call(client, "add_record", _APPEND)

    _session(build(), scenario)

    assert len(seams.dispatched) == 1 and seams.coordinator


# --- exclusions ---------------------------------------------------------------------------


def test_a_command_the_surface_withholds_loses_its_tools_and_door_operations_too(
    build, seams, monkeypatch
) -> None:
    records = next(c for c in commands.PRODUCT_COMMANDS if c.name == "record_memory")
    withheld = tuple(
        dataclasses.replace(c, tier=2) if c.name == "record_memory" else c
        for c in commands.PRODUCT_COMMANDS
    )
    assert records.tier == 1
    monkeypatch.setattr(commands, "PRODUCT_COMMANDS", withheld)
    monkeypatch.setenv("EXOMEM_DISABLE_TIER2", "1")
    mcp = build()

    async def scenario(client):
        listed = {tool.name for tool in await client.list_tools()}
        called = await _call(client, "inspect_memory", {"operation": "records.describe"})
        return listed, called

    listed, called = _session(mcp, scenario)

    assert not {"query_records", "add_record", "edit_record", "record_memory"} & listed
    assert {"query_plan_items", "add_plan_item", "edit_plan_item"} <= listed
    assert called.get("is_error") or "error" in called
    assert seams.dispatched == []


# --- refusals an agent can act on ---------------------------------------------------------


def _refusal(reply: dict) -> dict:
    """The error envelope of a refused call."""
    assert reply.get("success") is False or "error" in reply, reply
    return reply["error"]


PLANNING_COLLECTION_PATH = "Knowledge Base/Planning/Work/_collection.md"


async def _create_planning_collection(client) -> None:
    from test_planning_mutation import _manifest

    created = await _call(
        client, "manage_memory",
        {"operation": "plans.create",
         "arguments": {"manifest_path": PLANNING_COLLECTION_PATH, "manifest_text": _manifest(),
                       "why": "create planning collection"}},
    )
    assert "error" not in created, created


def test_a_refused_planning_call_names_the_values_the_parent_kind_and_the_readable_columns(
    build, vault
) -> None:
    from exomem.planning_values import PlanningValues

    statuses = ", ".join(PlanningValues(vault).values("status"))

    async def add(client, **item):
        return await _call(
            client, "add_plan_item",
            {"collection": PLANNING_COLLECTION_PATH, "why": "probe", "item": item},
        )

    async def scenario(client):
        await _create_planning_collection(client)
        bad_status = await add(client, title="Bad status", status="bogus")
        outcome = await add(client, title="Outcome", kind="outcome")
        ref = f"exomem://plan/2db90f18-70df-4e41-986e-2d7d7db1caca/{outcome['plan_id']}"
        wrong_kind = await add(client, title="Wrong kind", kind="work-item", parent=ref)
        missing = await add(
            client, title="Missing", kind="work-item",
            parent="exomem://plan/2db90f18-70df-4e41-986e-2d7d7db1caca/11111111-1111-4111-8111-111111111111",
        )
        foreign = await add(
            client, title="Foreign", kind="work-item",
            parent="exomem://plan/3db90f18-70df-4e41-986e-2d7d7db1caca/11111111-1111-4111-8111-111111111111",
        )
        bad_column = await _call(
            client, "query_plan_items", {"collection": PLANNING_COLLECTION_PATH, "columns": ["nope"]}
        )
        return bad_status, wrong_kind, missing, foreign, bad_column

    bad_status, wrong_kind, missing, foreign, bad_column = _session(build(), scenario)

    assert f"must be one of: {statuses}" in _refusal(bad_status)["message"]
    assert "schema_memory" not in _refusal(bad_status)["message"]
    # The rule names the parent kind the item's own kind requires, from the registry.
    assert "kind work-item takes an active parent of kind: initiative" in _refusal(wrong_kind)["message"]
    # A parent that exists with the wrong kind, one that does not exist and one from another
    # collection read the same.
    assert _refusal(wrong_kind) == _refusal(missing) == _refusal(foreign)
    # A refused column is named, with the columns the caller can read.
    column = _refusal(bad_column)
    assert column["code"] == "INVALID_RECORD_QUERY_FIELD"
    assert "'nope'" in column["message"] and "readable columns: " in column["message"]
    assert "title" in column["message"].split("readable columns: ")[1]


def test_schema_inspect_pages_with_more_or_continuation(build) -> None:
    arguments = {"subject": "planning-values", "limit": 1}

    async def scenario(client):
        first = await _call(client, "inspect_memory", {"operation": "schema.inspect", "arguments": arguments})
        token = first["continuation"]
        by_more = await _call(
            client, "inspect_memory",
            {"operation": "schema.inspect", "arguments": {"subject": "planning-values", "more": token}},
        )
        by_name = await _call(
            client, "inspect_memory",
            {"operation": "schema.inspect", "arguments": {"subject": "planning-values", "continuation": token}},
        )
        both = await _call(
            client, "inspect_memory",
            {"operation": "schema.inspect",
             "arguments": {"subject": "planning-values", "more": token, "continuation": token}},
        )
        # An operation that does not page keeps the name the caller sent.
        unpaged = await _call(
            client, "manage_memory",
            {"operation": "schema.save_entity_types", "arguments": {"more": token}},
        )
        return first, by_more, by_name, both, unpaged

    first, by_more, by_name, both, unpaged = _session(build(), scenario)

    assert first["returned"] == 1
    assert by_more["entries"] and by_more["entries"][0]["key"] != first["entries"][0]["key"]
    assert by_more == by_name
    assert "send one" in _refusal(both)["message"]
    assert "more" in _refusal(unpaged)["message"] and "continuation" not in _refusal(unpaged)["message"]


def test_an_unknown_operation_argument_is_named_with_the_arguments_the_operation_takes(build) -> None:
    async def scenario(client):
        return await _call(
            client, "inspect_memory",
            {"operation": "schema.inspect", "arguments": {"subject": "planning-values", "bogus": 1}},
        )

    refused = _refusal(_session(build(), scenario))

    assert refused["code"] == "INVALID_OPERATION_ARGUMENTS"
    assert "bogus" in refused["message"]
    allowed = refused["message"].split("inspect allowed: ")[1]
    assert "subject" in allowed and "continuation" in allowed


def test_a_stored_item_that_breaks_the_hierarchy_is_not_blamed_on_the_item_being_written(
    build, vault
) -> None:
    """The kind rule is the writer's own to fix; a stored item's broken parent reads as a plain refusal."""
    async def add(client, **item):
        return await _call(
            client, "add_plan_item",
            {"collection": PLANNING_COLLECTION_PATH, "why": "probe", "item": item},
        )

    async def scenario(client):
        await _create_planning_collection(client)
        outcome = await add(client, title="Outcome", kind="outcome")
        prefix = "exomem://plan/2db90f18-70df-4e41-986e-2d7d7db1caca/"
        initiative = await add(
            client, title="Initiative", kind="initiative", parent=prefix + outcome["plan_id"]
        )
        work = await add(
            client, title="Work", kind="work-item", parent=prefix + initiative["plan_id"]
        )
        assert "error" not in work, work
        (vault / "Knowledge Base/Planning/Work/Items/Initiative.md").unlink()
        written = await add(client, title="Another outcome", kind="outcome")
        queried = await _call(client, "query_plan_items", {"collection": PLANNING_COLLECTION_PATH})
        return written, queried

    written, queried = _session(build(), scenario)

    for reply in (written, queried):
        assert _refusal(reply)["code"] == "INVALID_PLAN_RELATION"
        assert "takes" not in _refusal(reply)["message"], reply


def test_a_refused_planning_value_names_the_allowed_values_or_the_fields_at_fault(build) -> None:
    async def add(client, **item):
        return await _call(
            client, "add_plan_item",
            {"collection": PLANNING_COLLECTION_PATH, "why": "probe", "item": item},
        )

    async def scenario(client):
        await _create_planning_collection(client)
        outcome = await add(client, title="Outcome", kind="outcome")
        prefix = "exomem://plan/2db90f18-70df-4e41-986e-2d7d7db1caca/"
        return (
            await add(client, title="Lifecycle", lifecycle="gone"),
            await add(client, title="Area", kind="area", priority="high", parent=prefix + "x"),
            # A missing target, one of the wrong kind and one in another collection read the same.
            await add(client, title="Gone", area=prefix + "11111111-1111-4111-8111-111111111111"),
            await add(client, title="Wrong", area=prefix + outcome["plan_id"]),
            await add(
                client, title="Foreign",
                area="exomem://plan/3db90f18-70df-4e41-986e-2d7d7db1caca/11111111-1111-4111-8111-111111111111",
            ),
            await add(
                client, title="Evidence",
                progress_evidence=[{
                    "collection": "exomem://memory/11111111-1111-4111-8111-111111111111",
                    "role": "gone", "view": "v",
                }],
            ),
        )

    lifecycle, area, missing, wrong_kind, foreign, evidence = _session(build(), scenario)

    assert "lifecycle must be one of: active, archived" in _refusal(lifecycle)["message"].lower()
    assert "areas cannot carry delivery state or hierarchy: parent, priority" in _refusal(area)["message"].lower()
    assert "area takes an active item of kind area" in _refusal(missing)["message"].lower()
    assert _refusal(missing) == _refusal(wrong_kind) == _refusal(foreign)
    assert "progress evidence role must be one of: progress, completion" in _refusal(evidence)["message"].lower()
