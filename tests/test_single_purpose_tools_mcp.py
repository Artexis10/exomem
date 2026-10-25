"""Single-purpose Records and Planning tools through an in-process MCP client.

The workflow: a client lists tools and calls `read_records`, `change_records`,
`read_plans` or `change_plans`. Middleware translates each call to the hidden grouped
command, so these tests drive a real server and observe the seams a grouped call also
crosses: the credential rule, the writer lease dispatcher, the egress pass and the call
ledger. Records fixtures give a vault; nothing here calls a leaf directly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest
import test_authorization_session_wire_reconnect as wire
from fastmcp import Client
from record_fixtures import LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT

from exomem import call_ledger, command_surface, commands, guards, writer_lease
from exomem import server as server_module
from exomem.governance import authorization_transport, egress

# What each side of each pair carries, written from the design and not read from the entries.
READ_RECORDS_KINDS = {"describe", "inspect", "validate", "query", "history", "import"}
CHANGE_RECORDS_KINDS = {
    "create", "append", "update", "bulk_upsert", "discard", "revise", "rebaseline", "import",
}
READ_PLANS_KINDS = {"inspect", "validate", "query", "history"}
CHANGE_PLANS_KINDS = {"create", "add", "update", "triage", "revise", "rebaseline"}
RENDERED = {"read_records", "change_records", "read_plans", "change_plans"}
GROUPED = {"record_memory", "plan_memory"}

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
    monkeypatch.delenv("EXOMEM_MCP_TOOL_SHAPE", raising=False)
    monkeypatch.delenv("EXOMEM_MCP_LEGACY_COMPAT", raising=False)
    call_ledger.reset_chain_cache()

    def make(shape: str | None = None):
        if shape is not None:
            monkeypatch.setenv("EXOMEM_MCP_TOOL_SHAPE", shape)
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

    def postfilter(name, result, root, *, request_kwargs=None):
        seen.egress.append((name, dict(request_kwargs or {})))
        return real_postfilter(name, result, root, request_kwargs=request_kwargs)

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
    meta = {command_surface.MCP_IDEMPOTENCY_META_KEY: key} if key else None
    result = await client.call_tool(name, arguments, raise_on_error=False, meta=meta)
    return result.structured_content if result.structured_content is not None else {
        "is_error": result.is_error,
        "text": " ".join(getattr(block, "text", "") for block in result.content),
    }


def _kinds(tool) -> set[str]:
    return {
        branch["properties"]["kind"]["const"]
        for branch in tool.input_schema["properties"]["operation"]["oneOf"]
    }


# --- (a) discovery ------------------------------------------------------------------------


def test_nested_shape_lists_the_four_renderings_and_hides_the_grouped_names(build) -> None:
    async def scenario(client):
        return {tool.name: tool for tool in await client.list_tools()}

    tools = _session(build(), scenario)

    assert RENDERED <= set(tools)
    assert not GROUPED & set(tools)
    assert _kinds(tools["read_records"]) == READ_RECORDS_KINDS
    assert _kinds(tools["change_records"]) == CHANGE_RECORDS_KINDS
    assert _kinds(tools["read_plans"]) == READ_PLANS_KINDS
    assert _kinds(tools["change_plans"]) == CHANGE_PLANS_KINDS
    for name in RENDERED:
        assert "operation" in tools[name].input_schema["required"]
        assert "$defs" not in tools[name].input_schema


def test_a_branch_declares_only_its_own_fields_and_forbids_the_rest(build) -> None:
    async def scenario(client):
        return {tool.name: tool for tool in await client.list_tools()}

    tools = _session(build(), scenario)
    branches = {
        branch["properties"]["kind"]["const"]: branch
        for branch in tools["change_records"].input_schema["properties"]["operation"]["oneOf"]
    }

    assert set(branches["append"]["properties"]) >= {"kind", "collection", "item", "why"}
    assert "manifest_text" not in branches["append"]["properties"]
    assert set(branches["create"]["required"]) == {"kind", "manifest_path", "manifest_text", "why"}
    assert all(branch["additionalProperties"] is False for branch in branches.values())
    assert branches["import"]["properties"]["import_request"]["properties"]["mode"]["enum"] == [
        "start", "cancel",
    ]


def test_annotations_follow_the_effect_of_each_tool(build) -> None:
    async def scenario(client):
        return {tool.name: tool.annotations for tool in await client.list_tools()}

    annotations = _session(build(), scenario)

    for name in ("read_records", "read_plans"):
        hints = annotations[name]
        assert (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint) == (True, False, True)
        assert hints.open_world_hint is False
    for name in ("change_records", "change_plans"):
        hints = annotations[name]
        assert (hints.read_only_hint, hints.destructive_hint) == (False, True)
        assert hints.open_world_hint is False


def test_the_hidden_grouped_name_stays_callable_with_its_own_annotation(build) -> None:
    mcp = build()

    async def scenario(client):
        grouped = await mcp.get_tool("record_memory")
        described = await _call(client, "record_memory", {"action": "describe"})
        return grouped, described

    grouped, described = _session(mcp, scenario)

    assert grouped.annotations.read_only_hint is False
    assert "contract_version" in described


# --- (b) one call, two spellings ----------------------------------------------------------

_APPEND = {"collection": "Knowledge Base/Records/Missing/_collection.md", "why": "parity", "item": {"a": 1}}


def test_a_rendered_write_crosses_every_seam_as_the_grouped_call_does(build, seams) -> None:
    async def scenario(client):
        rendered = await _call(client, "change_records", {"operation": {"kind": "append", **_APPEND}})
        direct = await _call(client, "record_memory", {"action": "append", **_APPEND})
        return rendered, direct

    rendered, direct = _session(build(), scenario)

    assert _envelope(rendered) == _envelope(direct)
    assert rendered["success"] is False
    first, second = seams.dispatched
    assert first == second and first["command"] == "record_memory"
    assert seams.egress[: len(seams.egress) // 2] == seams.egress[len(seams.egress) // 2 :]
    assert seams.egress and all(name == "record_memory" for name, _ in seams.egress)
    assert [(name, arguments, rule) for name, arguments, rule in seams.rules[1::2]] == [
        (name, arguments, rule) for name, arguments, rule in seams.rules[0::2]
    ]
    assert {name for name, _, _ in seams.rules} == {"record_memory"}
    rows = _ledger_rows()
    via_rendering, via_group = [row for row in rows if row["tool"] == "record_memory"]
    assert _stable(via_rendering) == _stable(via_group)
    assert via_rendering["rendering"] == "change_records"
    assert "rendering" not in via_group
    assert call_ledger.verify() == []


def test_a_read_returns_what_the_grouped_call_returns(build) -> None:
    async def scenario(client):
        rendered = await _call(client, "read_records", {"operation": {"kind": "describe"}})
        direct = await _call(client, "record_memory", {"action": "describe"})
        return rendered, direct

    rendered, direct = _session(build(), scenario)

    assert rendered == direct
    assert "contract_version" in rendered


def test_a_retry_across_spellings_with_one_key_replays_one_write(build, vault) -> None:
    fields = {"manifest_path": LEDGER_COLLECTION_PATH, "manifest_text": LEDGER_MANIFEST_TEXT, "why": "key"}

    async def scenario(client):
        first = await _call(client, "change_records", {"operation": {"kind": "create", **fields}}, key="k-1")
        written = (vault / LEDGER_COLLECTION_PATH).read_bytes()
        retry = await _call(client, "record_memory", {"action": "create", **fields}, key="k-1")
        assert (vault / LEDGER_COLLECTION_PATH).read_bytes() == written
        changed = await _call(
            client,
            "change_records",
            {"operation": {"kind": "create", **fields, "why": "another reason"}},
            key="k-1",
        )
        return first, retry, changed

    first, retry, changed = _session(build(), scenario)

    assert first["ok"] is True and retry == first
    assert retry["idempotency_key"] == first["idempotency_key"] == "k-1"
    assert "IDEMPOTENCY_KEY_REUSED" in json.dumps(changed)


def test_a_session_credential_in_the_arguments_binds_to_a_rendered_call_as_to_the_grouped_call(
    vault, tmp_path, monkeypatch, build
) -> None:
    """On stdio the credential travels as an argument: translation must carry it untouched."""
    credential = authorization_transport.MCP_CREDENTIAL_PARAMETER
    wire._configure_v4_authority(vault, tmp_path / "custody", monkeypatch)

    async def scenario(client):
        opened = await _call(
            client,
            "govern_memory",
            {"operation": "session", "session_action": "open", "ttl_seconds": 600,
             "response_detail": "compact"},
        )
        bearer = opened["issued_credential"]["bearer"]
        rendered = await _call(
            client, "read_records", {"operation": {"kind": "describe"}, credential: bearer}
        )
        direct = await _call(client, "record_memory", {"action": "describe", credential: bearer})
        refused_rendered = await _call(
            client,
            "read_records",
            {"operation": {"kind": "describe"}, credential: wire.UNKNOWN_SESSION_BEARER},
        )
        refused_direct = await _call(
            client, "record_memory", {"action": "describe", credential: wire.UNKNOWN_SESSION_BEARER}
        )
        return rendered, direct, refused_rendered, refused_direct

    rendered, direct, refused_rendered, refused_direct = _session(build(), scenario)

    assert "contract_version" in rendered
    assert rendered == direct
    assert refused_rendered.get("is_error") and refused_rendered == refused_direct


def test_the_blob_guard_fires_for_a_rendered_write(build, seams) -> None:
    oversize = "x" * (guards.MAX_TEXT_CONTENT_BYTES + 1)
    fields = {"manifest_path": LEDGER_COLLECTION_PATH, "manifest_text": oversize, "why": "guard"}

    mcp = build()

    async def attempt(name, arguments):
        with pytest.raises(Exception, match="BINARY_BLOB_REJECTED"):
            await mcp.call_tool(name, arguments, run_middleware=True)

    asyncio.run(attempt("change_records", {"operation": {"kind": "create", **fields}}))
    asyncio.run(attempt("record_memory", {"action": "create", **fields}))

    assert seams.dispatched == []
    via_rendering, via_group = [row for row in _ledger_rows() if row["tool"] == "record_memory"]
    assert (via_rendering["outcome"], via_rendering["error_code"]) == ("refused", "BINARY_BLOB_REJECTED")
    assert via_rendering["rendering"] == "change_records"
    assert _stable(via_rendering) == _stable(via_group)


# --- (c) refusal before any lease ---------------------------------------------------------

_REFUSED_BEFORE_ANY_LEASE = [
    ("read_records", {"kind": "append", "collection": "c", "why": "w", "item": {}}, "change_records"),
    ("read_records", {"kind": "revise", "collection": "c"}, "change_records"),
    ("read_records", {"kind": "import", "collection": "c", "import_request": {"mode": "start"}}, "change_records"),
    ("read_records", {"kind": "import", "collection": "c", "import_request": {"mode": "cancel"}}, "change_records"),
    ("read_records", {"kind": "import", "collection": "c"}, "import_request"),
    ("read_records", {"kind": "import", "collection": "c", "import_request": {}}, "preview, status"),
    ("read_records", {"kind": "import", "collection": "c", "import_request": {"mode": 3}}, "preview, status"),
    ("change_records", {"kind": "query", "collection": "c"}, "read_records"),
    ("read_plans", {"kind": "triage", "collection": "c"}, "change_plans"),
    ("change_plans", {"kind": "history", "collection": "c"}, "read_plans"),
]


@pytest.mark.parametrize("tool,operation,names", _REFUSED_BEFORE_ANY_LEASE)
def test_an_operation_the_tool_does_not_carry_is_refused_before_the_writer_is_contacted(
    build, seams, tool, operation, names
) -> None:
    mcp = build()
    coordinator_after_build = len(seams.coordinator)

    async def scenario(client):
        return await _call(client, tool, {"operation": operation})

    refusal = _session(mcp, scenario)

    code = "INVALID_PLAN_ARGUMENTS" if tool.endswith("plans") else "INVALID_RECORD_ARGUMENTS"
    assert refusal["success"] is False
    assert refusal["error"]["code"] == code
    assert names in refusal["error"]["message"]
    assert seams.dispatched == []
    assert len(seams.coordinator) == coordinator_after_build
    (row,) = [row for row in _ledger_rows() if row["tool"] in GROUPED]
    assert (row["outcome"], row["error_code"], row["rendering"]) == ("refused", code, tool)


_TYPE_WRONG = [
    ("read_records", {"kind": "query", "collection": "c", "limit": "ten"}, "limit"),
    ("change_records", {"kind": "append", "collection": "c", "why": "w", "item": "x"}, "item"),
    ("read_plans", {"kind": "query", "collection": "c", "descending": "sometimes"}, "descending"),
    (
        "change_plans",
        {
            "kind": "triage", "collection": "c", "plan_id": "p", "expected_container_hash": "h",
            "expected_item_version": "v", "why": "w", "transition": "done",
        },
        "transition",
    ),
]


@pytest.mark.parametrize("tool,operation,field", _TYPE_WRONG)
def test_a_wrong_type_in_a_rendered_call_is_refused_without_naming_a_hidden_tool(
    build, seams, tool, operation, field
) -> None:
    """The grouped tool's own validation error names that tool, which the host cannot call."""

    async def scenario(client):
        return await _call(client, tool, {"operation": operation})

    refusal = _session(build(), scenario)

    code = "INVALID_PLAN_ARGUMENTS" if tool.endswith("plans") else "INVALID_RECORD_ARGUMENTS"
    assert refusal.get("success") is False, refusal
    assert refusal["error"]["code"] == code
    assert field in refusal["error"]["message"] and tool in refusal["error"]["message"]
    assert not {name for name in GROUPED if name in json.dumps(refusal)}
    assert seams.dispatched == []


