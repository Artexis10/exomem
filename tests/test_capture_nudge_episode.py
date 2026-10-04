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
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def _stops(monkeypatch, capsys, tmp_path, count: int, *, landed: bool = True) -> list[dict | None]:
    """`count` substantive Stops. By default each turn also lands work (and saves a
    note, which keeps the per-turn capture reminder out of the way), because the
    episode ask needs a landing; `landed=False` is the same turns with none."""
    if landed:
        transcript = _claude_turn(
            tmp_path, SUBSTANTIVE, PUSH, ("mcp__exomem__remember", {"text": "note"}, False), name="landed.jsonl"
        )
    else:
        transcript = _transcript(tmp_path, SUBSTANTIVE)
    return [_stop(monkeypatch, capsys, transcript) for _ in range(count)]


def test_the_ask_comes_after_k_substantive_turns_with_the_session_key(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    results = _stops(monkeypatch, capsys, tmp_path, k)

    # Substantial turns with no landing are silent: the ask is what covers them.
    assert results[:-1] == [None] * (k - 1)
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
    compacted = _claude_turn(
        tmp_path, "Summary of the earlier conversation. " + "y" * 400, PUSH, name="c.jsonl"
    )
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


def test_the_episode_ask_is_its_own_text_with_a_key_slot() -> None:
    assert hook.EPISODE_ASK != hook.REMINDER_SHORT
    assert hook.EPISODE_ASK.startswith("[Exomem episode check]")
    assert "{key}" in hook.EPISODE_ASK


def test_the_deployed_capture_hook_matches_the_packaged_one() -> None:
    root = Path(__file__).resolve().parents[1]
    packaged = root / "src" / "exomem" / "_hooks" / "exomem_capture_nudge.py"
    deployed = root / "plugins" / "claude-code" / "hooks" / "exomem_capture_nudge.py"
    assert deployed.read_bytes() == packaged.read_bytes()


# --- a recap recorded through any door counts, not only through this hook's --
# --- own transcript-scan detector --------------------------------------------


class _RestResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self._status = status

    def getcode(self) -> int:
        return self._status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _inspect_payload(revision_count: int) -> bytes:
    return json.dumps(
        {
            "success": True,
            "data": {
                "revisions": [
                    {"revision": i + 1, "recovery": "kept"} for i in range(revision_count)
                ],
                "latest_source_ref": None,
                "coverage_current": "unchecked",
            },
        }
    ).encode("utf-8")


@pytest.mark.parametrize("route", ["episode", "recall", "working_set"])
def test_hook_rest_redirect_does_not_forward_file_key(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str
) -> None:
    received: list[str | None] = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_args):
            pass

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{destination.server_port}/redirected")
            self.end_headers()

        def log_message(self, *_args):
            pass

    source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (source, destination)
    ]
    for thread in threads:
        thread.start()
    try:
        monkeypatch.setenv("EXOMEM_HOST", "127.0.0.1")
        monkeypatch.setenv("EXOMEM_REST_PORT", str(source.server_port))
        monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
        service_env = tmp_path / "service.env"
        service_env.write_text("EXOMEM_REST_API_KEY=disposable-test-key\n", encoding="utf-8")
        monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(service_env))
        if route == "episode":
            assert hook._episode_revision_count("ep-test") is None
        else:
            key, origin = retrieve_hook._resolve_rest_key()
            assert origin == "file"
            if route == "recall":
                assert retrieve_hook._fetch_via_rest("test", key) is None
            else:
                assert retrieve_hook._fetch_packet_via_rest("test", key, "", 1.0) is None
        assert received == []
    finally:
        for server in (source, destination):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


