"""Task 5.3 — one leaf, three doors, one kill switch, honest timings.

The surface requirement is that MCP, the CLI and the personal REST facade reach
`activate_context` over the SAME leaf function, so there is one documented
contract and no per-door logic. The kill switch is the environment-independence
requirement: the tool stays ON the surface and abstains, so the published
tool-surface digest does not depend on how a particular install is configured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, server, working_set_index, working_set_runtime
from exomem.__main__ import main as cli_main

TURN = "I'm planning to tow the Cargo Sled north — how much depot stock is left?"

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

    _seed_structure(vault)
    _seed_planning(vault)
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


def _rest_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in (
        "EXOMEM_UPLOAD_TOKEN",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN",
        "EXOMEM_CF_ACCESS_AUD",
    ):
        monkeypatch.delenv(leaky, raising=False)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    mcp = server.build_server(require_auth=False)
    return TestClient(mcp.http_app())


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


def test_three_doors_return_the_same_packet(
    activation_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    direct = commands.op_activate_context(activation_vault, turn=TURN, max_chars=2000)

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