def test_a_write_refused_before_the_lease_carries_the_grouped_write_refusal_keys(build) -> None:
    """A client branches on `ok`, `error_code` and `request_id` whichever spelling it called."""
    stray = {**_APPEND, "manifest_text": "not an append field"}

    async def scenario(client):
        rendered = await _call(client, "change_records", {"operation": {"kind": "append", **stray}})
        direct = await _call(client, "record_memory", {"action": "append", **stray})
        return rendered, direct

    rendered, direct = _session(build(), scenario)

    assert _envelope(rendered) == _envelope(direct)
    assert set(rendered["error"]) == set(direct["error"])
    (row,) = [row for row in _ledger_rows() if row.get("rendering") == "change_records"]
    assert rendered["error"]["request_id"] == row["request_id"]


def test_the_contact_spy_sees_a_write_that_is_allowed(build, seams) -> None:
    """The refusal tests above prove absence, so show the same spy detects presence."""

    async def scenario(client):
        return await _call(client, "change_records", {"operation": {"kind": "append", **_APPEND}})

    _session(build(), scenario)

    assert len(seams.dispatched) == 1 and seams.coordinator


# --- (d) shapes ---------------------------------------------------------------------------


def test_each_shape_lists_its_tools_and_schemas(build) -> None:
    async def tools_of(shape):
        async with Client(build(shape)) as client:
            return {tool.name: tool for tool in await client.list_tools()}

    nested = asyncio.run(tools_of("nested"))
    writer_lease.reset_managers_for_tests()
    flat = asyncio.run(tools_of("flat"))
    writer_lease.reset_managers_for_tests()
    grouped = asyncio.run(tools_of("grouped"))

    assert RENDERED <= set(nested) and not GROUPED & set(nested)
    assert RENDERED <= set(flat) and not GROUPED & set(flat)
    assert GROUPED <= set(grouped) and not RENDERED & set(grouped)
    for name, kinds in (
        ("read_records", READ_RECORDS_KINDS),
        ("change_records", CHANGE_RECORDS_KINDS),
        ("read_plans", READ_PLANS_KINDS),
        ("change_plans", CHANGE_PLANS_KINDS),
    ):
        schema = flat[name].input_schema
        assert set(schema["properties"]["action"]["enum"]) == kinds
        assert "operation" not in schema["properties"]
        assert "action" in schema["required"]
    assert "item" not in flat["read_records"].input_schema["properties"]
    assert "manifest_text" in flat["change_records"].input_schema["properties"]
    assert grouped["record_memory"].annotations.read_only_hint is False


