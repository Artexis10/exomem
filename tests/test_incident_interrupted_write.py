"""The incident runner can interrupt a write and score how the agent recovers.

The product answers ``MUTATION_ACKNOWLEDGEMENT_PENDING`` when an accepted write outlives its deadline, and no
healthy write can be made to do so without degrading the whole session. The ``interrupted-write`` cases therefore
withhold one acknowledgement at the transport: the write commits, the agent is told its outcome is unknown, and a
check reads whether the agent replayed that write or read the collection before it wrote anything new. A transport
that dropped a message, hid a refusal as an uncertain write, or let the agent's replay count as a new write would
fail or pass every unit of these cases for a reason that says nothing about the product.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from membench.outcome import billing
from membench.outcome.incidents import __main__ as incidents
from membench.outcome.incidents import openai_actor, warmup, withhold
from membench.outcome.isolation import REPO_ROOT, mcp_server
from membench.outcome.judging import Judgement

# Each test that seeds starts the product in fresh interpreters, which takes a while on a busy machine.
pytestmark = pytest.mark.timeout(600)

PENDING = "MUTATION_ACKNOWLEDGEMENT_PENDING"
KEY = "io.exomem/idempotency_key"
BIKES = "Bike repairs"
#: The write that every test case withholds, and the words of the arm's pending result that each test can recognise.
SETTINGS = {
    "writers": ["add_record", "edit_record"],
    "result": {
        "content": [{"type": "text", "text": json.dumps({"error": {"code": PENDING}})}],
        "structuredContent": {"error": {"code": PENDING}},
        "isError": False,
    },
    "idempotency_key": KEY,
}


def request(request_id: int, tool: str, arguments: dict, meta: dict | None = None) -> dict:
    params = {"name": tool, "arguments": arguments, **({"_meta": meta} if meta else {})}
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


def response(request_id: int, structured: dict) -> dict:
    result = {"content": [], "structuredContent": structured, "isError": False}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def pass_through(wire: withhold.Withholder, sent: dict, answer: dict) -> dict:
    """What an actor is handed for ``sent`` when the server answers ``answer``."""
    call = wire.begin(sent)
    return answer if call is None else wire.finish(call, answer)


# --- the transport's decision --------------------------------------------------------------------------------


def test_the_first_accepted_write_is_answered_as_pending_and_the_real_result_is_logged(
    tmp_path,
) -> None:
    wire = withhold.Withholder(SETTINGS, tmp_path / "wire.json")
    first = pass_through(
        wire, request(1, "add_record", {"collection": BIKES}, {KEY: "k1"}), response(1, {"ok": 1})
    )
    second = pass_through(
        wire, request(2, "add_record", {"collection": BIKES}), response(2, {"ok": 2})
    )
    assert first == {"jsonrpc": "2.0", "id": 1, "result": SETTINGS["result"]}
    assert second == response(2, {"ok": 2})
    logged = json.loads((tmp_path / "wire.json").read_text())
    assert [(c["tool"], c["identity"], c["withheld"]) for c in logged] == [
        ("add_record", "k1", True),
        ("add_record", None, False),
    ]
    assert logged[0]["real_result"]["structuredContent"] == {"ok": 1}


def test_a_refused_write_is_never_the_one_withheld(tmp_path) -> None:
    """A refusal committed nothing; telling the agent its outcome is unknown would be a different incident."""
    wire = withhold.Withholder(SETTINGS, tmp_path / "wire.json")
    refusal = response(1, {"error": {"code": "RECORD_NATURAL_KEY_CONFLICT"}})
    assert pass_through(wire, request(1, "add_record", {}), refusal) == refusal
    accepted = pass_through(wire, request(2, "add_record", {}), response(2, {"ok": 2}))
    assert accepted["result"] == SETTINGS["result"]


def test_a_read_is_never_withheld(tmp_path) -> None:
    wire = withhold.Withholder(SETTINGS, tmp_path / "wire.json")
    read = response(1, {"rows": []})
    assert pass_through(wire, request(1, "query_records", {}), read) == read
    assert wire.owed


def test_a_session_after_the_main_one_is_neither_withheld_nor_logged(tmp_path) -> None:
    wire = withhold.Withholder(SETTINGS, tmp_path / "wire.json")
    wire.disarm()
    answer = response(1, {"ok": 1})
    assert pass_through(wire, request(1, "add_record", {}), answer) == answer
    assert json.loads((tmp_path / "wire.json").read_text()) == []


@pytest.mark.parametrize(
    "body, content_type",
    [
        (json.dumps(response(7, {"ok": 1})), "application/json"),
        (
            "event: message\r\ndata: " + json.dumps(response(7, {"ok": 1})) + "\r\n\r\n",
            "text/event-stream",
        ),
    ],
    ids=["json reply", "event-stream reply"],
)
def test_the_reply_is_rewritten_in_the_framing_the_server_used(
    tmp_path, body, content_type
) -> None:
    wire = withhold.Withholder(SETTINGS, tmp_path / "wire.json")
    calls = {7: wire.begin(request(7, "add_record", {}))}

    def swap(message):
        call = withhold.answered(calls, message)
        return message if call is None else wire.finish(call, message)

    rewritten = withhold.rewrite(body.encode(), content_type, swap).decode()
    assert PENDING in rewritten and '"ok": 1' not in rewritten
    assert content_type != "text/event-stream" or rewritten.startswith("event: message\ndata: ")


# --- what the agent did after the pending result -------------------------------------------------------------


def wired(*calls: tuple) -> list[dict]:
    """A wire log of ``(tool, arguments)`` or ``(tool, arguments, identity)``; the first call is the withheld write."""
    return [
        {
            "seq": n,
            "tool": call[0],
            "arguments": call[1],
            "identity": call[2] if len(call) > 2 else None,
            "withheld": n == 0,
        }
        for n, call in enumerate(calls)
    ]


WRITE = ("add_record", {"collection": BIKES, "item": {"bike": "Green step-through"}})
OTHER_WRITE = ("add_record", {"collection": BIKES, "item": {"bike": "Black tourer"}})
READ = ("query_records", {"collection": BIKES})


@pytest.mark.parametrize(
    "calls, natural_key, outcome, passes",
    [
        ((WRITE, WRITE), ["bike"], "replayed", True),
        ((WRITE, READ, OTHER_WRITE), ["bike"], "read", True),
        ((WRITE, ("search_memory", {"query": "tandem"}), READ), ["bike"], "read", True),
        ((WRITE, ("add_record", {**WRITE[1], "why": "again"})), ["bike"], "other_write", False),
        ((WRITE, ("add_record", WRITE[1], "new-key")), ["bike"], "other_write", False),
        ((WRITE, OTHER_WRITE, READ), ["bike"], "read", True),
        ((WRITE, OTHER_WRITE, ("add_record", WRITE[1], "new-key")), ["bike"], "other_write", False),
        ((WRITE, OTHER_WRITE, READ), None, "other_write", False),
        ((WRITE, OTHER_WRITE), ["bike"], "none", False),
        ((WRITE, ("query_records", {"collection": "Parts orders"})), ["bike"], "none", False),
        ((("create_note", {"title": "x"}), READ), ["bike"], "no_collection", None),
    ],
    ids=[
        "same write replayed",
        "read first",
        "unrelated read then read of the collection",
        "same row with a changed payload",
        "same payload under a new identity",
        "another row, then a read of the collection",
        "another row, then the same row under a new identity",
        "another row when the collection's key is unknown",
        "another row and never a read",
        "no later call on the collection",
        "write that names no collection",
    ],
)
def test_the_first_call_on_the_withheld_row_decides_whether_the_write_was_reconciled(
    calls, natural_key, outcome, passes
) -> None:
    result = incidents.pending_outcome(wired(*calls), SETTINGS["writers"], natural_key)
    assert result == outcome and incidents.pending_reconciled(result) is passes


def test_a_session_that_made_no_accepted_write_has_nothing_withheld_and_fails() -> None:
    result = incidents.pending_outcome(
        [{**c, "withheld": False} for c in wired(WRITE, READ)], ["add_record"]
    )
    assert result == "no_pending" and incidents.pending_reconciled(result) is False


# --- a unit on a real vault, with the agent stubbed ------------------------------------------------------------


def stream(answer: str) -> str:
    """One turn's ``claude -p`` stream with no tool call: the init event, then the result."""
    init = {
        "type": "system",
        "subtype": "init",
        "model": "m",
        "apiKeySource": billing.KEY_SOURCES[billing.declared()],
        "mcp_servers": [{"name": "exomem", "status": "connected"}],
    }
    done = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": answer,
        "total_cost_usd": 0.1,
    }
    return "\n".join(json.dumps(event) for event in (init, done))


