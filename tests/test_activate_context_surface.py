"""Task 5.3 — one leaf, three doors, one kill switch, honest timings.

The surface requirement is that MCP, the CLI and the personal REST facade reach
`activate_context` over the SAME leaf function, so there is one documented
contract and no per-door logic. The kill switch is the environment-independence
requirement: the tool stays ON the surface and abstains, so the published
tool-surface digest does not depend on how a particular install is configured.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, server, working_set_index, working_set_runtime
from exomem.__main__ import main as cli_main
from exomem.governance.principal import library_scope

TURN = "I'm planning to tow the Cargo Sled north — what are its constraints?"

#: Blocks that must agree across the three doors. `timings` is excluded because
#: wall time is not a contract, and `due_state` because the shared advisory
#: carrier is a delta keyed on session state, not on the door.
PARITY_BLOCKS = (
    "anchors",
    "roles",
    "units",
    "pointers",
    "current_state",
    "missing",
    "ambiguity",
    "budget",
    "abstained",
)


@pytest.fixture
def activation_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import lexstore

    _seed_structure(vault)
    _seed_planning(vault)
    # All doors must see the same published catalog. Activation deliberately
    # does not repair it; CLI/REST initialization must not change the evidence
    # halfway through a parity assertion.
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return vault


def _run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    try:
        code = cli_main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
def test_configured_role_content_stays_out_of_private_activation_diagnostics(
    activation_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time
    import uuid

    import yaml

    from exomem import call_spans, context_roles, latency_watch, query_log

    role_id = "Notes/Example Route.md"
    proposal = {
        "schema_version": 1,
        "roles": {
            name: {"anchor_defaults": [], "cues": [], "evidence_cues": []}
            for name in context_roles.shipped_registry().roles
        },
    }
    # These anchor kinds are fixed by the context-role schema.
    proposal["roles"][role_id] = {
        "lane": "units",
        "anchor_defaults": ["entity", "resource", "project", "hub", "collection", "page", "plan"],
    }
    checked = context_roles.load_roles(activation_vault, proposal=proposal)
    assert not checked.findings
    context_roles.override_path(activation_vault).write_text(yaml.safe_dump(proposal))
    context_roles.clear_cache()
    working_set_runtime.reset_caches_for_tests()
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(log_dir))
    monkeypatch.setattr(query_log, "_disabled", lambda: False)
    watch = latency_watch.Watch(started_at=time.time() - 601)
    monkeypatch.setattr(latency_watch, "_WATCH", watch)
    call_token = uuid.uuid4().hex
    token = call_spans.MCP_CALL_TOKEN.set(call_token)
    try:
        packet = commands.op_activate_context(activation_vault, turn=TURN)
    finally:
        call_spans.MCP_CALL_TOKEN.reset(token)
        spans = call_spans.pop_call_spans(call_token)

    assert packet["abstained"] is False
    assert any(role["id"] == role_id for role in packet["roles"])
    assert "timings" not in packet
    logged = json.loads((log_dir / "activations.jsonl").read_text())
    assert "anchors" not in logged
    assert any(span["name"] == "working_set.lanes.0" for span in logged["spans"])
    assert any(span["name"] == "recall.working_set.lanes.0" for span in spans)
    assert role_id not in json.dumps([logged, spans])
    assert role_id not in repr(list(watch._ring))


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
def test_unavailable_graph_diagnostics_cannot_delay_activation_past_its_deadline(
    activation_vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked queue must not delay a packet that discards graph diagnostics."""
    import threading
    import time

    from exomem import deferred_index, epistemic_graph, request_budget

    warm_managed_cell(activation_vault)
    caller = threading.get_ident()
    real_open = epistemic_graph.EpistemicGraphIndex._open_read_snapshot

    def unavailable_for_caller(index):
        return None if threading.get_ident() == caller else real_open(index)

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex, "_open_read_snapshot", unavailable_for_caller
    )
    reference = commands.op_activate_context(activation_vault, turn=TURN)
    working_set_runtime.reset_caches_for_tests()
    locker = deferred_index._connect(activation_vault, create=True)
    locker.execute("PRAGMA locking_mode=EXCLUSIVE")
    locker.execute("BEGIN EXCLUSIVE")
    budget = request_budget.RequestBudget(seconds=50, entry=time.monotonic() - 48.4)
    token = request_budget.set_current(budget)
    try:
        packet = commands.op_activate_context(activation_vault, turn=TURN)
        returned = time.monotonic()
    finally:
        request_budget.reset_current(token)
        locker.rollback()
        locker.close()

    assert returned < budget.deadline
    assert packet["abstained"] is False
    for block in PARITY_BLOCKS:
        assert packet.get(block) == reference.get(block), block


