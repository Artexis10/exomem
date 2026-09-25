"""The `episode_due` advisory: asking a tool-only client to record, never forcing it.

claude.ai, ChatGPT and generic MCP clients have no Stop hook to ask them. The
server can only advise, on the one call such a client makes every turn:
`activate_context`. After enough activations with no `episode_memory` record
from the same caller, the packet carries a one-sentence `episode_due` block, at
most once per cooldown, silent under `proactive_capture=off`, never on the REST
or CLI doors the hooks use, and never persisted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import capture_sweep, command_surface, envelope, episode_nudge, query_log, server

KEY = ("principal:abc", "chatgpt", "/vault")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch):
    episode_nudge.reset_state()
    clock = {"now": 10_000.0}
    monkeypatch.setattr(episode_nudge, "_clock", lambda: clock["now"])
    monkeypatch.setattr(capture_sweep, "_proactive_capture_permitted", lambda: True)
    yield clock
    episode_nudge.reset_state()


def _as_mcp_caller(
    monkeypatch: pytest.MonkeyPatch,
    *,
    transport: str | None = "http",
    client_name: str = "chatgpt",
) -> None:
    monkeypatch.setattr(
        command_surface,
        "mcp_caller_identity",
        lambda: {
            "client_name": client_name,
            "client_version": "1",
            "transport": transport,
            "session_id": None,
        },
    )
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "principal:abc")


def _activations(vault: Path, count: int) -> list[dict | None]:
    return [episode_nudge.on_activation(vault) for _ in range(count)]


def test_it_fires_at_the_eighth_activation_without_a_record(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_mcp_caller(monkeypatch)
    results = _activations(vault, episode_nudge.EPISODE_NUDGE_ACTIVATIONS)

    assert results[:-1] == [None] * (episode_nudge.EPISODE_NUDGE_ACTIVATIONS - 1)
    assert results[-1] == {
        "rule": episode_nudge.EPISODE_RULE,
        "activations": episode_nudge.EPISODE_NUDGE_ACTIVATIONS,
    }
    assert "episode_memory" in episode_nudge.EPISODE_RULE
    assert episode_nudge.EPISODE_NUDGE_ACTIVATIONS == 8


def test_it_honours_its_cooldown(vault: Path, monkeypatch: pytest.MonkeyPatch, _fresh) -> None:
    _as_mcp_caller(monkeypatch)
    _activations(vault, 8)
    assert _activations(vault, 20) == [None] * 20

    _fresh["now"] += episode_nudge.EPISODE_NUDGE_COOLDOWN_SECONDS
    again = episode_nudge.on_activation(vault)
    assert again is not None and again["activations"] == 29
    assert episode_nudge.EPISODE_NUDGE_COOLDOWN_SECONDS == 1800


def test_a_record_resets_the_count(vault: Path, monkeypatch: pytest.MonkeyPatch, _fresh) -> None:
    _as_mcp_caller(monkeypatch)
    _activations(vault, 7)
    episode_nudge.note_record(vault)
    _fresh["now"] += episode_nudge.EPISODE_NUDGE_COOLDOWN_SECONDS

    results = _activations(vault, 8)
    assert results[:-1] == [None] * 7
    assert results[-1] is not None and results[-1]["activations"] == 8


def test_it_is_silent_on_the_rest_and_cli_doors(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside an MCP call there is no transport: the hooks' own doors."""
    _as_mcp_caller(monkeypatch, transport=None)
    assert _activations(vault, 20) == [None] * 20