class EveryAnswerTrue:
    """A judge that never calls a model: every boolean field of the schema is true."""

    def call(self, prompt, schema, *, out, timeout):
        verdict = {
            name: True for name, spec in schema["properties"].items() if spec["type"] == "boolean"
        }
        return Judgement({**verdict, "reason": "stub"}, 0.0, [], "")


class StubbedAgent:
    """Stands in for the ``claude`` executable: on the second turn it acts as an MCP client on the server that the
    session's own configuration names, which is the proxy. Every other subprocess is the real one.

    ``after_pending`` is what the agent does when its first write comes back pending, given the client, the item
    it sent and the repairs still to file; it returns the replies the agent saw.
    """

    def __init__(self, case: dict, after_pending) -> None:
        self.turn, self.after_pending, self.replies = case["turns"][1], after_pending, []
        self.real_run = subprocess.run

    async def file(self, server: dict) -> None:
        from fastmcp import Client

        repairs = [
            ("Green step-through", "2026-11-15", "Swapped the saddle clamp"),
            ("Black tourer", "2026-11-15", "Re-greased the bottom bracket"),
            ("Yellow tandem", "2026-11-16", "Fitted new brake cables"),
            ("Orange folder", "2026-11-16", "Adjusted the rear derailleur"),
        ]
        async with Client({"mcpServers": {"exomem": server}}) as client:

            async def add(bike: str, day: str, work: str, **changes) -> dict:
                arguments = {
                    "collection": BIKES,
                    "item": {"bike": bike, "repaired_on": day, "work_done": work},
                    "why": "reported by the user",
                    **changes,
                }
                reply = await client.call_tool("add_record", arguments, raise_on_error=False)
                self.replies.append(json.loads(reply.content[0].text))
                return arguments

            first = await add(*repairs[0])
            await self.after_pending(client, add, first, repairs[0])
            for repair in repairs[1:]:
                await add(*repair)

    def __call__(self, argv, **kwargs):
        if argv[0] != "fake-claude":
            return self.real_run(argv, **kwargs)
        if argv[2] == self.turn:
            server = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())[
                "mcpServers"
            ]["exomem"]
            asyncio.run(self.file(server))
        return subprocess.CompletedProcess(argv, 0, stdout=stream("Done."), stderr="")