def _surface_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in (
        "EXOMEM_UPLOAD_TOKEN",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN",
        "EXOMEM_CF_ACCESS_AUD",
    ):
        monkeypatch.delenv(leaky, raising=False)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    return server.build_server(require_auth=False)


def _rest_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return TestClient(_surface_server(monkeypatch).http_app())


# --------------------------------------------------------------------------- #
# Registry and surface membership
# --------------------------------------------------------------------------- #


def test_the_tool_is_registered_on_all_three_surfaces() -> None:
    product = {command.name: command for command in commands.PRODUCT_COMMANDS}
    assert "activate_context" in product
    command = product["activate_context"]
    assert command.surfaces >= {"mcp", "rest", "cli"}
    assert command.tier == 1
    assert command.cli_writes is False
    assert command.leaf is commands.op_activate_context
    assert [param.name for param in command.params][:6] == [
        "turn",
        "max_chars",
        "purpose",
        "continuity",
        "anchor",
        "include_timings",
    ]


def test_the_tool_stays_on_the_surface_under_the_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET", "1")
    names = {
        command.name
        for command in commands.product_commands_for("mcp", expose_tier2=True)
    }
    assert "activate_context" in names


def test_kill_switch_abstains_with_reason_disabled(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET", "1")
    packet = commands.op_activate_context(activation_vault, turn=TURN)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "disabled"}
    assert packet["units"] == []
    assert packet["budget"]["used_chars"] == 0


# --------------------------------------------------------------------------- #
# Three-door parity
# --------------------------------------------------------------------------- #