@pytest.mark.parametrize("route", ["episode", "recall", "working_set"])
def test_hook_rest_file_key_bypasses_environment_proxy(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str
) -> None:
    direct: list[str | None] = []
    proxied: list[str | None] = []

    class Intended(BaseHTTPRequestHandler):
        def do_POST(self):
            direct.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"success":true,"data":{"revisions":[],"hits":[],"abstained":true}}')

        def log_message(self, *_args):
            pass

    class Proxy(BaseHTTPRequestHandler):
        def do_POST(self):
            proxied.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    intended = ThreadingHTTPServer(("127.0.0.1", 0), Intended)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (intended, proxy)
    ]
    for thread in threads:
        thread.start()
    try:
        monkeypatch.setenv("EXOMEM_HOST", "127.0.0.1")
        monkeypatch.setenv("EXOMEM_REST_PORT", str(intended.server_port))
        monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
        service_env = tmp_path / "service.env"
        service_env.write_text("EXOMEM_REST_API_KEY=disposable-test-key\n", encoding="utf-8")
        monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(service_env))
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
        if route == "episode":
            hook._episode_revision_count("ep-test")
        else:
            key, origin = retrieve_hook._resolve_rest_key()
            assert origin == "file"
            if route == "recall":
                retrieve_hook._fetch_via_rest("test", key)
            else:
                retrieve_hook._fetch_packet_via_rest("test", key, "", 1.0)
        assert proxied == []
        assert direct == ["Bearer disposable-test-key"]
    finally:
        for server in (intended, proxy):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_a_revision_recorded_through_the_rest_door_suppresses_the_next_ask(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A recap recorded straight over REST -- a door this hook's transcript
    scan never sees -- still counts, exactly like a successful tool call."""
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "test-key")
    calls: list[dict] = []

    def fake_urlopen(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _RestResponse(_inspect_payload(1))

    monkeypatch.setattr(hook, "_open_no_redirect", fake_urlopen)

    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    results = _stops(monkeypatch, capsys, tmp_path, k)

    assert not any(_is_episode_ask(result) for result in results)
    assert calls, "the door should have been checked once the ask was about to fire"
    assert calls[0] == {
        "action": "inspect",
        "episode": episode_capture.hook_key("claude-code", SESSION),
    }

    # The counter reset there too; the next ask needs another full K turns,
    # and since the door now reports no NEW revision beyond what it already
    # saw, that one fires normally.
    more = _stops(monkeypatch, capsys, tmp_path, k)
    assert not any(_is_episode_ask(result) for result in more[:-1])
    assert _is_episode_ask(more[-1])


def test_an_unconfigured_door_falls_back_to_todays_behavior(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """No REST key resolves anywhere -- what a client that never enabled REST
    looks like -- and the ask fires exactly as it did before this door check
    existed. The hook must not even attempt a call in this case."""
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(tmp_path / "does-not-exist.env"))

    def fail_urlopen(request, timeout):
        raise AssertionError("no key resolved; the hook must not call out")

    monkeypatch.setattr(hook, "_open_no_redirect", fail_urlopen)

    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    results = _stops(monkeypatch, capsys, tmp_path, k)
    assert not any(_is_episode_ask(result) for result in results[:-1])
    assert _is_episode_ask(results[-1])


@pytest.mark.parametrize(
    "make_response",
    [
        lambda: _RestResponse(b"not json"),
        lambda: _RestResponse(_inspect_payload(0), status=500),
        lambda: (_ for _ in ()).throw(TimeoutError("timed out")),
    ],
)
def test_a_door_error_falls_back_to_todays_behavior(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    make_response,
) -> None:
    """A configured door that errors -- a bad status, a malformed body, a
    timeout -- is exactly as silent as an unconfigured one: the ask fires."""
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "test-key")

    def flaky_urlopen(request, timeout):
        return make_response()

    monkeypatch.setattr(hook, "_open_no_redirect", flaky_urlopen)

    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    results = _stops(monkeypatch, capsys, tmp_path, k)
    assert not any(_is_episode_ask(result) for result in results[:-1])
    assert _is_episode_ask(results[-1])


def test_the_door_check_stays_within_its_bounded_timeout(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A door that hangs must not stall the Stop past the bounded timeout —
    the whole reason the retrieve hook's `_bounded` join pattern is reused
    rather than a bare blocking call."""
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "test-key")
    monkeypatch.setattr(hook, "_EPISODE_DOOR_TIMEOUT_SECONDS", 0.05)

    def slow_urlopen(request, timeout):
        time.sleep(2.0)
        return _RestResponse(_inspect_payload(0))

    monkeypatch.setattr(hook, "_open_no_redirect", slow_urlopen)

    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    _stops(monkeypatch, capsys, tmp_path, k - 1)
    transcript = _transcript(tmp_path, SUBSTANTIVE, name="slow.jsonl")
    started = time.monotonic()
    result = _stop(monkeypatch, capsys, transcript)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, elapsed
    assert _is_episode_ask(result)  # the door didn't answer in time -> today's behaviour