def test_every_shape_resolves_one_logical_call_to_the_same_dispatch(build, seams) -> None:
    calls = {
        "nested": [
            ("read_records", {"operation": {"kind": "describe"}}),
            ("change_records", {"operation": {"kind": "append", **_APPEND}}),
        ],
        "flat": [
            ("read_records", {"action": "describe"}),
            ("change_records", {"action": "append", **_APPEND}),
        ],
        "grouped": [
            ("record_memory", {"action": "describe"}),
            ("record_memory", {"action": "append", **_APPEND}),
        ],
    }
    dispatched = {}
    for shape, sequence in calls.items():

        async def scenario(client, sequence=sequence):
            return [await _call(client, name, arguments) for name, arguments in sequence]

        seams.dispatched.clear()
        results = _session(build(shape), scenario)
        writer_lease.reset_managers_for_tests()
        dispatched[shape] = (list(seams.dispatched), results)

    reached = {shape: dispatches for shape, (dispatches, _) in dispatched.items()}
    outcomes = {shape: [_envelope(result) for result in results] for shape, (_, results) in dispatched.items()}
    assert reached["nested"] == reached["flat"] == reached["grouped"]
    assert outcomes["nested"] == outcomes["flat"] == outcomes["grouped"]


def test_an_unknown_shape_stops_the_server_build(build) -> None:
    with pytest.raises(RuntimeError, match="EXOMEM_MCP_TOOL_SHAPE"):
        build("sideways")


# --- exclusions ---------------------------------------------------------------------------


def test_a_command_the_surface_withholds_loses_its_renderings_too(build, seams, monkeypatch) -> None:
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
        called = await _call(client, "read_records", {"operation": {"kind": "describe"}})
        return listed, called

    listed, called = _session(mcp, scenario)

    assert not {"read_records", "change_records", "record_memory"} & listed
    assert {"read_plans", "change_plans"} <= listed
    assert called.get("is_error")
    assert seams.dispatched == []