def _comparable(packet: dict) -> dict:
    return {key: packet.get(key) for key in PARITY_BLOCKS}


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
def test_three_doors_return_the_same_packet(
    activation_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    direct = commands.op_activate_context(activation_vault, turn=TURN, max_chars=2000)
    assert direct["abstained"] is False
    assert direct["units"]

    code, out = _run_cli(
        ["activate", TURN, "--max-chars", "2000", "--json"], capsys
    )
    assert code == 0, out
    envelope = json.loads(out)
    assert envelope["success"] is True, envelope
    cli_packet = envelope["data"]

    client = _rest_client(monkeypatch)
    response = client.post(
        "/api/activate_context",
        json={"turn": TURN, "max_chars": 2000},
        headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 200, response.text
    rest_packet = response.json()["data"]

    assert _comparable(cli_packet) == _comparable(direct)
    assert _comparable(rest_packet) == _comparable(direct)
    assert cli_packet["generation"]["roles_hash"] == direct["generation"]["roles_hash"]
    assert rest_packet["generation"]["index_generation"] == direct["generation"][
        "index_generation"
    ]


@pytest.mark.usefixtures("anchors_packet")  # relevant-due advice is anchors-only until the maps packet carries turn-relevant material; see Amendment 12
def test_mcp_and_rest_return_the_same_relevant_due_identity(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote activation must retain a complete review identity through both dispatchers."""
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import due_state, lexstore

    mcp = _surface_server(monkeypatch)
    client = TestClient(mcp.http_app())
    _seed_structure(vault)
    _seed_planning(vault)
    page = "Knowledge Base/Products/Cargo Sled.md"
    target = vault / page
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\n## Prediction\n\n- id: capacity-check\n- check_by: "
        + (dt.date.today() - dt.timedelta(days=1)).isoformat()
        + "\n\nThe runners withstand the planned load.\n",
        encoding="utf-8",
    )
    due_state.reconcile(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    row = next(row for row in due_state.served_entries(vault) if row["path"] == page)
    arguments = {"turn": TURN, "anchor": page}

    rest = client.post("/api/activate_context", json=arguments,
                       headers={"Authorization": "Bearer sekret"})
    assert rest.status_code == 200, rest.text
    with library_scope():
        result = asyncio.run(mcp.call_tool("activate_context", arguments, run_middleware=False))
    packet = result.structured_content

    assert rest.json()["data"]["relevant_due_refs"] == packet["relevant_due_refs"]
    (candidate,) = packet["relevant_due_refs"]
    assert candidate["ref"] == row["ref"] and candidate["fingerprint"] == row["fingerprint"]
    assert "due_state" not in packet and "due_state" not in rest.json()["data"]


@pytest.mark.usefixtures("anchors_packet")  # relevant-due advice is anchors-only until the maps packet carries turn-relevant material; see Amendment 12
@pytest.mark.parametrize("headroom", [0.05, 1.3])
def test_mcp_due_omission_reaches_the_response_and_final_observation(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headroom: float,
) -> None:
    """A served MCP packet and its observation must expose optional due omissions."""
    import time

    from fastmcp import Client
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import call_ledger, due_state, lexstore, query_log, request_budget
    from exomem import vault as vault_module
    from exomem.governance import egress

    mcp = _surface_server(monkeypatch)
    _seed_structure(vault)
    _seed_planning(vault)
    page = "Knowledge Base/Products/Cargo Sled.md"
    target = vault / page
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\n## Prediction\n\n- id: capacity-check\n- check_by: "
        + (dt.date.today() - dt.timedelta(days=1)).isoformat()
        + "\n\nThe runners withstand the planned load.\n",
        encoding="utf-8",
    )
    due_state.reconcile(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    baseline = commands.op_activate_context(vault, turn=TURN, anchor=page)
    assert baseline["abstained"] is False and baseline["relevant_due_refs"]
    # The late read below must happen: drop the parse the baseline left behind.
    due_state.reset_serve_cache()

    logs = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(logs))
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(logs))
    monkeypatch.delenv("EXOMEM_DISABLE_CALL_LEDGER", raising=False)
    monkeypatch.setattr(query_log, "_disabled", lambda: False)
    monkeypatch.setenv(request_budget.BUDGET_ENV_VAR, "5")
    request_budget.reset_resolution_cache()
    call_ledger.reset_chain_cache()
    guard = egress.guard_working_set
    read = vault_module.read_bytes_without_pinning
    projection = due_state.state_path(vault)
    observed: list[tuple[request_budget.RequestBudget, float]] = []
    projection_reads: list[float] = []

    def slow_guard(*args, **kwargs):
        packet = guard(*args, **kwargs)
        budget = request_budget.current()
        if budget is not None:
            observed.append((budget, budget.deadline))
            pause = budget.remaining() - headroom
            assert pause > 0
            time.sleep(pause)
        return packet

    def slow_projection(path, *args, **kwargs):
        budget = request_budget.current()
        if path == projection and budget is not None:
            projection_reads.append(budget.remaining())
            time.sleep(budget.remaining() + 0.04)
        return read(path, *args, **kwargs)

    monkeypatch.setattr(egress, "guard_working_set", slow_guard)
    monkeypatch.setattr(vault_module, "read_bytes_without_pinning", slow_projection)

    async def invoke():
        async with Client(mcp) as client:
            result = await client.call_tool("activate_context", {"turn": TURN, "anchor": page})
            return result.structured_content

    try:
        packet = asyncio.run(invoke())
    finally:
        request_budget.reset_resolution_cache()
        call_ledger.reset_chain_cache()
    assert request_budget.current() is None
    assert packet["abstained"] is False
    for name in ("anchors", "units", "current_state"):
        assert packet.get(name) == baseline.get(name), name
    assert "relevant_due_refs" not in packet
    (budget, deadline), = observed
    assert budget.deadline == deadline
    assert packet["request_budget"]["applied"] is True
    assert packet["request_budget"]["seconds"] == budget.seconds
    assert packet["request_budget"]["skipped"] == budget.skipped
    assert packet["request_budget"]["truncated"] == budget.truncated
    if headroom < request_budget.ACTIVATION_STAGE_RESERVE_SECONDS:
        assert projection_reads == []
        assert budget.skipped == ["working_set.relevant_due"] and budget.truncated == []
    else:
        assert projection_reads
        assert budget.skipped == [] and budget.truncated == ["working_set.relevant_due"]

    ledger = [json.loads(line) for line in (logs / "ledger.jsonl").read_text().splitlines()]
    assert len(ledger) == 1 and ledger[0]["outcome"] == "ok"
    assert ledger[0]["activation_result"] == {
        "abstained": False, "reason": None, "budget_limited": True,
    }
    assert ledger[0]["budget"]["skipped"] == budget.skipped
    assert call_ledger.verify(logs / "ledger.jsonl") == []
    activation = [json.loads(line) for line in (logs / "activations.jsonl").read_text().splitlines()]
    assert len(activation) == 1 and activation[0]["outcome"] == "served"
    assert "capacity-check" not in json.dumps(ledger + activation)
    assert "relevant_due_refs" not in json.dumps(ledger + activation)


def test_the_long_cli_name_reaches_the_same_leaf(
    activation_vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    short_code, short_out = _run_cli(["activate", TURN, "--json"], capsys)
    long_code, long_out = _run_cli(["activate_context", TURN, "--json"], capsys)

    assert short_code == long_code == 0
    assert _comparable(json.loads(short_out)["data"]) == _comparable(
        json.loads(long_out)["data"]
    )


# --------------------------------------------------------------------------- #
# Timings: stages partition the total
# --------------------------------------------------------------------------- #


def _root_stages(timings: dict) -> dict[str, dict]:
    """The stages that partition the call: entries no other stage contained."""
    return {
        name: entry
        for name, entry in timings["stages"].items()
        if "parent" not in entry and "ms" in entry
    }


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
def test_working_set_spans_are_registered_and_sum_within_the_total(
    activation_vault: Path,
) -> None:
    packet = commands.op_activate_context(
        activation_vault, turn=TURN, include_timings=True
    )

    timings = packet["timings"]
    stages = timings["stages"]
    assert {"working_set.index", "working_set.resolve", "working_set.roles", "working_set.budget"} <= set(
        stages
    )
    assert any(name.startswith("working_set.lanes.") for name in stages)
    roots = _root_stages(timings)
    assert roots, "every stage must be a registered interval"
    summed = sum(entry["ms"] for entry in roots.values())
    assert summed <= timings["total_ms"] + 1e-6, (summed, timings["total_ms"])
    assert timings["unattributed_ms"] >= 0.0


def test_timings_are_absent_unless_requested(activation_vault: Path) -> None:
    packet = commands.op_activate_context(activation_vault, turn=TURN)

    assert "timings" not in packet


# --------------------------------------------------------------------------- #
# Read-only and additive
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
def test_an_empty_turn_abstains_without_touching_the_index(activation_vault: Path) -> None:
    packet = commands.op_activate_context(activation_vault, turn="   ")

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unresolved"}


def test_the_operation_writes_nothing_to_the_vault(activation_vault: Path) -> None:
    before = {
        path: path.stat().st_mtime_ns
        for path in sorted((activation_vault / "Knowledge Base").rglob("*"))
        if path.is_file()
    }

    commands.op_activate_context(activation_vault, turn=TURN)

    after = {
        path: path.stat().st_mtime_ns
        for path in sorted((activation_vault / "Knowledge Base").rglob("*"))
        if path.is_file()
    }
    assert after == before


# --------------------------------------------------------------------------- #
# Review round: the packet is not a due_state carrier
# --------------------------------------------------------------------------- #


def test_activation_never_consumes_the_due_state_emission(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recall stays the only `due_state` carrier.

    `_with_due_state` consults the emission ledger and MARKS it emitted, so an
    activation that carried the block would silently eat the next recall's
    delta — a read-only operation changing what a later read returns.
    """
    from exomem import due_state as due_state_module

    block = {"totals": {"due": 2, "overdue": 1}, "items": []}
    monkeypatch.setattr(due_state_module, "served", lambda *_a, **_k: block)

    emitted: list[object] = []
    real_should_emit = due_state_module.should_emit

    def counting_should_emit(candidate, **kwargs):
        emitted.append(candidate)
        return real_should_emit(candidate, **kwargs)

    monkeypatch.setattr(due_state_module, "should_emit", counting_should_emit)

    packet = commands.op_activate_context(activation_vault, turn=TURN)

    assert "due_state" not in packet
    assert emitted == [], "activation must not consult or advance the emission ledger"


def test_ask_memory_keeps_its_due_state_block_after_an_activation(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import due_state as due_state_module
    from exomem import find as find_module

    block = {"totals": {"due": 2, "overdue": 1}, "items": []}
    monkeypatch.setattr(due_state_module, "served", lambda *_a, **_k: block)
    monkeypatch.setattr(due_state_module, "should_emit", lambda *_a, **_k: True)

    find_module.clear_cache()
    baseline = commands.op_ask_memory(activation_vault, query="depot sled", limit=5)
    assert isinstance(baseline, dict) and baseline["due_state"] == block

    find_module.clear_cache()
    commands.op_activate_context(activation_vault, turn=TURN)
    find_module.clear_cache()
    after = commands.op_ask_memory(activation_vault, query="depot sled", limit=5)

    assert isinstance(after, dict)
    assert after["due_state"] == baseline["due_state"]


# --------------------------------------------------------------------------- #
# Attribution: `client` and `session` are recorded, never served
# --------------------------------------------------------------------------- #


def _without_token(packet: dict) -> dict:
    """The packet minus its continuity token, which dates its own minting."""
    return {key: value for key, value in packet.items() if key != "continuity"}


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
def test_attribution_is_accepted_and_never_changes_the_packet(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[dict] = []
    real_serve = working_set_runtime.serve

    per_request = {"freshness_snapshot", "lexical_seconds", "timings"}

    def recording_serve(*args, **kwargs):
        served.append({key: value for key, value in kwargs.items() if key not in per_request})
        return real_serve(*args, **kwargs)

    monkeypatch.setattr(working_set_runtime, "serve", recording_serve)

    plain = commands.op_activate_context(activation_vault, turn=TURN)
    attributed = commands.op_activate_context(
        activation_vault, turn=TURN, client="claude-code", session="ep-" + "a1" * 16
    )

    # A session with no history of its own gets exactly the vault's packet.
    assert json.dumps(_without_token(plain), sort_keys=True) == json.dumps(
        _without_token(attributed), sort_keys=True
    )
    # Re-based for ruling S5-1 (the heat is scoped to the caller's thread
    # first): attribution now reaches `serve`, so the caller's own session can
    # rank first, but only as derived keys. Nothing else about the request
    # differs, and the raw session id and client never reach it.
    assert served[0].get("attribution") is None
    held = served[1]["attribution"]
    assert held.client == "claude-code"
    assert held.session and "a1" * 16 not in held.session
    assert {key: value for key, value in served[0].items() if key != "attribution"} == {
        key: value for key, value in served[1].items() if key != "attribution"
    }
    assert not {"client", "session"} & set(served[1])


def test_an_invalid_client_label_or_session_is_ignored_not_refused(
    activation_vault: Path,
) -> None:
    packet = commands.op_activate_context(
        activation_vault, turn=TURN, client="Not A Label!", session="s" * 5000
    )

    assert packet["abstained"] is False


def test_attribution_is_accepted_on_every_door(
    activation_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in (
        ["activate", TURN, "--client", "codex", "--session", "abc", "--json"],
        ["activate_context", TURN, "--client", "codex", "--session", "abc", "--json"],
        ["activate", TURN, "--session", "abc", "--workspace", "0f" * 12, "--json"],
        ["activate_context", TURN, "--session", "abc", "--workspace", "0f" * 12, "--json"],
    ):
        code, out = _run_cli(argv, capsys)
        assert code == 0, out
        assert json.loads(out)["success"] is True

    client = _rest_client(monkeypatch)
    response = client.post(
        "/api/activate_context",
        json={"turn": TURN, "client": "claude-code", "session": "abc", "workspace": "0f" * 12},
        headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 200, response.text

    product = {command.name: command for command in commands.PRODUCT_COMMANDS}
    names = [param.name for param in product["activate_context"].params]
    assert {"client", "session", "workspace"} <= set(names)


def test_the_server_instructions_skip_a_turn_a_hook_already_activated() -> None:
    """Round-2 ruling Q1: the skip clause names the injected working set, so
    the agent does not repeat the hook's keyed call without its keys."""
    text = server.SERVER_INSTRUCTIONS
    assert "a turn whose Exomem working set a hook already injected" in text
    assert "trigger=task_refresh" in text
    assert "keep turn" in text and "`conversation.focus`" in text
