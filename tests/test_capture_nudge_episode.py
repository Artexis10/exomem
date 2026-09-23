"""The Stop hook's episode ask, and coverage that only a record earns (task 4.1).

Hooks trigger; agents author. Every K substantive turns (K by prominence) the
Stop hook asks the agent for one `episode_memory` record under a key derived
from the session it already holds, so the key survives compaction without the
hook reading a transcript record. Only a SUCCESSFUL record resets the count:
an unrelated committed note, a `Saved ->` marker or a failed record all leave
the episode pending, which is what one committed note silencing the whole
episode used to get wrong.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from exomem import episode_capture, prominence
from exomem._hooks import exomem_capture_nudge as hook
from exomem._hooks import exomem_retrieve_nudge as retrieve_hook

SESSION = "session-episode-under-test"
SUBSTANTIVE = "We compared the two lamps and settled the finish. " + "x" * 400


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("EXOMEM_HOOK_HOME", str(root))
    monkeypatch.setenv("EXOMEM_HOOK_CLIENT", "claude")
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("EXOMEM_PROMINENCE", "balanced")
    for name in (
        "EXOMEM_CAPTURE_NUDGE_DISABLE",
        "EXOMEM_CAPTURE_NUDGE_MIN_CHARS",
        "EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC",
        "EXOMEM_EPISODE_ASK_TURNS",
        "EXOMEM_EPISODE_ASK_COOLDOWN_SEC",
    ):
        monkeypatch.delenv(name, raising=False)
    # The per-turn capture reminder has its own cooldown; zero it so each Stop
    # below is decided by the episode logic, not by that stamp.
    monkeypatch.setenv("EXOMEM_CAPTURE_NUDGE_COOLDOWN_SEC", "0")
    return root


def _transcript(
    tmp_path: Path,
    text: str,
    *,
    tool: str | None = None,
    tool_input: dict | None = None,
    failed: bool = False,
    name: str = "t.jsonl",
) -> Path:
    content: list[dict] = []
    if tool:
        content.append({"type": "tool_use", "id": "tool-1", "name": tool, "input": tool_input or {}})
    content.append({"type": "text", "text": text})
    lines = [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "q"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": content}},
    ]
    if tool and failed:
        lines.append(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tool-1", "is_error": True, "content": "x"}
                    ],
                },
            }
        )
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def _stop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    transcript: Path | None,
    *,
    session: str = SESSION,
    active: bool = False,
) -> dict | None:
    event: dict = {"session_id": session}
    if transcript is not None:
        event["transcript_path"] = str(transcript)
    if active:
        event["stop_hook_active"] = True
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert hook.main() == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def _is_episode_ask(result: dict | None) -> bool:
    return bool(result) and result["reason"].startswith("[Exomem episode check]")


def _stops(monkeypatch, capsys, tmp_path, count: int) -> list[dict | None]:
    transcript = _transcript(tmp_path, SUBSTANTIVE)
    return [_stop(monkeypatch, capsys, transcript) for _ in range(count)]


def test_the_ask_comes_after_k_substantive_turns_with_the_session_key(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    results = _stops(monkeypatch, capsys, tmp_path, k)

    assert not any(_is_episode_ask(result) for result in results[:-1])
    assert _is_episode_ask(results[-1])
    key = episode_capture.hook_key("claude-code", SESSION)
    assert key in results[-1]["reason"]
    assert "episode_memory" in results[-1]["reason"]
    assert results[-1]["decision"] == "block"


def test_the_hook_key_matches_the_server_derivation() -> None:
    for client, session in (("claude-code", "abc"), ("codex", "rollout-9"), ("claude-code", "")):
        assert hook.episode_key(client, session) == episode_capture.hook_key(client, session)
        assert retrieve_hook.episode_key(client, session) == episode_capture.hook_key(client, session)


def test_the_key_survives_a_simulated_compaction(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """After compaction the transcript is different; the session is not."""
    k, cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    first = _stops(monkeypatch, capsys, tmp_path, k)[-1]
    monkeypatch.setattr(hook.time, "time", lambda: 10**12)
    compacted = _transcript(tmp_path, "Summary of the earlier conversation. " + "y" * 400, name="c.jsonl")
    second = _stop(monkeypatch, capsys, compacted)

    assert _is_episode_ask(first) and _is_episode_ask(second)
    key = episode_capture.hook_key("claude-code", SESSION)
    assert key in first["reason"] and key in second["reason"]


@pytest.mark.parametrize(
    "tool, tool_input, text, failed",
    [
        ("mcp__exomem__remember", {"title": "x"}, SUBSTANTIVE, False),
        (None, None, "Saved -> Knowledge Base/Notes/x.md. " + "x" * 400, False),
        ("mcp__exomem__episode_memory", {"action": "record"}, SUBSTANTIVE, True),
        ("mcp__exomem__episode_memory", {"action": "inspect"}, SUBSTANTIVE, False),
    ],
)
def test_only_a_successful_record_resets_coverage(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    tool: str | None,
    tool_input: dict | None,
    text: str,
    failed: bool,
) -> None:
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    _stops(monkeypatch, capsys, tmp_path, k - 1)
    other = _transcript(tmp_path, text, tool=tool, tool_input=tool_input, failed=failed, name="o.jsonl")

    assert _is_episode_ask(_stop(monkeypatch, capsys, other))


def test_a_successful_record_resets_coverage(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    _stops(monkeypatch, capsys, tmp_path, k - 1)
    recorded = _transcript(
        tmp_path,
        SUBSTANTIVE,
        tool="mcp__exomem__episode_memory",
        tool_input={"action": "record", "subject": "Harbor Lamp purchase"},
        name="r.jsonl",
    )
    assert not _is_episode_ask(_stop(monkeypatch, capsys, recorded))
    results = _stops(monkeypatch, capsys, tmp_path, k)
    assert not any(_is_episode_ask(result) for result in results[:-1])
    assert _is_episode_ask(results[-1])


def test_an_ignored_ask_repeats_only_after_its_cooldown(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    k, cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(hook.time, "time", lambda: clock["now"])
    assert _is_episode_ask(_stops(monkeypatch, capsys, tmp_path, k)[-1])
    assert not any(_is_episode_ask(result) for result in _stops(monkeypatch, capsys, tmp_path, 3))
    clock["now"] += cooldown
    assert _is_episode_ask(_stops(monkeypatch, capsys, tmp_path, 1)[-1])


def test_stop_hook_active_stays_silent(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    _stops(monkeypatch, capsys, tmp_path, k - 1)
    transcript = _transcript(tmp_path, SUBSTANTIVE)
    assert _stop(monkeypatch, capsys, transcript, active=True) is None


@pytest.mark.parametrize("record", [True, False])
def test_a_record_made_in_answer_to_the_ask_is_counted(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    record: bool,
) -> None:
    """The ask blocks the Stop; the agent records in the continuation, which
    Stops again with `stop_hook_active`. That record is the coverage the ask
    asked for, so the next turn after the cooldown stays silent. The
    continuation itself never asks, and without a record nothing is reset."""
    k, cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(hook.time, "time", lambda: clock["now"])
    assert _is_episode_ask(_stops(monkeypatch, capsys, tmp_path, k)[-1])

    continuation = _transcript(
        tmp_path,
        "Recorded the recap.",
        tool="mcp__exomem__episode_memory" if record else "mcp__exomem__remember",
        tool_input={"action": "record", "subject": "Harbor Lamp purchase"},
        name="continuation.jsonl",
    )
    assert _stop(monkeypatch, capsys, continuation, active=True) is None

    clock["now"] += cooldown
    assert _is_episode_ask(_stops(monkeypatch, capsys, tmp_path, 1)[-1]) is not record


def test_prominence_off_is_silent(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_PROMINENCE", "off")
    assert _stops(monkeypatch, capsys, tmp_path, 20) == [None] * 20


def test_the_threshold_follows_prominence(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    k, _cooldown = hook._EPISODE_ASK_PRESETS["maximal"]
    results = _stops(monkeypatch, capsys, tmp_path, k)
    assert _is_episode_ask(results[-1])
    assert not any(_is_episode_ask(result) for result in results[:-1])


def test_the_hook_table_matches_the_canonical_one() -> None:
    assert hook._EPISODE_ASK_PRESETS == prominence.EPISODE_ASK_PRESETS
    assert prominence.EPISODE_ASK_PRESETS == {
        "off": None,
        "light": (12, 3600),
        "balanced": (6, 1200),
        "maximal": (3, 600),
    }


def test_the_capture_reminder_bytes_are_untouched() -> None:
    """The ask is its own constant; the pinned reminder does not move."""
    assert hook.EPISODE_ASK != hook.REMINDER
    assert hook.REMINDER.startswith("[Exomem capture check]")
    assert "{key}" in hook.EPISODE_ASK


def test_the_deployed_capture_hook_matches_the_packaged_one() -> None:
    root = Path(__file__).resolve().parents[1]
    packaged = root / "src" / "exomem" / "_hooks" / "exomem_capture_nudge.py"
    deployed = root / "plugins" / "claude-code" / "hooks" / "exomem_capture_nudge.py"
    assert deployed.read_bytes() == packaged.read_bytes()


# --- the retrieve hook sends attribution with every packet request ------------


def test_the_retrieve_hook_sends_client_and_session_on_both_rungs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_HOOK_CLIENT", "codex")
    bodies: list[dict] = []

    class _Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        bodies.append(json.loads(request.data.decode("utf-8")))
        return _Response(json.dumps({"success": True, "data": {"abstained": True}}).encode())

    monkeypatch.setattr(retrieve_hook, "_rest_port", lambda: 1234)
    monkeypatch.setattr(retrieve_hook.urllib.request, "urlopen", fake_urlopen)
    attribution = retrieve_hook.attribution("rollout-9")
    retrieve_hook._fetch_packet_via_rest("continue", "key", "", 1.0, attribution)

    assert bodies[0]["client"] == "codex"
    assert bodies[0]["session"] == episode_capture.hook_key("codex", "rollout-9")

    argvs: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = json.dumps({"success": True, "data": {"abstained": True}})

    def fake_run(argv, **_kwargs):
        argvs.append(list(argv))
        return _Proc()

    monkeypatch.setattr(retrieve_hook.shutil, "which", lambda name: "/usr/bin/exomem")
    monkeypatch.setattr(retrieve_hook.subprocess, "run", fake_run)
    retrieve_hook._fetch_packet_via_cli("continue", "", 1.0, attribution)

    argv = argvs[0]
    assert argv[argv.index("--client") + 1] == "codex"
    assert argv[argv.index("--session") + 1] == episode_capture.hook_key("codex", "rollout-9")
    assert argv.index("--session") < argv.index("--")


def test_an_older_service_that_refuses_attribution_still_serves_the_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin can update before the service it talks to; the packet must not
    degrade to the bare reminder for that window."""
    import urllib.error

    bodies: list[dict] = []

    class _Response:
        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return json.dumps({"success": True, "data": {"abstained": True, "ok": 1}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        bodies.append(body)
        if "client" in body:
            raise urllib.error.HTTPError(request.full_url, 400, "UNKNOWN_PARAM", {}, None)
        return _Response()

    monkeypatch.setattr(retrieve_hook, "_rest_port", lambda: 1234)
    monkeypatch.setattr(retrieve_hook.urllib.request, "urlopen", fake_urlopen)
    packet = retrieve_hook._fetch_packet_via_rest(
        "continue", "key", "", 1.0, retrieve_hook.attribution("s")
    )

    assert packet == {"abstained": True, "ok": 1}
    assert ["client" in body for body in bodies] == [True, False]
