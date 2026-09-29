"""The retrieve hook's local-listener rung (`add-authenticated-local-ingress`).

When a local client token file and a local port are configured, the REST-first
rung posts to the managed service's loopback-only local listener with that
token before anything else, and never to `EXOMEM_HOST`. For one release a
failed local request still falls through to the lifted-key request. The final
test runs the shipped script as a subprocess over real loopback HTTP.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import exomem

RETRIEVE_SCRIPT = Path(exomem.__file__).parent / "_hooks" / "exomem_retrieve_nudge.py"
LOCAL_TOKEN = "exo_s1.synthetic-local-client-token"
HITS = [{"path": "Notes/local.md", "type": "note", "updated": "2026-09-01"}]


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("exomem_retrieve_local_under_test", RETRIEVE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook_module()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in (
        "EXOMEM_RETRIEVE_INJECT",
        "EXOMEM_RETRIEVE_INJECT_CLI",
        "EXOMEM_REST_API_KEY",
        "EXOMEM_REST_PORT",
        "EXOMEM_LOCAL_TOKEN_FILE",
        "EXOMEM_LOCAL_PORT",
        "EXOMEM_HOST",
        "XDG_CONFIG_HOME",
        "KB_RETRIEVE_INJECT",
        "KB_RETRIEVE_INJECT_CLI",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(tmp_path / "absent-service.env"))


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self.body = status, body

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _token_file(tmp_path: Path, token: str = LOCAL_TOKEN) -> Path:
    path = tmp_path / "home.token"
    path.write_text(f"{token}\n")
    return path


def _record_requests(monkeypatch: pytest.MonkeyPatch, answer) -> list[tuple[str, str]]:  # noqa: ANN001
    seen: list[tuple[str, str]] = []

    def fake_open(request, timeout=None):  # noqa: ANN001
        authorization = dict(request.header_items())["Authorization"]
        seen.append((request.full_url, authorization))
        return answer(request)

    monkeypatch.setattr(hook, "_open_no_redirect", fake_open)
    return seen


def _ok(request) -> _Response:  # noqa: ANN001
    return _Response(200, json.dumps({"success": True, "data": HITS}).encode())


def test_the_local_listener_answers_first_with_the_local_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_LOCAL_TOKEN_FILE", str(_token_file(tmp_path)))
    monkeypatch.setenv("EXOMEM_LOCAL_PORT", "8764")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "lifted-owner-key")
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", "1")
    monkeypatch.setattr(hook, "_fetch_via_cli", lambda *a, **k: pytest.fail("CLI must not run"))
    seen = _record_requests(monkeypatch, _ok)

    assert hook._gather_hits_with_lane("what did I conclude?") == (HITS, "local")
    assert seen == [("http://127.0.0.1:8764/api/ask_memory", f"Bearer {LOCAL_TOKEN}")]


def test_a_failed_local_request_falls_back_to_the_lifted_key_for_one_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_LOCAL_TOKEN_FILE", str(_token_file(tmp_path)))
    monkeypatch.setenv("EXOMEM_LOCAL_PORT", "8764")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "lifted-owner-key")

    def answer(request) -> _Response:  # noqa: ANN001
        if ":8764/" in request.full_url:
            raise ConnectionRefusedError("supervisor predates the local listener")
        return _ok(request)

    seen = _record_requests(monkeypatch, answer)

    assert hook._gather_hits_with_lane("prompt") == (HITS, "rest")
    assert seen == [
        ("http://127.0.0.1:8764/api/ask_memory", f"Bearer {LOCAL_TOKEN}"),
        ("http://127.0.0.1:8765/api/ask_memory", "Bearer lifted-owner-key"),
    ]


def test_the_local_rung_never_follows_exomem_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_LOCAL_TOKEN_FILE", str(_token_file(tmp_path)))
    monkeypatch.setenv("EXOMEM_LOCAL_PORT", "8764")
    monkeypatch.setenv("EXOMEM_HOST", "evil.example")
    seen = _record_requests(monkeypatch, _ok)

    assert hook._gather_hits_with_lane("prompt")[1] == "local"
    assert [url for url, _ in seen] == ["http://127.0.0.1:8764/api/ask_memory"]


def test_the_local_port_can_come_from_the_managed_service_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service_env = tmp_path / "service.env"
    service_env.write_text('EXOMEM_REST_API_KEY="k"\nEXOMEM_LOCAL_PORT="9764"\n')
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(service_env))
    monkeypatch.setenv("EXOMEM_LOCAL_TOKEN_FILE", str(_token_file(tmp_path)))
    seen = _record_requests(monkeypatch, _ok)

    assert hook._gather_hits_with_lane("prompt")[1] == "local"
    assert seen[0][0] == "http://127.0.0.1:9764/api/ask_memory"


@pytest.mark.parametrize(
    ("token", "port"),
    [(None, "8764"), ("", "8764"), ("two words", "8764"), (LOCAL_TOKEN, None), (LOCAL_TOKEN, "0"),
     (LOCAL_TOKEN, "port"), (LOCAL_TOKEN, "70000")],
)
def test_without_a_usable_token_and_port_the_local_rung_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str | None, port: str | None
) -> None:
    if token is not None:
        monkeypatch.setenv("EXOMEM_LOCAL_TOKEN_FILE", str(_token_file(tmp_path, token)))
    else:
        monkeypatch.setenv("EXOMEM_LOCAL_TOKEN_FILE", str(tmp_path / "missing.token"))
    if port is not None:
        monkeypatch.setenv("EXOMEM_LOCAL_PORT", port)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "lifted-owner-key")
    seen = _record_requests(monkeypatch, _ok)

    assert hook._gather_hits_with_lane("prompt")[1] == "rest"
    assert [url for url, _ in seen] == ["http://127.0.0.1:8765/api/ask_memory"]


def test_working_set_mode_uses_the_same_local_rung(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_LOCAL_TOKEN_FILE", str(_token_file(tmp_path)))
    monkeypatch.setenv("EXOMEM_LOCAL_PORT", "8764")
    packet = {"abstained": True, "abstention": {"reason": "no_anchor"}}
    seen = _record_requests(
        monkeypatch,
        lambda request: _Response(200, json.dumps({"success": True, "data": packet}).encode()),
    )

    assert hook._gather_packet_with_lane("turn", "") == (packet, "local")
    assert seen == [("http://127.0.0.1:8764/api/activate_context", f"Bearer {LOCAL_TOKEN}")]


def test_the_shipped_script_injects_hits_from_the_local_listener(tmp_path: Path) -> None:
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            length = int(self.headers.get("content-length") or 0)
            self.rfile.read(length)
            requests.append((self.path, self.headers.get("authorization", "")))
            if self.headers.get("authorization") != f"Bearer {LOCAL_TOKEN}":
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps({"success": True, "data": HITS}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    local = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=local.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "EXOMEM_HOOK_HOME": str(home),
        "EXOMEM_HOOK_CLIENT": "claude",
        "EXOMEM_RETRIEVE_INJECT": "1",
        "EXOMEM_LOCAL_TOKEN_FILE": str(_token_file(tmp_path)),
        "EXOMEM_LOCAL_PORT": str(local.server_address[1]),
        "EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC": "0",
        "EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC": "0",
        "EXOMEM_SERVICE_ENV": str(tmp_path / "absent-service.env"),
        "EXOMEM_CONFIG_PATH": str(tmp_path / "absent-config.json"),
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(RETRIEVE_SCRIPT)],
            input=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "local-rung",
                    "prompt": "What did I conclude about the local listener rollout?",
                }
            ),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
    finally:
        local.shutdown()
        thread.join(5)
    assert proc.returncode == 0, proc.stderr
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Notes/local.md" in context
    assert requests == [("/api/ask_memory", f"Bearer {LOCAL_TOKEN}")]
    assert LOCAL_TOKEN not in proc.stdout + proc.stderr