async def replays_the_same_write(client, add, first, repair) -> None:
    await add(*repair)


async def reads_the_collection_first(client, add, first, repair) -> None:
    await client.call_tool("query_records", {"collection": BIKES}, raise_on_error=False)


async def files_it_again_under_a_new_spelling(client, add, first, repair) -> None:
    # A different spelling of the same bike is a different row key for the product, so this commits a second row.
    await add("green step-through ", repair[1], repair[2], why="filing it again")


def play_unit(
    tmp_path, monkeypatch, after_pending, case_id: str = "interrupted-write"
) -> tuple[dict, StubbedAgent]:
    case = next(c for c in incidents.load_cases([case_id]))
    agent = StubbedAgent(case, after_pending)
    monkeypatch.setattr(incidents.subprocess, "run", agent)
    # The unit's server is the arm's own; its recall answers "warming" for a while on a fresh vault.
    result = incidents.run_unit(
        case,
        REPO_ROOT,
        tmp_path / "unit",
        judge=EveryAnswerTrue(),
        model="m",
        executable=Path("fake-claude"),
        max_turns=1,
    )
    return result, agent


@pytest.mark.reads_repository_docs
@pytest.mark.parametrize("case_id", ["interrupted-write", "interrupted-write-store"])
def test_an_agent_that_replays_the_uncertain_write_passes_and_the_row_exists_once(
    tmp_path, monkeypatch, case_id
) -> None:
    result, agent = play_unit(tmp_path, monkeypatch, replays_the_same_write, case_id)
    # The agent was told that the outcome was unknown, though the write had committed.
    assert agent.replies[0]["error"]["code"] == PENDING
    held = next(
        c for c in json.loads((tmp_path / "unit" / "wire.json").read_text()) if c["withheld"]
    )
    assert "error" not in held["real_result"]["structuredContent"]
    assert (
        result["pending_outcome"] == "replayed" and result["withheld_write"]["tool"] == "add_record"
    )
    assert result["checks"] == {
        "collection_row": True,
        "no_new_collection": True,
        "pending_reconciled": True,
        "judge": True,
    }
    assert result["passed"] is True


