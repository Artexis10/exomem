"""Native MCP activation also delivers the session context that changed.

The agent still decides what to recall for each turn. What the hook adds, in
`EXOMEM_RETRIEVE_INJECT=mcp`, is one empty-turn `recall_context` call that
serves the turn-independent map, stance and skills, so a prompt pays for only
the blocks the session does not hold yet. The hook sends no user text on that
call, and every failure leaves the activation text alone.

Every transport seam is the credential module's own opener, patched; every
home is a tmp dir.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.error
from pathlib import Path

import pytest
from hook_requests import answer_requests

from exomem import server
from exomem._hooks import exomem_retrieve_nudge as hook

SESSION = "native-session"
SUBJECT = "Check the adapter decisions we made about the depot schedule"
MAP = {
    "ref": "exomem://map",
    "generation": "map-1",
    "description": "Index of what exists.",
    "items": [{"title": "Depot Ledger", "ref": "exomem://memory/depot", "description": "Stock decisions."}],
}
STANCE = {
    "state": "ready",
    "generation": "stance-1",
    "items": [{"ref": "stance.md#rule", "text": "Keep the source for each claim."}],
}
SKILLS = {
    "state": "ready",
    "items": [{"name": "exomem-review", "summary": "Review a vault.", "ref": "exomem://skill/review"}],
}


@pytest.fixture(autouse=True)
def _native_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in tuple(os.environ):
        if name.startswith(("EXOMEM_", "KB_", "CODEX_", "CLAUDE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(tmp_path / "hook-home"))
    monkeypatch.setenv("EXOMEM_HOOK_CLIENT", "claude")
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(tmp_path / "absent-service.env"))
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "absent-config.json"))
    monkeypatch.setenv("EXOMEM_PROMINENCE", "balanced")
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "mcp")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def getcode(self) -> int:
        return 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _packet(**overrides) -> dict:
    packet = {
        "map": dict(MAP),
        "stance": dict(STANCE),
        "skills": dict(SKILLS),
        "continuity": "token-1",
        "abstained": False,
    }
    return {**packet, **overrides}


def _serve(monkeypatch: pytest.MonkeyPatch, packet: dict) -> list[dict]:
    """Answer every recall request with `packet` (read at answer time) and keep each request body."""
    bodies: list[dict] = []

    def answer(request, timeout=None):
        bodies.append(json.loads(request.data.decode("utf-8")))
        return _Response(json.dumps({"success": True, "data": packet}).encode("utf-8"))

    answer_requests(monkeypatch, answer)
    return bodies


def _prompt(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str = SUBJECT) -> str:
    event = {"hook_event_name": "UserPromptSubmit", "prompt": text, "session_id": SESSION}
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(event)))
    assert hook.main() == 0
    output = capsys.readouterr().out
    return json.loads(output)["hookSpecificOutput"]["additionalContext"] if output.strip() else ""


def _log_text(tmp_path: Path) -> str:
    return (tmp_path / "hook-home" / "exomem-retrieve-nudge.log").read_text(encoding="utf-8")


def test_the_first_prompt_gets_the_session_blocks_before_the_activation_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without this the agent pays a tool call, and the map, stance and skills reach it late or never."""
    bodies = _serve(monkeypatch, _packet())

    context = _prompt(monkeypatch, capsys)

    assert len(bodies) == 1, "one call, no smaller-cap retry"
    assert bodies[0]["turn"] == ""
    assert "conversation" not in bodies[0]
    assert "continuity" not in bodies[0]
    assert "adapter" not in json.dumps(bodies[0]), "the user's words never leave in this call"
    order = [
        context.index(marker)
        for marker in (
            "Keep the source for each claim.",
            "exomem-review",
            "Depot Ledger",
            hook._MCP_ACTIVATION_HEAD[:30],
        )
    ]
    assert order == sorted(order)
    assert context.endswith(hook._mcp_activation_context())