def test_a_transcript_record_still_counts_without_consulting_the_door(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A successful `episode_memory` tool call resets the counter before the
    ask is ever about to fire, so the door check -- which only runs right
    before an otherwise-due ask -- is never reached for it."""
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "test-key")

    def fail_urlopen(request, timeout):
        raise AssertionError("a transcript-observed record needs no door check")

    monkeypatch.setattr(hook, "_open_no_redirect", fail_urlopen)

    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    _stops(monkeypatch, capsys, tmp_path, k - 1)
    recorded = _transcript(
        tmp_path,
        SUBSTANTIVE,
        tool="mcp__exomem__episode_memory",
        tool_input={"action": "record", "subject": "Harbor Lamp purchase"},
        name="r2.jsonl",
    )
    assert not _is_episode_ask(_stop(monkeypatch, capsys, recorded))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_a_blocking_service_env_stays_within_the_door_timeout(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Reading the REST key from `service.env` is part of the door check, so a
    service env that never yields (a FIFO nobody writes) is bounded too."""
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
    fifo = tmp_path / "service.env"
    os.mkfifo(fifo)
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(fifo))
    monkeypatch.setattr(hook, "_EPISODE_DOOR_TIMEOUT_SECONDS", 0.05)

    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    _stops(monkeypatch, capsys, tmp_path, k - 1)
    transcript = _transcript(tmp_path, SUBSTANTIVE, name="fifo.jsonl")
    outcome: list = []
    worker = threading.Thread(
        target=lambda: outcome.append(_stop(monkeypatch, capsys, transcript)), daemon=True
    )
    worker.start()
    worker.join(2.0)
    if worker.is_alive():
        # Release the blocked reader so the test process can exit.
        with open(fifo, "w", encoding="utf-8"):
            pass
        worker.join(2.0)
        pytest.fail("the Stop blocked on reading service.env")
    assert _is_episode_ask(outcome[0])


def _counting_door(monkeypatch: pytest.MonkeyPatch) -> dict:
    """A door whose reported revision count the test moves by hand."""
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "test-key")
    count = {"n": 0}

    def fake_urlopen(request, timeout):
        return _RestResponse(_inspect_payload(count["n"]))

    monkeypatch.setattr(hook, "_open_no_redirect", fake_urlopen)
    return count


def _recorded_transcript(tmp_path: Path, name: str) -> Path:
    return _transcript(
        tmp_path,
        SUBSTANTIVE,
        tool="mcp__exomem__episode_memory",
        tool_input={"action": "record", "subject": "Harbor Lamp purchase"},
        name=name,
    )


@pytest.mark.parametrize("continuation", [False, True], ids=["turn", "continuation"])
def test_a_record_the_hook_saw_does_not_swallow_the_next_due_ask(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    continuation: bool,
) -> None:
    """The door later reports the very record the transcript already showed.
    That is not a new revision from another door, so it must not suppress the
    next ask and stretch the cadence to 2K turns."""
    count = _counting_door(monkeypatch)
    _stop(monkeypatch, capsys, _recorded_transcript(tmp_path, "rec.jsonl"), active=continuation)
    count["n"] = 1  # that record is now in the ledger

    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    results = _stops(monkeypatch, capsys, tmp_path, k)
    assert not any(_is_episode_ask(result) for result in results[:-1])
    assert _is_episode_ask(results[-1])