@pytest.mark.reads_repository_docs
def test_an_agent_that_reads_the_collection_before_writing_again_passes(
    tmp_path, monkeypatch
) -> None:
    result, agent = play_unit(tmp_path, monkeypatch, reads_the_collection_first)
    assert agent.replies[0]["error"]["code"] == PENDING
    assert result["pending_outcome"] == "read" and result["passed"] is True


@pytest.mark.reads_repository_docs
def test_an_agent_that_files_the_row_again_under_a_new_identity_leaves_a_duplicate_and_fails(
    tmp_path, monkeypatch
) -> None:
    result, _ = play_unit(tmp_path, monkeypatch, files_it_again_under_a_new_spelling)
    # A new spelling is a new key, so the agent neither replayed nor read the withheld row; the row count catches it.
    assert result["pending_outcome"] == "none"
    # The first write committed, so the second makes five new rows where four repairs were reported.
    path = "Records/Bike repairs/_collection.md"
    assert len(result["rows_after"][path]) - len(result["rows_before"][path]) == 5
    assert (
        result["checks"]["collection_row"] is False
        and result["checks"]["pending_reconciled"] is False
    )
    assert result["passed"] is False


# --- the OpenAI actor's server behind the relay --------------------------------------------------------------


def test_the_relay_withholds_one_write_of_a_stdio_server_and_the_write_commits(tmp_path) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    case = next(c for c in incidents.load_cases(["interrupted-write"]))
    incidents.seed_unit(case, REPO_ROOT, tmp_path, None)
    settings = incidents.run_arm_side(REPO_ROOT, tmp_path, "withhold")
    spec = withhold.relayed(mcp_server(tmp_path, REPO_ROOT), settings, tmp_path)
    item = {
        "bike": "Yellow tandem",
        "repaired_on": "2026-11-16",
        "work_done": "Fitted new brake cables",
    }

    async def session() -> tuple[dict, dict, dict]:
        transport = StdioTransport(
            spec["command"], spec["args"], env=spec["env"], cwd=str(tmp_path)
        )
        async with Client(transport) as client:

            async def ask() -> dict:
                return warmup.answer_of(
                    await client.call_tool("recall_context", {"turn": warmup.WARM_TURN})
                )

            await warmup.await_warm(ask, {})
            replies = []
            for _ in range(2):
                reply = await client.call_tool(
                    "add_record",
                    {"collection": BIKES, "item": item, "why": "reported by the user"},
                    raise_on_error=False,
                )
                replies.append(json.loads(reply.content[0].text))
            rows = await client.call_tool(
                "query_records", {"collection": BIKES}, raise_on_error=False
            )
            return replies[0], replies[1], json.loads(rows.content[0].text)

    first, second, rows = asyncio.run(asyncio.wait_for(session(), 300))
    assert first["error"]["code"] == PENDING
    # The second send is the arm's own answer to a replay, not another pending result.
    assert second.get("error", {}).get("code") != PENDING
    assert [row["bike"] for row in rows["rows"]].count("Yellow tandem") == 1
    logged = json.loads((tmp_path / "wire.json").read_text())
    assert [(c["tool"], c["withheld"]) for c in logged if c["tool"] == "add_record"] == [
        ("add_record", True),
        ("add_record", False),
    ]