def test_a_later_prompt_with_nothing_changed_gets_the_activation_text_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Held session context must cost a later prompt nothing, and the held token must travel."""
    packet = _packet()
    bodies = _serve(monkeypatch, packet)
    _prompt(monkeypatch, capsys)
    packet["map"] = {**MAP, "items": [], "unchanged": True}
    packet["skills"] = {"state": "unchanged"}

    second = _prompt(monkeypatch, capsys, "And what about the winter schedule for the depot")

    assert bodies[1]["continuity"] == "token-1"
    assert bodies[1]["turn"] == ""
    assert second == hook._mcp_activation_context()


def test_a_later_prompt_gets_only_the_block_that_changed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    packet = _packet()
    _serve(monkeypatch, packet)
    _prompt(monkeypatch, capsys)
    packet["map"] = {**MAP, "items": [], "unchanged": True}
    packet["skills"] = {"state": "unchanged"}
    packet["stance"] = {**STANCE, "generation": "stance-2", "items": [{"ref": "stance.md#new", "text": "Name the vault."}]}

    second = _prompt(monkeypatch, capsys, "And what about the winter schedule for the depot")

    assert "Name the vault." in second
    assert "Keep the source for each claim." not in second
    assert "Depot Ledger" not in second and "map unchanged" not in second
    assert second.endswith(hook._mcp_activation_context())


def test_the_capture_policy_comes_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The agent reads its authority before its context, as in working-set mode."""
    _serve(monkeypatch, _packet(capture_policy={"say": "Capture is explicit-only."}))

    context = _prompt(monkeypatch, capsys)

    assert context.startswith(hook._CAPTURE_POLICY_HEADER)
    assert context.index("Capture is explicit-only.") < context.index("Depot Ledger")


def test_a_slow_service_costs_the_prompt_no_more_than_the_session_budget(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A hook that waits on a stalled service delays every prompt of the session."""
    monkeypatch.setattr(hook, "SESSION_CONTEXT_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(hook, "_MIN_RUNG_SECONDS", 0.1)
    release = threading.Event()
    answer_requests(monkeypatch, lambda request, timeout=None: release.wait(10) and _Response(b""))

    try:
        started = time.monotonic()
        context = _prompt(monkeypatch, capsys)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert context == hook._mcp_activation_context()
    assert elapsed < 2.0
    assert "session=timeout" in _log_text(tmp_path)


def _transport_failure(request, timeout=None):
    raise urllib.error.URLError("refused")


def _not_json(request, timeout=None):
    return _Response(b"<html>gateway</html>")


def _server_error(request, timeout=None):
    raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)


@pytest.mark.parametrize("fake", [_transport_failure, _not_json, _server_error])
def test_an_unusable_answer_leaves_the_activation_text_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, fake
) -> None:
    answer_requests(monkeypatch, fake)

    context = _prompt(monkeypatch, capsys)

    assert context == hook._mcp_activation_context()
    assert "session=none" in _log_text(tmp_path)


def test_without_a_credentialed_lane_the_hook_makes_no_call_and_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A CLI spawn cannot fit the session budget, so the CLI rung stays off even when it is enabled."""
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", "1")
    monkeypatch.setattr(hook, "_credentialed_lanes", lambda: iter(()))
    spawned: list = []
    monkeypatch.setattr(hook.shutil, "which", lambda name: "/usr/bin/exomem")
    monkeypatch.setattr(hook.subprocess, "run", lambda *args, **kwargs: spawned.append(args))

    context = _prompt(monkeypatch, capsys)

    assert context == hook._mcp_activation_context()
    assert spawned == []
    assert "session=none" in _log_text(tmp_path)


def test_the_native_text_asks_for_recall_per_new_subject() -> None:
    """A rule that says recall once leaves every later subject of the conversation without memory."""
    nudge = hook._mcp_activation_context()
    instructions = server.SERVER_INSTRUCTIONS

    assert "once" not in nudge and "once" not in instructions
    assert "subject" in nudge and "conversation.focus" in nudge
    assert "New subject/evidence, before answering" in instructions
    assert "conversation.focus" in instructions
    assert len(instructions) <= 900
