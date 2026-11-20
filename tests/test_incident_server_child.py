"""What the incident runner hands its server child, and how it reads a session it did not expect.

No real model and no real server: the server child's arguments are captured at ``Popen``, as the other
incident harness tests fake their inputs.
"""

from __future__ import annotations

import json
from pathlib import Path

from epistemic.journeys.f33_referent_replay import tool_calls
from membench.outcome.incidents import server as server_module


def test_the_server_child_runs_in_the_unit_root_with_uploads_on_and_no_token_in_the_record(
    monkeypatch, tmp_path: Path
) -> None:
    # The server loads a `.env` from its working directory over the environment it is given. Run from
    # a checkout that holds one, an "isolated" unit served whatever vault that file names.
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / ".env").write_text("EXOMEM_VAULT_PATH=/somewhere/else\n")
    monkeypatch.chdir(caller)
    root = tmp_path / "unit"
    root.mkdir()
    captured = {}

    class FakeServer:
        pid = 99999
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(server_module, "_await_ready", lambda *args: None)
    monkeypatch.setattr(server_module, "_stop", lambda server: None)
    monkeypatch.setattr(
        server_module.subprocess,
        "Popen",
        lambda argv, **kwargs: captured.update(kwargs) or FakeServer(),
    )
    with server_module.loopback_mcp(root, tmp_path, lambda session: "minted") as entry:
        pass

    assert not (Path(captured["cwd"]) / ".env").exists()
    env = captured["env"]
    token = env["EXOMEM_UPLOAD_TOKEN"]
    assert len(token) >= 32
    assert token not in json.dumps(entry)
    assert entry["headers"]["Authorization"] == "Bearer minted"


def test_a_denied_tool_call_does_not_stop_the_session_being_read() -> None:
    # Claude Code reports a denial as a `system` event whose message is a plain string.
    stream = "\n".join(
        json.dumps(event)
        for event in (
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
                    ]
                },
            },
            {"type": "system", "subtype": "permission_denied", "message": "Bash was denied"},
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "denied", "is_error": True}
                    ]
                },
            },
        )
    )

    calls = tool_calls(stream)

    assert [call["name"] for call in calls] == ["Bash"]
    assert calls[0]["is_error"] is True