def test_a_revision_after_the_ledger_count_drops_still_suppresses_the_ask(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A restored vault can report fewer revisions than were last seen. A new
    revision from another door after that must still count, not stay hidden
    until it passes the old high-water mark."""
    monkeypatch.setenv("EXOMEM_EPISODE_ASK_COOLDOWN_SEC", "0")
    count = _counting_door(monkeypatch)
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]

    count["n"] = 2
    assert not any(_is_episode_ask(result) for result in _stops(monkeypatch, capsys, tmp_path, k))

    count["n"] = 1  # the ledger was restored to an earlier state
    assert _is_episode_ask(_stops(monkeypatch, capsys, tmp_path, k)[-1])

    count["n"] = 2  # another door records a new revision
    assert not _is_episode_ask(_stops(monkeypatch, capsys, tmp_path, 1)[-1])


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
    monkeypatch.setattr(retrieve_hook, "_open_no_redirect", fake_urlopen)
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
    monkeypatch.setattr(retrieve_hook, "_open_no_redirect", fake_urlopen)
    packet = retrieve_hook._fetch_packet_via_rest(
        "continue", "key", "", 1.0, retrieve_hook.attribution("s")
    )

    assert packet == {"abstained": True, "ok": 1}
    # Stepped down one field at a time: first without the workspace key a
    # service older than it refuses, then without attribution at all.
    assert [("client" in body, "workspace" in body) for body in bodies] == [
        (True, True),
        (True, False),
        (False, False),
    ]


# --- 4.1: a committed note never covers unresolved candidates ------------------
#
# The checkpoint's candidate state is the ledger's, read through the same
# bounded REST door: attempted, pending, covered through which input revision,
# and the next step. A write or a Saved marker this turn is an attempted
# capture; it answers this turn's capture reminder and never the episode's
# coverage. Sessions that never prepared a candidate never consult the ledger.

WORKFLOW_KEY = "ep-" + "9a" * 16


def _is_coverage_ask(result: dict | None) -> bool:
    return bool(result) and result["reason"].startswith("[Exomem episode coverage]")


def _candidates_payload(
    step: str,
    *,
    execution: str = "enabled",
    attempted: int = 0,
    pending: int = 2,
    covered: int | None = None,
) -> bytes:
    return json.dumps(
        {
            "success": True,
            "data": {
                "episode": WORKFLOW_KEY,
                "input_revision": 1,
                "execution": execution,
                "coverage": {
                    "attempted": attempted,
                    "pending": pending,
                    "covered_through_input_revision": covered,
                    "historically_covered_through": None,
                    "next": step,
                    "basis": "agent_attestation",
                },
            },
        }
    ).encode("utf-8")


def _ledger_door(monkeypatch: pytest.MonkeyPatch, **payload: object) -> dict:
    """A door whose `candidates` answer the test steers; `calls` records bodies."""
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "test-key")
    ledger: dict = {"payload": payload, "calls": []}

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        ledger["calls"].append(body)
        if body["action"] == "candidates":
            return _RestResponse(_candidates_payload(**ledger["payload"]))
        return _RestResponse(_inspect_payload(0))

    monkeypatch.setattr(hook, "_open_no_redirect", fake_urlopen)
    return ledger


def _prepared(tmp_path: Path, name: str = "prepare.jsonl") -> Path:
    return _transcript(
        tmp_path,
        SUBSTANTIVE,
        tool="mcp__exomem__episode_memory",
        tool_input={"action": "prepare", "episode": WORKFLOW_KEY, "candidate": "thesis"},
        name=name,
    )


def _noted(tmp_path: Path, *, saved: bool, name: str = "note.jsonl") -> Path:
    text = ("Saved -> Knowledge Base/Notes/x.md. " + "x" * 400) if saved else SUBSTANTIVE
    return _transcript(
        tmp_path, text, tool="mcp__exomem__remember", tool_input={"title": "x"}, name=name
    )


@pytest.mark.parametrize("saved", [False, True])
def test_one_committed_note_cannot_suppress_unresolved_candidates(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    saved: bool,
) -> None:
    monkeypatch.setenv("EXOMEM_EPISODE_ASK_COOLDOWN_SEC", "0")
    ledger = _ledger_door(monkeypatch, step="decide")

    first = _stop(monkeypatch, capsys, _prepared(tmp_path))
    assert _is_coverage_ask(first) and WORKFLOW_KEY in first["reason"]
    assert ledger["calls"][-1] == {"action": "candidates", "episode": WORKFLOW_KEY}

    # A committed note (and its Saved marker) is an attempt, not coverage.
    ledger["payload"] = {"step": "resume", "attempted": 0}
    noted = _stop(monkeypatch, capsys, _noted(tmp_path, saved=saved))
    assert _is_coverage_ask(noted) and "resume" in noted["reason"]

    # Covered through the current input: the write turn is quiet again.
    ledger["payload"] = {"step": "none", "attempted": 2, "pending": 0, "covered": 1}
    assert _stop(monkeypatch, capsys, _prepared(tmp_path, "resumed.jsonl")) is None
    assert _stop(monkeypatch, capsys, _noted(tmp_path, saved=saved, name="n2.jsonl")) is None


def test_the_checkpoint_mirrors_the_ledgers_attempted_pending_and_covered_state(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _ledger_door(monkeypatch, step="attest", attempted=3, pending=1, covered=None)
    assert _is_coverage_ask(_stop(monkeypatch, capsys, _prepared(tmp_path)))

    state = hook._read_episode_state(hook._episode_state_path(SESSION))
    assert {key: state[key] for key in ("workflow_episode", "coverage_next", "attempted", "pending", "covered_through")} == {
        "workflow_episode": WORKFLOW_KEY,
        "coverage_next": "attest",
        "attempted": 3,
        "pending": 1,
        "covered_through": 0,
    }


def test_a_session_without_candidates_never_consults_the_ledger(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "test-key")

    def fail_urlopen(request, timeout):
        raise AssertionError("no candidate was ever prepared in this session")

    monkeypatch.setattr(hook, "_open_no_redirect", fail_urlopen)
    for index in range(3):
        assert _stop(monkeypatch, capsys, _noted(tmp_path, saved=bool(index % 2), name=f"{index}.jsonl")) is None


def test_disabled_execution_asks_only_for_decisions(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_EPISODE_ASK_COOLDOWN_SEC", "0")
    ledger = _ledger_door(monkeypatch, step="resume", execution="disabled")
    assert _stop(monkeypatch, capsys, _prepared(tmp_path)) is None

    ledger["payload"] = {"step": "decide", "execution": "disabled"}
    assert _is_coverage_ask(_stop(monkeypatch, capsys, _prepared(tmp_path, "p2.jsonl")))


def test_the_coverage_ask_is_bounded_by_its_cooldown(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _k, cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(hook.time, "time", lambda: clock["now"])
    ledger = _ledger_door(monkeypatch, step="decide")

    assert _is_coverage_ask(_stop(monkeypatch, capsys, _prepared(tmp_path)))
    calls = len(ledger["calls"])
    # Within the cooldown the ledger is not read again and nothing is asked.
    for index in range(3):
        assert not _is_coverage_ask(
            _stop(monkeypatch, capsys, _noted(tmp_path, saved=False, name=f"{index}.jsonl"))
        )
    assert len(ledger["calls"]) == calls
    clock["now"] += cooldown
    assert _is_coverage_ask(_stop(monkeypatch, capsys, _noted(tmp_path, saved=False)))


def test_a_covered_ledger_is_read_again_only_when_the_workflow_moves(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_EPISODE_ASK_COOLDOWN_SEC", "0")
    ledger = _ledger_door(monkeypatch, step="none", pending=0, covered=1)
    assert _stop(monkeypatch, capsys, _prepared(tmp_path)) is None
    calls = len(ledger["calls"])
    for index in range(3):
        _stop(monkeypatch, capsys, _noted(tmp_path, saved=False, name=f"{index}.jsonl"))
    assert len(ledger["calls"]) == calls

    # A workflow action answered in a continuation re-arms the check, silently.
    ledger["payload"] = {"step": "attest"}
    assert _stop(monkeypatch, capsys, _prepared(tmp_path, "cont.jsonl"), active=True) is None
    assert len(ledger["calls"]) == calls
    assert _is_coverage_ask(_stop(monkeypatch, capsys, _noted(tmp_path, saved=False)))


def test_an_unavailable_ledger_falls_back_to_todays_behavior(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_EPISODE_ASK_COOLDOWN_SEC", "0")
    monkeypatch.delenv("EXOMEM_REST_API_KEY", raising=False)
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(tmp_path / "missing.env"))
    # Preparing a candidate is itself an attempted capture: this turn is answered.
    assert _stop(monkeypatch, capsys, _prepared(tmp_path)) is None
    # With nothing known about the ledger, the write answers this turn as before.
    assert _stop(monkeypatch, capsys, _noted(tmp_path, saved=True)) is None


def test_the_checkpoint_reads_the_real_ledger_through_the_rest_door(
    home: Path,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The payload the hook parses is the one the service actually returns."""
    from starlette.testclient import TestClient

    from exomem import commands, episode_workflow, server
    from exomem import schema as schema_module
    from exomem.governance.principal import owner_principal, request_scope

    monkeypatch.setenv("EXOMEM_EPISODE_ASK_COOLDOWN_SEC", "0")
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in ("EXOMEM_UPLOAD_TOKEN", "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD"):
        monkeypatch.delenv(leaky, raising=False)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    client = TestClient(server.build_server(require_auth=False).http_app())

    def via_app(request, timeout):
        response = client.post(
            "/api/episode_memory",
            content=request.data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer sekret"},
        )
        return _RestResponse(response.content, response.status_code)

    monkeypatch.setattr(hook, "_open_no_redirect", via_app)

    def episode(**kwargs: object) -> dict:
        return commands.op_episode_memory(
            vault, schema_module.load_source_schema(vault), episode=WORKFLOW_KEY, **kwargs
        )

    with request_scope(owner_principal(surface="mcp")):
        episode(
            action="record",
            subject="Loom shed lighting",
            summary="Chose warm lamps for the shed.",
            decided=["Warm lamps light the loom shed"],
        )
        episode(
            action="prepare",
            candidate="lamps",
            proposal={
                "route": "focused_note",
                "title": "Warm shed lamps",
                "alternatives": [],
                "evidence": "complete",
                "reason": "A distinct future question.",
                "leaves": [
                    {
                        "leaf_key": "write",
                        "effect_revision": 1,
                        "kind": "create-note",
                        "args": {
                            "title": "Warm shed lamps",
                            "slug": "warm-shed-lamps",
                            "content": (
                                "## Observations\n\n- [finding] Warm lamps light the"
                                " loom shed. ^warm-shed-lamps\n"
                            ),
                            "relation_disposition": "reviewed_none",
                            "relation_review_reason": "No supported relation here.",
                        },
                    }
                ],
            },
        )
    asked = _stop(monkeypatch, capsys, _prepared(tmp_path))
    assert _is_coverage_ask(asked) and "decide" in asked["reason"]

    with request_scope(owner_principal(surface="mcp")):
        reviewed = episode(
            action="disposition", candidate="lamps", disposition="routed", reason="r"
        )
        executed = episode(
            action="resume", input_revision=1, journal_digest=reviewed["journal_digest"]
        )
    # Executed but not yet attested: a note landed, and the pass is still due.
    attest = _stop(monkeypatch, capsys, _noted(tmp_path, saved=True))
    assert _is_coverage_ask(attest) and "attest" in attest["reason"]

    with request_scope(owner_principal(surface="mcp")):
        episode(
            action="resume",
            input_revision=1,
            postcommit=True,
            journal_digest=executed["journal_digest"],
        )
    assert _stop(monkeypatch, capsys, _prepared(tmp_path, "after.jsonl")) is None


def test_the_stop_ask_bytes_are_unchanged_from_the_base() -> None:
    """Sink guidance arrives through the coverage response, never the per-turn
    Stop ask, so the injected bytes must not grow."""
    import hashlib

    ask = hook.COVERAGE_ASK
    assert len(ask) == 381
    assert (
        hashlib.sha256(ask.encode("utf-8")).hexdigest()
        == "939a5fcb7ede0e278f5feb70d054862ba8b32470664cc6058c7f93b68fb9424d"
    )
    assert "sink" not in ask and "cluster" not in ask


# --- the per-turn capture reminder fires on landings, not on reply length -----


def _is_capture_reminder(result: dict | None) -> bool:
    return bool(result) and result["reason"].startswith("[Exomem capture check]")


def _claude_turn(
    tmp_path: Path, text: str, *calls: tuple[str, dict, bool], name: str = "turn.jsonl"
) -> Path:
    """One Claude turn: each call is (tool name, input, failed)."""
    blocks = [
        {"type": "tool_use", "id": f"t{i}", "name": tool, "input": tool_input}
        for i, (tool, tool_input, _failed) in enumerate(calls)
    ]
    lines = [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "q"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [*blocks, {"type": "text", "text": text}]}},
    ]
    results = [
        {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": failed, "content": "x"}
        for i, (_tool, _input, failed) in enumerate(calls)
    ]
    if results:
        lines.append({"type": "user", "message": {"role": "user", "content": results}})
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def _codex_turn(tmp_path: Path, shape: str, command: str, *, exit_code: int | None = 0) -> Path:
    """One Codex turn running `command` as an `exec` cell or an `exec_command` call.

    `exit_code=None` is a call that returned while the command was still running."""
    if shape == "exec-cell":
        call = {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "c1",
            "input": f"text(await tools.exec_command({{cmd:{json.dumps(command)},workdir:\"/repo\"}}));",
        }
        chunk = json.dumps(
            {"chunk_id": "a1", "output": "done\n"}
            if exit_code is None
            else {"chunk_id": "a1", "exit_code": exit_code, "output": "done\n"}
        )
        output = {
            "type": "custom_tool_call_output",
            "call_id": "c1",
            "output": [
                {"type": "input_text", "text": "Script completed\nWall time 1.0 seconds\nOutput:\n"},
                {"type": "input_text", "text": chunk},
            ],
        }
    else:
        call = {
            "type": "function_call",
            "name": "exec_command",
            "call_id": "c1",
            "arguments": json.dumps({"cmd": command}),
        }
        output = {
            "type": "function_call_output",
            "call_id": "c1",
            "output": "Wall time: 0.1 seconds\nOutput:\ndone\n"
            if exit_code is None
            else f"Wall time: 0.1 seconds\nProcess exited with code {exit_code}\nOutput:\ndone\n",
        }
    user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q"}]}
    lines = [{"type": "response_item", "payload": item} for item in (user, call, output)]
    path = tmp_path / f"codex-{shape}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


@pytest.fixture
def no_episode_ask(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_EPISODE_ASK_TURNS", "0")


PUSH = ("Bash", {"command": "git push origin fix/x"}, False)


@pytest.mark.usefixtures("no_episode_ask")
class TestCaptureReminderLandingGate:
    def test_a_substantial_turn_without_a_landing_stays_silent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        reading = _claude_turn(tmp_path, SUBSTANTIVE, ("Bash", {"command": "git log --oneline && gh pr view 3"}, False))
        assert _stop(monkeypatch, capsys, reading) is None

    def test_a_landing_fires_even_with_a_short_reply(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        assert _is_capture_reminder(_stop(monkeypatch, capsys, _claude_turn(tmp_path, "Pushed.", PUSH)))

    def test_a_landing_that_also_wrote_to_the_kb_stays_silent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        turn = _claude_turn(tmp_path, "Pushed.", PUSH, ("mcp__exomem__remember", {"text": "decision"}, False))
        assert _stop(monkeypatch, capsys, turn) is None

    @pytest.mark.parametrize("shape", ["exec-cell", "exec-command"])
    def test_a_codex_landing_fires(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, shape: str
    ) -> None:
        turn = _codex_turn(tmp_path, shape, "cd /repo && git commit -am 'fix' && git push")
        assert _is_capture_reminder(_stop(monkeypatch, capsys, turn))

    @pytest.mark.parametrize("shape", ["exec-cell", "exec-command"])
    def test_a_landing_still_running_when_the_cell_returned_counts(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, shape: str
    ) -> None:
        turn = _codex_turn(tmp_path, shape, "git push", exit_code=None)
        assert _is_capture_reminder(_stop(monkeypatch, capsys, turn))

    @pytest.mark.parametrize("shape", ["claude", "exec-cell", "exec-command"])
    def test_a_failed_landing_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, shape: str
    ) -> None:
        if shape == "claude":
            turn = _claude_turn(tmp_path, SUBSTANTIVE, ("Bash", {"command": "git push"}, True))
        else:
            turn = _codex_turn(tmp_path, shape, "git push", exit_code=1)
        assert _stop(monkeypatch, capsys, turn) is None

    def test_the_most_aggressive_level_keeps_the_length_gate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
        assert _is_capture_reminder(_stop(monkeypatch, capsys, _claude_turn(tmp_path, SUBSTANTIVE)))
        assert _stop(monkeypatch, capsys, _claude_turn(tmp_path, "Short.", name="short.jsonl")) is None


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'x'",
        "git commit -n -m 'x'",
        "git merge --continue",
        "git tag v1.2",
        "git tag -a v1 -m release",
        "git tag -fam v1 note",
        "git push --force-with-lease origin x",
        "gh pr merge 7 --auto --squash",
        "timeout 120 git push",
        "timeout -k 5 2m git push",
        "env -u GIT_DIR git push",
        "if git push origin x; then echo ok; fi",
        "! git commit -m x",
        "git -C ../repo push origin main",
        "CI=1 GIT_TERMINAL_PROMPT=0 git push",
        "make test && git merge --no-ff topic",
        "git add -A; git tag -a v1 -m v1",
        "yadm commit -am sync",
        "gh pr create --fill",
        "gh -R owner/repo pr merge 7 --squash",
        "gh release create v1.0",
        "bash -lc 'pytest -q && git push'",
        'git commit -m "$(cat <<\'EOF\'\nsubject\nEOF\n)"',
    ],
)
def test_landing_commands_are_recognised(command: str) -> None:
    assert hook._landing_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "git status && git diff",
        "git commit-graph verify",
        "git merge-base main HEAD",
        "echo git push",
        "git log --grep='git commit'",
        "gh pr view 3 && gh pr checks 3",
        "git commit -m 'unbalanced",
        "git commit --dry-run",
        "git push --dry-run",
        "git push -n origin x",
        "git push --delete origin old",
        "git push origin -d old",
        "git merge --abort",
        "git merge --quit",
        "git tag",
        "git tag -l 'v*'",
        "git tag --list",
        "git tag -n",
        "git tag --contains abc123",
        "git tag -d v1",
        "git tag -v v1",
    ],
)
def test_other_commands_are_not_landings(command: str) -> None:
    assert not hook._landing_command(command)