def test_it_is_silent_when_proactive_capture_is_off(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_mcp_caller(monkeypatch)
    monkeypatch.setattr(capture_sweep, "_proactive_capture_permitted", lambda: False)
    assert _activations(vault, 20) == [None] * 20


def test_it_is_silent_for_a_caller_with_no_stable_key(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_mcp_caller(monkeypatch)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "session:ephemeral")
    assert _activations(vault, 20) == [None] * 20


@pytest.fixture
def activation_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The host-local activation log, switched on in its own directory."""
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(log_dir))
    monkeypatch.setattr(query_log, "_disabled", lambda: False)
    return log_dir / "activations.jsonl"


def _hook_activation(vault: Path, client: str) -> None:
    """What the retrieve hook's REST rung leaves: an activation attributed to
    its client, on a door with no MCP transport."""
    query_log.log_activation_call(vault, packet={}, client=client, session="ep-" + "c3" * 16)


HOOK_CLIENTS = [
    ("claude-code", "claude-code"),
    ("Claude Code", "claude-code"),
    ("codex-mcp-client", "codex"),
    ("codex", "codex"),
]


@pytest.mark.parametrize("client_name, label", HOOK_CLIENTS)
def test_a_hook_client_with_live_hooks_gets_only_its_stop_hook_ask(
    vault: Path, monkeypatch: pytest.MonkeyPatch, activation_log: Path, client_name: str, label: str
) -> None:
    """This vault recently served that client's hook, so its Stop hook asks;
    the advisory would ask twice."""
    _hook_activation(vault, label)
    _as_mcp_caller(monkeypatch, transport="stdio", client_name=client_name)

    assert _activations(vault, 20) == [None] * 20


@pytest.mark.parametrize("client_name, label", HOOK_CLIENTS)
def test_a_hook_client_without_hooks_is_still_asked(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    activation_log: Path,
    tmp_path: Path,
    client_name: str,
    label: str,
) -> None:
    """Claude Code or Codex with only the MCP server has no Stop hook, so the
    advisory is its only ask. Hook evidence that is stale, from another vault,
    or from the other hook client does not count."""
    other_vault = tmp_path / "other-vault"
    (other_vault / "Knowledge Base").mkdir(parents=True)
    _hook_activation(other_vault, label)
    _hook_activation(vault, "codex" if label == "claude-code" else "claude-code")
    _hook_activation(vault, label)
    rows = activation_log.read_text(encoding="utf-8").splitlines()
    stale = json.loads(rows[-1])
    stale["ts_utc"] = "2020-01-01T00:00:00.000+00:00"
    activation_log.write_text("\n".join([*rows[:-1], json.dumps(stale)]) + "\n", encoding="utf-8")
    _as_mcp_caller(monkeypatch, transport="stdio", client_name=client_name)

    results = _activations(vault, episode_nudge.EPISODE_NUDGE_ACTIVATIONS)

    assert results[:-1] == [None] * (episode_nudge.EPISODE_NUDGE_ACTIVATIONS - 1)
    assert results[-1] is not None


def test_the_proactive_gate_is_the_delegation_envelope(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same fail-closed read the capture-sweep advisory makes."""
    monkeypatch.undo()
    episode_nudge.reset_state()
    _as_mcp_caller(monkeypatch)
    monkeypatch.setattr(envelope, "active", lambda: {"proactive_capture": "off"})
    assert _activations(vault, 9) == [None] * 9


def test_it_is_never_persisted(vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}
    _as_mcp_caller(monkeypatch)
    assert _activations(vault, 8)[-1] is not None
    after = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before

    episode_nudge.reset_state()  # what a restart does
    assert _activations(vault, 7) == [None] * 7


def test_the_ledger_is_bounded(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_mcp_caller(monkeypatch)
    for index in range(episode_nudge.LEDGER_CAP + 50):
        monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda i=index: f"principal:{i}")
        episode_nudge.on_activation(vault)
    assert episode_nudge.tracked_keys() == episode_nudge.LEDGER_CAP


def test_the_server_instructions_ask_for_a_record_and_stay_short() -> None:
    text = server.SERVER_INSTRUCTIONS
    assert "episode_memory" in text
    assert "decided" in text and "left open" in text
    assert len(text) <= 900, len(text)


def test_only_the_advisory_is_added_to_the_packet(
    monkeypatch: pytest.MonkeyPatch, vault: Path
) -> None:
    from test_activate_context_surface import TURN
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import commands, lexstore, working_set_index, working_set_runtime

    _seed_structure(vault)
    _seed_planning(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    _as_mcp_caller(monkeypatch)

    packets = [commands.op_activate_context(vault, turn=TURN) for _ in range(8)]

    assert "episode_due" not in packets[6]
    assert packets[7]["episode_due"]["activations"] == 8
    strip = {"episode_due", "continuity"}
    assert json.dumps(
        {key: value for key, value in packets[7].items() if key not in strip}, sort_keys=True
    ) == json.dumps(
        {key: value for key, value in packets[6].items() if key not in strip}, sort_keys=True
    )
