"""The incident runner's turn cap, upload configuration and recall-index warming, at unit level.

No real model and no real server: the server child's environment is captured at ``Popen``, and
``recall_context`` replies are faked, as the other incident harness tests fake their inputs.
"""

from __future__ import annotations

import json
from pathlib import Path

from membench.outcome.incidents import __main__ as incidents
from membench.outcome.incidents import server as server_module


def test_the_actor_turn_cap_defaults_to_twenty() -> None:
    assert incidents.INCIDENT_MAX_TURNS == 20


def test_the_server_child_env_configures_uploads_without_leaking_the_token(
    monkeypatch, tmp_path: Path
) -> None:
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
        lambda argv, **kwargs: captured.setdefault("env", kwargs["env"]) or FakeServer(),
    )
    with server_module.loopback_mcp(tmp_path, tmp_path, lambda session: "minted") as entry:
        pass

    env = captured["env"]
    assert env["EXOMEM_BASE_URL"] == entry["url"].removesuffix("/mcp")
    token = env["EXOMEM_UPLOAD_TOKEN"]
    assert len(token) >= 32
    assert token not in json.dumps(entry)
    assert entry["headers"]["Authorization"] == "Bearer minted"


def test_the_readiness_poll_returns_once_recall_stops_warming(monkeypatch) -> None:
    replies = iter(
        [
            {"abstained": True, "reason": "index_warming"},
            {"abstained": True, "reason": "index_warming"},
            {"abstained": False, "anchors": []},
        ]
    )
    monkeypatch.setattr(incidents, "recall_probe", lambda server, turn: next(replies))
    seconds = incidents.await_recall_warm({"url": "u", "headers": {}}, timeout=10, poll=0)
    assert seconds is not None


def test_the_readiness_poll_faults_past_the_deadline(monkeypatch) -> None:
    monkeypatch.setattr(
        incidents,
        "recall_probe",
        lambda server, turn: {"abstained": True, "reason": "index_warming"},
    )
    assert (
        incidents.await_recall_warm({"url": "u", "headers": {}}, timeout=0.01, poll=0.001)
        is None
    )


def _recall_warming_stream() -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "mcp__exomem__recall_context",
                                "input": {"turn": "hi"},
                            }
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call-1",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps(
                                            {
                                                "abstained": True,
                                                "reason": "index_warming",
                                            }
                                        ),
                                    }
                                ],
                            }
                        ]
                    },
                }
            ),
        ]
    )


def test_a_transcript_recall_warming_abstention_is_a_harness_fault() -> None:
    assert incidents.transcript_index_warming(_recall_warming_stream()) is True


def test_a_clean_transcript_is_no_warming_fault() -> None:
    clean = _recall_warming_stream().replace("index_warming", "ambiguous")
    assert incidents.transcript_index_warming(clean) is False