# --- the episode ask needs a landing since the last ask or record -------------


def _landing_turn(tmp_path: Path, shape: str) -> Path:
    if shape == "claude":
        return _claude_turn(tmp_path, "Pushed.", PUSH, name="landing.jsonl")
    return _codex_turn(tmp_path, shape, "cd /repo && git commit -am 'fix' && git push")


def test_a_session_that_never_lands_work_is_never_asked(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(hook.time, "time", lambda: clock["now"])
    for _ in range(4):
        assert not any(_is_episode_ask(r) for r in _stops(monkeypatch, capsys, tmp_path, k + 2, landed=False))
        clock["now"] += 24 * 3600


@pytest.mark.parametrize("shape", ["claude", "exec-cell", "exec-command"])
def test_a_landing_makes_the_due_ask_fire(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, shape: str
) -> None:
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    assert not any(_is_episode_ask(r) for r in _stops(monkeypatch, capsys, tmp_path, k, landed=False))

    assert _is_episode_ask(_stop(monkeypatch, capsys, _landing_turn(tmp_path, shape)))


def test_a_landing_before_the_cadence_is_due_waits_for_it(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    assert _stop(monkeypatch, capsys, _claude_turn(tmp_path, "Pushed.", PUSH, name="early.jsonl")) is not None
    results = _stops(monkeypatch, capsys, tmp_path, k, landed=False)
    assert not any(_is_episode_ask(r) for r in results[:-1])
    assert _is_episode_ask(results[-1])


def test_an_ask_answered_without_a_record_waits_for_a_new_landing(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    k, cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(hook.time, "time", lambda: clock["now"])
    assert _is_episode_ask(_stops(monkeypatch, capsys, tmp_path, k)[-1])

    for _ in range(3):
        clock["now"] += cooldown
        assert not any(_is_episode_ask(r) for r in _stops(monkeypatch, capsys, tmp_path, 3, landed=False))

    assert _is_episode_ask(_stops(monkeypatch, capsys, tmp_path, 1)[-1])


def test_a_landing_on_the_asking_turn_does_not_count_twice(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The continuation that answers the ask re-reads the same turn, landing included."""
    k, cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(hook.time, "time", lambda: clock["now"])
    landed = _claude_turn(tmp_path, SUBSTANTIVE, PUSH, ("mcp__exomem__remember", {"text": "n"}, False))
    _stops(monkeypatch, capsys, tmp_path, k - 1, landed=False)
    assert _is_episode_ask(_stop(monkeypatch, capsys, landed))
    assert _stop(monkeypatch, capsys, landed, active=True) is None

    clock["now"] += cooldown
    assert not any(_is_episode_ask(r) for r in _stops(monkeypatch, capsys, tmp_path, 2, landed=False))


def test_a_record_covers_the_landings_before_it(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    _stops(monkeypatch, capsys, tmp_path, k - 1)  # landed, not yet due
    assert not _is_episode_ask(_stop(monkeypatch, capsys, _recorded_transcript(tmp_path, "rec.jsonl")))

    assert not any(_is_episode_ask(r) for r in _stops(monkeypatch, capsys, tmp_path, k + 1, landed=False))
    results = _stops(monkeypatch, capsys, tmp_path, 1)
    assert _is_episode_ask(results[-1])


def test_a_record_made_in_the_continuation_covers_the_landings_before_it(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    assert _stop(monkeypatch, capsys, _claude_turn(tmp_path, "Pushed.", PUSH, name="early.jsonl")) is not None
    _stop(monkeypatch, capsys, _recorded_transcript(tmp_path, "rec.jsonl"), active=True)

    assert not any(_is_episode_ask(r) for r in _stops(monkeypatch, capsys, tmp_path, k + 2, landed=False))


def test_a_revision_recorded_through_the_rest_door_covers_the_landings_before_it(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    count = _counting_door(monkeypatch)
    k, _cooldown = hook._EPISODE_ASK_PRESETS["balanced"]
    count["n"] = 1  # another door recorded; the hook has not seen it yet
    assert not any(_is_episode_ask(r) for r in _stops(monkeypatch, capsys, tmp_path, k))  # re-base on the due check

    assert not any(_is_episode_ask(r) for r in _stops(monkeypatch, capsys, tmp_path, k + 2, landed=False))