def test_the_openai_actor_puts_only_its_main_session_behind_the_relay(
    tmp_path, monkeypatch
) -> None:
    servers = []

    async def converse(spec, root, instructions, turns, model, max_turns, record, trace):
        servers.append(spec)
        return ["answer"]

    monkeypatch.setattr(openai_actor, "converse", converse)
    monkeypatch.setattr(
        openai_actor, "mcp_server", lambda *_: {"command": "exomem", "args": ["serve"], "env": {}}
    )
    monkeypatch.setattr(openai_actor, "record_spend", lambda model, usage: "recorded")
    openai_actor.play(
        {"id": "x", "prompt": "hi", "follow_up": "later", "pass": {}},
        REPO_ROOT,
        tmp_path,
        {},
        server_instructions="x",
        model="m",
        max_turns=5,
        timeout=5,
        withhold=SETTINGS,
    )
    main, follow_up = servers
    assert main["args"][-3:] == ["--", "exomem", "serve"] and "relay" in main["args"]
    assert follow_up == {"command": "exomem", "args": ["serve"], "env": {}}


# --- the case file ---------------------------------------------------------------------------------------------


def test_the_interrupted_write_cases_load_and_the_store_variant_differs_only_by_its_vault() -> None:
    base, variant = incidents.load_cases(["interrupted-write", "interrupted-write-store"])
    assert base["withhold_first_write_ack"] is True and base["pass"]["pending_reconciled"] is True
    assert {k: v for k, v in variant.items() if k not in ("id", "about", "store")} == {
        k: v for k, v in base.items() if k not in ("id", "about")
    }
    assert variant["store"] is True


@pytest.mark.parametrize(
    "change, message",
    [
        ({"transport": "stdio"}, "of an http case"),
        ({"withhold_first_write_ack": False}, "of an http case"),
        ({"withhold_first_write_ack": None}, "come together"),
        ({"pass": {"pending_reconciled": False}}, "come together"),
    ],
    ids=["stdio case", "false option", "option without its check", "check that is not true"],
)
def test_an_interrupted_write_case_that_could_not_be_read_is_refused(change, message) -> None:
    case = {**next(c for c in incidents.load_cases(["interrupted-write"]))}
    if change.get("withhold_first_write_ack", True) is None:
        del case["withhold_first_write_ack"]
    case |= {k: v for k, v in change.items() if v is not None}
    with pytest.raises(SystemExit, match=message):
        incidents.validate_case(case)


def test_rows_each_of_a_collection_row_names_a_field_and_needs_a_row_for_each_list() -> None:
    case = next(c for c in incidents.load_cases(["interrupted-write"]))
    case["pass"]["collection_row"]["rows_each"] = [[{"field": "*", "op": "contains", "value": "x"}]]
    with pytest.raises(SystemExit, match="name a field"):
        incidents.validate_case(case)
    listed = {
        "A": {"rows": [{"record_id": "r1", "bike": "Green"}, {"record_id": "r2", "bike": "Green"}]}
    }
    spec = {
        "collection": "A",
        "equals": 2,
        "rows_each": [
            [{"field": "bike", "op": "contains", "value": "green"}],
            [{"field": "bike", "op": "contains", "value": "black"}],
        ],
    }
    # Two rows for one bike leave the other bike without a row of its own.
    assert incidents.collection_row(listed, {"A": []}, spec) is False
    listed["A"]["rows"][1]["bike"] = "Black tourer"
    assert incidents.collection_row(listed, {"A": []}, spec) is True


def test_the_arm_builds_its_pending_result_and_names_the_tools_that_write(tmp_path) -> None:
    settings = incidents.run_arm_side(REPO_ROOT, tmp_path, "withhold")
    result = settings["result"]
    assert result["structuredContent"]["error"]["code"] == PENDING and result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert {"add_record", "edit_record"} <= set(
        settings["writers"]
    ) and "query_records" not in settings["writers"]
    assert settings["idempotency_key"] == KEY
