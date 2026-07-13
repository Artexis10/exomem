from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex_auth_session_harness.py"
_SPEC = importlib.util.spec_from_file_location("codex_auth_session_harness", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
harness = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = harness
_SPEC.loader.exec_module(harness)

SENTINEL_QUERY = "__exomem_codex_session_smoke_absent__"


def _mcp_event(*, event_type: str = "item.completed", **item_overrides) -> str:
    item = {
        "id": "sentinel-call",
        "type": "mcp_tool_call",
        "server": "exomem",
        "tool": "ask_memory",
        "arguments": {"query": SENTINEL_QUERY},
        "status": "completed",
        "isError": False,
    }
    item.update(item_overrides)
    return json.dumps({"type": event_type, "item": item})


def _mcp_success() -> str:
    return _mcp_event()


class FakeRunner:
    def __init__(self, exec_outputs: list[str] | None = None) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.exec_outputs = list(exec_outputs or [_mcp_success()] * 3)
        self.browser_launches = 0

    def __call__(self, command: list[str], **kwargs):
        self.calls.append((command, kwargs))
        if command[1:3] == ["mcp", "add"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:3] == ["mcp", "login"]:
            self.browser_launches += 1
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, self.exec_outputs.pop(0), "")


def test_harness_adds_and_logs_in_once_then_runs_fresh_ephemeral_processes(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    codex_home = tmp_path / "isolated-codex"

    result = harness.run_harness(
        url="https://kb.example.com/mcp",
        codex_home=codex_home,
        runs=3,
        runner=runner,
        acknowledge_disposable_target=True,
    )

    assert result == 0
    commands = [call[0] for call in runner.calls]
    assert commands[0] == [
        "codex", "mcp", "add", "exomem", "--url", "https://kb.example.com/mcp"
    ]
    assert commands.count(["codex", "mcp", "login", "exomem"]) == 1
    assert runner.browser_launches == 1
    exec_commands = [command for command in commands if command[:2] == ["codex", "exec"]]
    assert len(exec_commands) == 3
    assert all("--ephemeral" in command and "--json" in command for command in exec_commands)
    assert all(call[1]["env"]["CODEX_HOME"] == str(codex_home) for call in runner.calls)
    assert all(call[1]["timeout"] > 0 for call in runner.calls)
    assert codex_home.is_dir()


@pytest.mark.parametrize(
    "output",
    [
        "The exomem MCP server is not logged in",
        "MCP startup incomplete (failed: exomem)",
        "Opening your browser to authenticate",
        "Run codex mcp login exomem",
        json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}),
        _mcp_event(status=None),
        _mcp_event(isError=True),
        _mcp_event(result={"error": "tool failed"}),
        _mcp_event(tool="remember"),
        _mcp_event(arguments={"query": "wrong"}),
        _mcp_success() + "\n" + _mcp_success(),
        json.dumps({"type": "error", "message": "turn failed"}) + "\n" + _mcp_success(),
        _mcp_event(
            event_type="item.started",
            id="write-call",
            tool="remember",
            arguments={"content": "must not run"},
            status="in_progress",
        )
        + "\n"
        + _mcp_success(),
    ],
)
def test_exec_classification_rejects_reauth_or_missing_success(output: str) -> None:
    with pytest.raises(harness.HarnessFailure):
        harness.classify_exec_output(output, "")


def test_exec_classification_accepts_only_completed_exomem_mcp_call() -> None:
    assert harness.classify_exec_output(_mcp_success(), "diagnostic noise") is None


def test_exec_classification_deduplicates_one_sentinel_call_lifecycle() -> None:
    output = (
        _mcp_event(event_type="item.started", status="in_progress")
        + "\n"
        + _mcp_success()
    )
    assert harness.classify_exec_output(output, "") is None


def test_harness_stops_if_registration_fails(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 7, "", "bad")

    with pytest.raises(harness.HarnessFailure, match="register"):
        harness.run_harness(
            url="https://kb.example.com/mcp",
            codex_home=tmp_path / "codex",
            runs=3,
            runner=runner,
            acknowledge_disposable_target=True,
        )

    assert calls == [[
        "codex", "mcp", "add", "exomem", "--url", "https://kb.example.com/mcp"
    ]]


def test_harness_requires_explicit_disposable_target_acknowledgement(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    with pytest.raises(harness.HarnessFailure, match="disposable"):
        harness.run_harness(
            url="https://kb.example.com/mcp",
            codex_home=tmp_path / "codex",
            runs=3,
            runner=runner,
        )
    assert runner.calls == []


def test_harness_refuses_nonempty_codex_home_before_initial_login(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("existing-auth")
    runner = FakeRunner()

    with pytest.raises(harness.HarnessFailure, match="empty"):
        harness.run_harness(
            url="https://kb.example.com/mcp",
            codex_home=codex_home,
            runs=3,
            runner=runner,
            acknowledge_disposable_target=True,
        )

    assert runner.calls == []


def test_unit_suite_never_invokes_live_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unit test attempted live Codex"),
    )
    assert harness.classify_exec_output(_mcp_success(), "") is None
