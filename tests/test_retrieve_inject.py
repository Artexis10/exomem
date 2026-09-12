"""`exomem_retrieve_nudge.py`'s opt-in retrieve-and-inject upgrade (EXOMEM_RETRIEVE_INJECT).

The hook is a standalone, stdlib-only script — not a package member — so it is
loaded via `importlib.util.spec_from_file_location` (matching how
`tests/test_install_hook.py` resolves `RETRIEVE_SCRIPT` for its subprocess
tests) to get an in-process module whose seam functions
(`_fetch_via_rest` / `_fetch_via_cli` / the stdlib `urllib.request.urlopen` /
`subprocess.run` / `shutil.which`) can be monkeypatched — the same seam
precedent as `doctor_module._probe_get` in `tests/test_doctor_probe.py`. No
real network request or subprocess is ever spawned by this suite; the one
exception is the final subprocess-level black-box check, which spawns the
*script itself* (like `test_install_hook.py` already does) purely to prove the
default-off path is untouched end-to-end.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.request as urllib_request
from pathlib import Path

import pytest

import exomem

RETRIEVE_SCRIPT = Path(exomem.__file__).parent / "_hooks" / "exomem_retrieve_nudge.py"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("exomem_retrieve_nudge_under_test", RETRIEVE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook_mod = _load_hook_module()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every test starts from a known-clean env — a real EXOMEM_REST_API_KEY or
    EXOMEM_RETRIEVE_INJECT already set on the host machine must never leak in. The
    legacy KB_RETRIEVE_* names are cleared too: they still alias onto the EXOMEM_*
    names at startup (back-compat), so a host-set old name would leak just the same."""
    for var in (
        "EXOMEM_RETRIEVE_NUDGE_DISABLE",
        "EXOMEM_RETRIEVE_NUDGE_MIN_CHARS",
        "EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS",
        "EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC",
        "EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC",
        "EXOMEM_RETRIEVE_INJECT",
        "EXOMEM_RETRIEVE_INJECT_CLI",
        "EXOMEM_REST_API_KEY",
        "EXOMEM_REST_PORT",
        "EXOMEM_HOST",
        "EXOMEM_SERVICE_ENV",
        "EXOMEM_PROMINENCE",
        "XDG_CONFIG_HOME",
        # Legacy aliases (still honored via _normalize_env_aliases).
        "KB_RETRIEVE_NUDGE_DISABLE",
        "KB_RETRIEVE_NUDGE_MIN_CHARS",
        "KB_RETRIEVE_NUDGE_CONTROL_MAX_CHARS",
        "KB_RETRIEVE_NUDGE_COOLDOWN_SEC",
        "KB_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC",
        "KB_RETRIEVE_INJECT",
        "KB_RETRIEVE_INJECT_CLI",
    ):
        monkeypatch.delenv(var, raising=False)
    # The key fallback reads the managed install's `service.env`. Point it at an
    # absent temp file for every test so a real key on the host never leaks in.
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(tmp_path / "absent-service.env"))


class _FakeResponse:
    """Minimal stand-in for the context-managed object `urllib.request.urlopen`
    returns: `.getcode()` + `.read()`, usable in a `with ... as resp:` block."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _envelope_bytes(hits: list[dict]) -> bytes:
    return json.dumps({"success": True, "data": hits}).encode("utf-8")


def _call_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, event: dict, home: Path) -> str:
    """Invoke the loaded module's `main()` in-process with a fake stdin and HOME,
    so cooldown/log state lands under `home` and seam monkeypatches apply."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(hook_mod.sys, "stdin", io.StringIO(json.dumps(event)))
    hook_mod.main()
    return capsys.readouterr().out


# --- truthy-parse (_env_flag) ----------------------------------------------------


@pytest.mark.parametrize("value", ["", "0", "false", "FALSE", "No", "OFF", "off"])
def test_env_flag_falsy_values_are_disabled(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", value)
    assert hook_mod._env_flag("EXOMEM_RETRIEVE_INJECT") is False


def test_env_flag_unset_is_disabled() -> None:
    assert hook_mod._env_flag("EXOMEM_RETRIEVE_INJECT_DOES_NOT_EXIST") is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "anything"])
def test_env_flag_truthy_values_are_enabled(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", value)
    assert hook_mod._env_flag("EXOMEM_RETRIEVE_INJECT_CLI") is True


# --- prompt relevance gate ------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "perfect merge then to main gj",
        "are you done?",
        "restart the server",
        "continue",
        "status?",
    ],
)
def test_obvious_control_prompts_are_skipped(prompt: str) -> None:
    assert hook_mod._is_obvious_control_prompt(prompt, max_chars=180) is True


@pytest.mark.parametrize(
    "event",
    [
        {"hook_event_name": "task-notification", "prompt": "A task completed."},
        {"hook_event_name": "task_notification", "prompt": "A task completed."},
        {"hook_event_name": "stop-hook", "prompt": "Stop hook control."},
        {"stop_hook_active": True, "prompt": "Stop hook control."},
    ],
)
def test_top_level_task_control_events_are_skipped_before_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
    event: dict,
) -> None:
    monkeypatch.setenv("EXOMEM_RETRIEVE_NUDGE_MIN_CHARS", "0")
    monkeypatch.setattr(
        hook_mod,
        "_touch",
        lambda stamp: (_ for _ in ()).throw(AssertionError("cooldown touched")),
    )
    monkeypatch.setattr(
        hook_mod,
        "_fetch_via_rest",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("retrieval attempted")),
    )
    assert _call_main(monkeypatch, capsys, event, tmp_path / "home") == ""


@pytest.mark.parametrize(
    "prompt",
    [
        "what does task-notification mean here?",
        "Please investigate the stop-hook behavior in the current repo.",
        "Stop hook handling is broken; fix the current repo.",
    ],
)
def test_notification_terms_in_ordinary_questions_are_not_suppressed(prompt: str) -> None:
    assert hook_mod._is_task_control_event({"prompt": prompt}) is False


def test_anchored_task_notification_text_is_suppressed() -> None:
    assert hook_mod._is_task_control_event({"prompt": "<task-notification> completed"}) is True


def test_stop_hook_feedback_text_is_suppressed() -> None:
    assert hook_mod._is_task_control_event({"prompt": "Stop hook feedback: completed"}) is True


def test_stub_header_gives_diagnostic_verification_guidance() -> None:
    header = hook_mod._STUB_HEADER.lower()
    assert header.startswith("kb routing stubs"), "Keep the Track C payload marker stable"
    assert "read_memory" in header
    assert "first relevant" in header
    assert "before investigating" in header
    assert "current repo" in header
    assert "evidence" in header
    assert "not instructions" in header


@pytest.mark.parametrize(
    "prompt",
    [
        "what did I conclude about the kb hook design earlier?",
        "save this to the knowledge base",
        "Have I looked at this Exomem decision before?",
        "去年このプロジェクトについて何を結論づけましたか？詳しく教えてください。",
    ],
)
def test_kb_bearing_or_non_english_prompts_are_not_control_skipped(prompt: str) -> None:
    assert hook_mod._is_obvious_control_prompt(prompt, max_chars=180) is False


# --- _format_inject_block ---------------------------------------------------------


def test_format_inject_block_empty_for_no_hits() -> None:
    assert hook_mod._format_inject_block([]) == ""


def test_format_inject_block_one_line_per_hit_in_order() -> None:
    hits = [
        {"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"},
        {"path": "Notes/b.md", "type": "insight", "updated": "2026-01-02"},
    ]
    block = hook_mod._format_inject_block(hits)
    lines = block.splitlines()
    assert lines[0] == hook_mod._STUB_HEADER
    assert lines[1] == "- Notes/a.md (note, 2026-01-01)"
    assert lines[2] == "- Notes/b.md (insight, 2026-01-02)"


def test_format_inject_block_caps_at_three_hits() -> None:
    hits = [{"path": f"Notes/{i}.md", "type": "note", "updated": "2026-01-01"} for i in range(5)]
    block = hook_mod._format_inject_block(hits)
    assert len([ln for ln in block.splitlines() if ln.startswith("- ")]) == 3


def test_format_inject_block_omits_missing_fields_gracefully() -> None:
    block = hook_mod._format_inject_block([{"path": "Notes/only-path.md"}])
    assert block.splitlines()[1] == "- Notes/only-path.md"


def test_format_inject_block_drops_whole_lines_and_never_cuts_a_path() -> None:
    long_path = "Knowledge Base/" + ("very-long-segment-" * 15) + "note.md"
    hits = [{"path": long_path, "type": "note", "updated": "2026-01-01"} for _ in range(3)]
    block = hook_mod._format_inject_block(hits)
    assert len(block) <= hook_mod._STUB_BLOCK_MAX_CHARS
    lines = block.splitlines()
    assert lines[0] == hook_mod._STUB_HEADER
    stub_lines = [ln for ln in lines[1:] if not ln.startswith("- …")]
    assert stub_lines, "at least one whole stub line must survive"
    for ln in stub_lines:
        assert ln == f"- {long_path} (note, 2026-01-01)", "a stub line was cut"
    assert lines[-1] == hook_mod._STUB_OMITTED_LINE.format(n=3 - len(stub_lines))


def test_format_inject_block_three_readable_filenames_fit_whole() -> None:
    hits = [
        {"path": f"Notes/{'a-readable-slug-' * 4}{i}.md", "type": "insight", "updated": "2026-09-11T19:27:00Z"}
        for i in range(3)
    ]
    block = hook_mod._format_inject_block(hits)
    assert len([ln for ln in block.splitlines() if ln.startswith("- Notes")]) == 3
    assert "more not shown" not in block
    assert len(block) <= hook_mod._STUB_BLOCK_MAX_CHARS


def test_format_inject_block_is_empty_when_not_even_one_line_fits() -> None:
    hits = [{"path": "x" * (hook_mod._STUB_BLOCK_MAX_CHARS + 10), "type": "note"}]
    assert hook_mod._format_inject_block(hits) == ""


def test_format_inject_block_never_contains_excerpt_text() -> None:
    hits = [{
        "path": "Notes/a.md", "type": "note", "updated": "2026-01-01",
        "excerpt": "SENTINEL_EXCERPT_TEXT_SHOULD_NEVER_APPEAR",
    }]
    block = hook_mod._format_inject_block(hits)
    assert "SENTINEL_EXCERPT_TEXT_SHOULD_NEVER_APPEAR" not in block
    assert "excerpt" not in block.lower()


# --- REST seam (_fetch_via_rest) --------------------------------------------------


def test_fetch_via_rest_success_returns_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(200, _envelope_bytes(hits))

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    result = hook_mod._fetch_via_rest("what did I conclude about X?", "secret-key")

    assert result == hits
    assert captured["url"] == "http://127.0.0.1:8765/api/ask_memory"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["body"] == {
        "query": "what did I conclude about X?",
        "detail": "compact",
        "limit": 3,
        "mode": "hybrid",
    }


def test_fetch_via_rest_respects_exomem_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_HOST", "10.0.0.5")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResponse(200, _envelope_bytes([]))

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    hook_mod._fetch_via_rest("prompt", "key")
    assert captured["url"] == "http://10.0.0.5:8765/api/ask_memory"


@pytest.mark.parametrize(
    "value, expected",
    [("9123", 9123), ("0", None), ("65536", None), ("bad", None), ("", 8765)],
)
def test_rest_port_uses_only_valid_environment_override(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: int
) -> None:
    monkeypatch.setenv("EXOMEM_REST_PORT", value)
    assert hook_mod._rest_port() == expected


def test_fetch_via_rest_uses_valid_port_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_REST_PORT", "9123")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResponse(200, _envelope_bytes([]))

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    hook_mod._fetch_via_rest("prompt", "key")
    assert captured["url"] == "http://127.0.0.1:9123/api/ask_memory"


def test_fetch_via_rest_rejects_invalid_port_without_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_REST_PORT", "bad")
    monkeypatch.setattr(urllib_request, "urlopen", lambda *args: (_ for _ in ()).throw(AssertionError("request attempted")))
    assert hook_mod._fetch_via_rest("prompt", "key") is None


def test_fetch_via_rest_success_false_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(200, json.dumps({"success": False, "error": {}}).encode())

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    assert hook_mod._fetch_via_rest("prompt", "key") is None


def test_fetch_via_rest_non_200_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(500, b"internal error")

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    assert hook_mod._fetch_via_rest("prompt", "key") is None


def test_fetch_via_rest_malformed_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(200, b"{not json")

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    assert hook_mod._fetch_via_rest("prompt", "key") is None


def test_fetch_via_rest_connection_error_returns_none_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    assert hook_mod._fetch_via_rest("prompt", "key") is None


def test_fetch_via_rest_timeout_returns_none_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    assert hook_mod._fetch_via_rest("prompt", "key") is None


# --- CLI seam (_fetch_via_cli) ----------------------------------------------------


def test_fetch_via_cli_success_invokes_expected_command(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]
    captured: dict = {}

    monkeypatch.setattr(hook_mod.shutil, "which", lambda name: "/usr/local/bin/exomem" if name == "exomem" else None)

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"success": True, "data": hits}), stderr="")

    monkeypatch.setattr(hook_mod.subprocess, "run", fake_run)
    result = hook_mod._fetch_via_cli("find my thing")

    assert result == hits
    assert captured["cmd"] == [
        "/usr/local/bin/exomem", "ask_memory",
        "--detail", "compact",
        "--limit", "3",
        "--mode", "hybrid",
        "--json", "find my thing",
    ]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True


def test_fetch_via_cli_falls_back_to_kb_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        hook_mod.shutil, "which",
        lambda name: "/usr/local/bin/kb" if name == "kb" else None,
    )
    monkeypatch.setattr(
        hook_mod.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"success": True, "data": []}), stderr=""),
    )
    assert hook_mod._fetch_via_cli("prompt") == []


def test_fetch_via_cli_neither_script_resolvable_returns_none_without_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hook_mod.shutil, "which", lambda name: None)

    def _boom(cmd, **kwargs):
        raise AssertionError("subprocess.run must not be called when no console script resolves")

    monkeypatch.setattr(hook_mod.subprocess, "run", _boom)
    assert hook_mod._fetch_via_cli("prompt") is None


def test_fetch_via_cli_non_zero_exit_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook_mod.shutil, "which", lambda name: "/usr/local/bin/exomem")
    monkeypatch.setattr(
        hook_mod.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
    )
    assert hook_mod._fetch_via_cli("prompt") is None


def test_fetch_via_cli_malformed_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook_mod.shutil, "which", lambda name: "/usr/local/bin/exomem")
    monkeypatch.setattr(
        hook_mod.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="{not json", stderr=""),
    )
    assert hook_mod._fetch_via_cli("prompt") is None


def test_fetch_via_cli_timeout_returns_none_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook_mod.shutil, "which", lambda name: "/usr/local/bin/exomem")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 5.0))

    monkeypatch.setattr(hook_mod.subprocess, "run", fake_run)
    assert hook_mod._fetch_via_cli("prompt") is None


# --- ladder decision (_gather_hits) -----------------------------------------------


def test_gather_hits_rest_reachable_never_calls_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "secret")
    monkeypatch.setattr(hook_mod, "_fetch_via_rest", lambda prompt, api_key, **kw: hits)

    def _boom(*a, **kw):
        raise AssertionError("_fetch_via_cli must not be called when REST succeeds")

    monkeypatch.setattr(hook_mod, "_fetch_via_cli", _boom)
    assert hook_mod._gather_hits_with_lane("prompt")[0] == hits


def test_gather_hits_rest_failing_cli_unset_is_nudge_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "secret")
    monkeypatch.setattr(hook_mod, "_fetch_via_rest", lambda prompt, api_key, **kw: None)

    def _boom(*a, **kw):
        raise AssertionError("_fetch_via_cli must not be called when EXOMEM_RETRIEVE_INJECT_CLI is unset")

    monkeypatch.setattr(hook_mod, "_fetch_via_cli", _boom)
    assert hook_mod._gather_hits_with_lane("prompt")[0] == []


def test_gather_hits_rest_unconfigured_cli_opted_in_uses_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [{"path": "Notes/b.md", "type": "insight", "updated": "2026-01-02"}]
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", "1")

    def _boom(*a, **kw):
        raise AssertionError("_fetch_via_rest must not be called when EXOMEM_REST_API_KEY is unset")

    monkeypatch.setattr(hook_mod, "_fetch_via_rest", _boom)
    monkeypatch.setattr(hook_mod, "_fetch_via_cli", lambda prompt, **kw: hits)
    assert hook_mod._gather_hits_with_lane("prompt")[0] == hits


def test_gather_hits_neither_configured_calls_neither_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **kw):
        raise AssertionError("no transport seam should be called when neither is configured")

    monkeypatch.setattr(hook_mod, "_fetch_via_rest", _boom)
    monkeypatch.setattr(hook_mod, "_fetch_via_cli", _boom)
    assert hook_mod._gather_hits_with_lane("prompt")[0] == []


# --- hit envelopes (_parse_hits) --------------------------------------------------


def test_parse_hits_accepts_list_envelope() -> None:
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]
    assert hook_mod._parse_hits({"success": True, "data": hits}) == hits


def test_parse_hits_accepts_marked_rest_envelope() -> None:
    """The REST facade wraps the hits in `data: {hits, ...}` whenever it attaches
    a marker (`degraded`, `warming`, timings, ...), in any mode; otherwise `data`
    is the bare list. The lane must read either shape instead of falling through
    to the CLI and paying a second search."""
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]
    payload = {"success": True, "data": {"hits": hits, "degraded": ["clip"]}}
    assert hook_mod._parse_hits(payload) == hits


def test_parse_hits_rejects_dict_without_hits() -> None:
    assert hook_mod._parse_hits({"success": True, "data": {"degraded": []}}) is None


def test_fetch_via_rest_reads_marked_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]
    body = json.dumps({"success": True, "data": {"hits": hits, "degraded": []}}).encode("utf-8")
    monkeypatch.setattr(urllib_request, "urlopen", lambda req, timeout=None: _FakeResponse(200, body))
    assert hook_mod._fetch_via_rest("prompt", "key") == hits


def test_fetch_via_rest_passes_the_rest_timeout_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hybrid recall uses the existing REST socket timeout."""
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _FakeResponse(200, _envelope_bytes([]))

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    hook_mod._fetch_via_rest("prompt", "key")
    assert captured["timeout"] == hook_mod.REST_TIMEOUT_SECONDS
    assert hook_mod.REST_TIMEOUT_SECONDS > 2.0


# --- REST key resolution (_rest_api_key) ------------------------------------------


def _write_service_env(path: Path, key_line: str) -> None:
    path.write_text(
        "EXOMEM_VAULT_PATH=some vault\n"
        f"{key_line}\n"
        "EXOMEM_JWT_SIGNING_KEY=other-secret\n",
        encoding="utf-8",
    )


def test_rest_api_key_prefers_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "service.env"
    _write_service_env(env_file, "EXOMEM_REST_API_KEY=from-file")
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(env_file))
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "from-env")
    assert hook_mod._rest_api_key() == "from-env"


@pytest.mark.parametrize(
    "key_line, expected",
    [
        ("EXOMEM_REST_API_KEY=plain-key", "plain-key"),
        ('EXOMEM_REST_API_KEY="double quoted"', "double quoted"),
        ("EXOMEM_REST_API_KEY='single quoted'", "single quoted"),
        ("  EXOMEM_REST_API_KEY = spaced ", "spaced"),
    ],
)
def test_rest_api_key_falls_back_to_the_managed_service_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key_line: str, expected: str,
) -> None:
    """Issue #1142: on a managed install the key lives only in the service's
    EnvironmentFile, so the client shell never carries it and the REST lane
    never ran. Read it the way `scripts/_service-common.sh` does: first
    `NAME=` line, one layer of matching quotes stripped."""
    env_file = tmp_path / "service.env"
    _write_service_env(env_file, key_line)
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(env_file))
    assert hook_mod._rest_api_key() == expected


def test_rest_api_key_ignores_other_keys_and_comments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_text(
        "# EXOMEM_REST_API_KEY=commented-out\n"
        "EXOMEM_REST_API_KEY_OLD=not-this\n"
        "EXOMEM_JWT_SIGNING_KEY=not-this-either\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(env_file))
    assert hook_mod._rest_api_key() == ""


def test_rest_api_key_absent_file_is_empty_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(tmp_path / "nowhere" / "service.env"))
    assert hook_mod._rest_api_key() == ""


def test_service_env_path_defaults_to_xdg_config_on_posix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EXOMEM_SERVICE_ENV", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(hook_mod.os, "name", "posix")
    monkeypatch.setattr(hook_mod.sys, "platform", "linux")
    assert hook_mod._service_env_path() == tmp_path / "xdg" / "exomem" / "service.env"


def test_service_env_path_on_macos_uses_application_support(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EXOMEM_SERVICE_ENV", raising=False)
    monkeypatch.setattr(hook_mod.os, "name", "posix")
    monkeypatch.setattr(hook_mod.sys, "platform", "darwin")
    monkeypatch.setattr(hook_mod.Path, "home", classmethod(lambda cls: tmp_path / "mac-home"))
    assert hook_mod._service_env_path() == (
        tmp_path / "mac-home" / "Library" / "Application Support" / "Exomem" / "service.env"
    )


def test_service_env_path_is_none_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_SERVICE_ENV", raising=False)
    monkeypatch.setattr(hook_mod.os, "name", "nt")
    assert hook_mod._service_env_path() is None
    assert hook_mod._rest_api_key() == ""


def test_rest_api_key_reverses_the_installers_double_quote_escaping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`install-service.sh` renders values through `systemd_quote`: double
    quotes around the value, backslash and double quote escaped inside. The
    shell reader strips only the quotes; this one also undoes the escaping, so a
    key containing either character is the bearer the service expects."""
    env_file = tmp_path / "service.env"
    env_file.write_text('EXOMEM_REST_API_KEY="a\\"b\\\\c"\n', encoding="utf-8")
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(env_file))
    assert hook_mod._rest_api_key() == 'a"b\\c'


def test_rest_api_key_survives_a_byte_order_mark(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "service.env"
    env_file.write_bytes("\ufeffEXOMEM_REST_API_KEY=first-line-key\n".encode("utf-8"))
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(env_file))
    assert hook_mod._rest_api_key() == "first-line-key"


# --- a key read from disk only travels to loopback ------------------------------


def _file_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "service.env"
    _write_service_env(env_file, "EXOMEM_REST_API_KEY=from-file")
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(env_file))


def test_file_sourced_key_never_leaves_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two attacker-set variables (`EXOMEM_HOST` and `EXOMEM_RETRIEVE_INJECT`)
    must not turn the hook into a primitive that posts a key it read off disk,
    plus the prompt, to a host of the attacker's choosing."""
    _file_key(monkeypatch, tmp_path)
    monkeypatch.setenv("EXOMEM_HOST", "10.0.0.5")
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", "1")
    hits = [{"path": "Notes/c.md", "type": "note", "updated": "2026-01-03"}]

    def _boom(*a, **kw):
        raise AssertionError("REST must not be attempted with a file-sourced key and a non-loopback host")

    monkeypatch.setattr(hook_mod, "_fetch_via_rest", _boom)
    monkeypatch.setattr(hook_mod, "_fetch_via_cli", lambda prompt, **kw: hits)
    assert hook_mod._gather_hits_with_lane("prompt") == (hits, "cli")


def test_file_sourced_key_with_non_loopback_host_and_no_cli_is_the_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _file_key(monkeypatch, tmp_path)
    monkeypatch.setenv("EXOMEM_HOST", "evil.example/collect?x=")

    def _boom(*a, **kw):
        raise AssertionError("no transport may run")

    monkeypatch.setattr(hook_mod, "_fetch_via_rest", _boom)
    monkeypatch.setattr(hook_mod, "_fetch_via_cli", _boom)
    assert hook_mod._gather_hits_with_lane("prompt") == ([], "none")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", ""])
def test_file_sourced_key_reaches_loopback_hosts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, host: str) -> None:
    _file_key(monkeypatch, tmp_path)
    if host:
        monkeypatch.setenv("EXOMEM_HOST", host)
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]
    monkeypatch.setattr(hook_mod, "_fetch_via_rest", lambda prompt, api_key, **kw: hits)
    assert hook_mod._gather_hits_with_lane("prompt") == (hits, "rest")


def test_env_sourced_key_keeps_the_configured_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user who exported the key into the client environment also chose the
    host; that path is unchanged."""
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "from-env")
    monkeypatch.setenv("EXOMEM_HOST", "10.0.0.5")
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]
    seen: dict = {}

    def fake_rest(prompt, api_key, **kw):
        seen["api_key"] = api_key
        return hits

    monkeypatch.setattr(hook_mod, "_fetch_via_rest", fake_rest)
    assert hook_mod._gather_hits_with_lane("prompt") == (hits, "rest")
    assert seen["api_key"] == "from-env"


# --- one wall-clock budget across both rungs ------------------------------------


def test_gather_hits_hands_each_rung_the_remaining_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "key")
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", "1")
    seen: dict = {}
    # start, before REST, before CLI (REST took 3.5 s); the last value repeats because
    # `hook_mod.time` is the shared stdlib module and pytest reads the clock too.
    ticks = [100.0, 100.0, 103.5]
    monkeypatch.setattr(hook_mod.time, "monotonic", lambda: ticks.pop(0) if len(ticks) > 1 else ticks[0])

    def fake_rest(prompt, api_key, **kw):
        seen["rest_timeout"] = kw["timeout"]
        return None

    def fake_cli(prompt, **kw):
        seen["cli_timeout"] = kw["timeout"]
        return []

    monkeypatch.setattr(hook_mod, "_fetch_via_rest", fake_rest)
    monkeypatch.setattr(hook_mod, "_fetch_via_cli", fake_cli)
    assert hook_mod._gather_hits_with_lane("prompt") == ([], "cli")
    assert seen["rest_timeout"] == hook_mod.REST_TIMEOUT_SECONDS
    assert seen["cli_timeout"] == pytest.approx(hook_mod.INJECT_BUDGET_SECONDS - 3.5)
    # time already spent (3.5 s) plus what the CLI rung may still spend stays inside the budget
    assert 3.5 + seen["cli_timeout"] <= hook_mod.INJECT_BUDGET_SECONDS + 1e-6


def test_rest_rung_that_dribbles_past_its_socket_timeout_is_cut_at_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """`urlopen(timeout=)` is a per-socket-operation timeout: a server that
    returns a byte every few seconds never trips it. The ladder must still end
    within the shared budget — measured 52 s against an 8 s budget before this
    bound existed. This test lets the rung spend real wall-clock time."""
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "key")
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", "1")
    monkeypatch.setattr(hook_mod, "INJECT_BUDGET_SECONDS", 0.6)
    monkeypatch.setattr(hook_mod, "_MIN_RUNG_SECONDS", 0.05)

    def dribbling_rest(prompt, api_key, **kw):
        hook_mod.time.sleep(3.0)  # the socket-level timeout never fires; only the budget can
        return [{"path": "Notes/late.md"}]

    def _boom(*a, **kw):
        raise AssertionError("no budget is left for the CLI rung after REST overran")

    monkeypatch.setattr(hook_mod, "_fetch_via_rest", dribbling_rest)
    monkeypatch.setattr(hook_mod, "_fetch_via_cli", _boom)
    started = hook_mod.time.monotonic()
    result = hook_mod._gather_hits_with_lane("prompt")
    elapsed = hook_mod.time.monotonic() - started
    assert result == ([], "none")
    assert elapsed < 1.5, f"ladder ran {elapsed:.2f}s against a 0.6s budget"


def test_gather_hits_skips_a_rung_the_budget_cannot_cover(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "key")
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", "1")
    ticks = [100.0, 100.0, 100.0 + hook_mod.INJECT_BUDGET_SECONDS - 0.1]
    monkeypatch.setattr(hook_mod.time, "monotonic", lambda: ticks.pop(0) if len(ticks) > 1 else ticks[0])
    monkeypatch.setattr(hook_mod, "_fetch_via_rest", lambda prompt, api_key, **kw: None)

    def _boom(*a, **kw):
        raise AssertionError("the CLI rung must be skipped when the budget is spent")

    monkeypatch.setattr(hook_mod, "_fetch_via_cli", _boom)
    assert hook_mod._gather_hits_with_lane("prompt") == ([], "none")


def test_cooldown_stamp_is_written_only_after_the_transport_ran(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """A hook the client kills mid-ladder must not also burn the session's
    cooldown: the stamp is written after the transport, so a killed hook
    re-nudges on the next prompt instead of going quiet for the cooldown."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "1")

    def _killed(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr(hook_mod, "_gather_hits_with_lane", _killed)
    with pytest.raises(KeyboardInterrupt):
        _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "killed-mid-ladder"}, home)
    assert not list((home / ".claude").glob("**/retrieve_killed-mid-ladder")), "stamp written before the transport"


def test_gather_hits_uses_rest_when_the_key_is_only_in_service_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]
    env_file = tmp_path / "service.env"
    _write_service_env(env_file, "EXOMEM_REST_API_KEY=from-file")
    monkeypatch.setenv("EXOMEM_SERVICE_ENV", str(env_file))
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", "1")
    seen: dict = {}

    def fake_rest(prompt, api_key, **kw):
        seen["api_key"] = api_key
        return hits

    def _boom(*a, **kw):
        raise AssertionError("_fetch_via_cli must not run when the service.env key reaches REST")

    monkeypatch.setattr(hook_mod, "_fetch_via_rest", fake_rest)
    monkeypatch.setattr(hook_mod, "_fetch_via_cli", _boom)
    assert hook_mod._gather_hits_with_lane("prompt")[0] == hits
    assert seen["api_key"] == "from-file"


# --- the log names the lane and the hit count -------------------------------------


def test_log_records_lane_and_hit_count_without_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """A silent fall-through to the reminder-only floor looked identical to a
    successful inject in the log for months; the line now says which lane
    answered and with how many hits, and never the key."""
    home = tmp_path / "home"
    home.mkdir()
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "1")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "secret-key-value")
    monkeypatch.setattr(urllib_request, "urlopen", lambda req, timeout=None: _FakeResponse(200, _envelope_bytes(hits)))

    _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "e2e-log-lane"}, home)

    log = (home / ".claude" / "exomem-retrieve-nudge.log").read_text(encoding="utf-8")
    assert "nudge fired | lane=rest hits=1 |" in log
    assert "secret-key-value" not in log


def test_log_records_the_reminder_only_floor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "1")
    monkeypatch.setattr(hook_mod.shutil, "which", lambda name: None)

    _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "e2e-log-floor"}, home)

    log = (home / ".claude" / "exomem-retrieve-nudge.log").read_text(encoding="utf-8")
    assert "nudge fired | lane=none hits=0 |" in log


def test_log_records_inject_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "e2e-log-off"}, home)

    log = (home / ".claude" / "exomem-retrieve-nudge.log").read_text(encoding="utf-8")
    assert "nudge fired | lane=off hits=0 |" in log


# --- end-to-end main() wiring ------------------------------------------------------


PROMPT = "what did I conclude about the kb hook design earlier?"


def test_default_off_no_network_or_subprocess_attempted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    def _boom_url(req, timeout=None):
        raise AssertionError("urlopen must not be called when EXOMEM_RETRIEVE_INJECT is unset")

    def _boom_run(cmd, **kwargs):
        raise AssertionError("subprocess.run must not be called when EXOMEM_RETRIEVE_INJECT is unset")

    monkeypatch.setattr(urllib_request, "urlopen", _boom_url)
    monkeypatch.setattr(hook_mod.subprocess, "run", _boom_run)

    out = _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "e2e-off"}, home)
    payload = json.loads(out)
    assert payload == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": hook_mod.REMINDER,
        }
    }


def test_control_prompt_skips_before_reminder_or_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "1")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "secret")

    def _boom_url(req, timeout=None):
        raise AssertionError("urlopen must not be called for control-only prompts")

    def _boom_run(cmd, **kwargs):
        raise AssertionError("subprocess.run must not be called for control-only prompts")

    monkeypatch.setattr(urllib_request, "urlopen", _boom_url)
    monkeypatch.setattr(hook_mod.subprocess, "run", _boom_run)

    out = _call_main(
        monkeypatch,
        capsys,
        {"prompt": "perfect merge then to main gj", "session_id": "e2e-control"},
        home,
    )
    assert out.strip() == ""


def test_inject_rest_configured_and_reachable_appends_stubs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]

    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "1")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "secret")
    monkeypatch.setattr(urllib_request, "urlopen", lambda req, timeout=None: _FakeResponse(200, _envelope_bytes(hits)))

    def _boom_run(cmd, **kwargs):
        raise AssertionError("subprocess must not be called when REST succeeds")

    monkeypatch.setattr(hook_mod.subprocess, "run", _boom_run)

    out = _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "e2e-rest-ok"}, home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(hook_mod.REMINDER + "\n\n")
    assert "- Notes/a.md (note, 2026-01-01)" in ctx
    assert "excerpt" not in ctx.lower()


def test_legacy_kb_retrieve_inject_env_still_activates_inject(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    # Back-compat: the OLD env name (KB_RETRIEVE_INJECT, no EXOMEM_ prefix) must
    # still turn inject mode on, aliased onto EXOMEM_RETRIEVE_INJECT at startup.
    home = tmp_path / "home"
    home.mkdir()
    hits = [{"path": "Notes/a.md", "type": "note", "updated": "2026-01-01"}]

    monkeypatch.setenv("KB_RETRIEVE_INJECT", "1")  # legacy name only — NOT EXOMEM_RETRIEVE_INJECT
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "secret")
    monkeypatch.setattr(urllib_request, "urlopen", lambda req, timeout=None: _FakeResponse(200, _envelope_bytes(hits)))

    out = _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "e2e-legacy-inject"}, home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(hook_mod.REMINDER + "\n\n")            # inject mode activated
    assert "- Notes/a.md (note, 2026-01-01)" in ctx


def test_inject_rest_unreachable_cli_not_opted_in_is_nudge_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "1")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "secret")

    def _boom_url(req, timeout=None):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(urllib_request, "urlopen", _boom_url)

    out = _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "e2e-rest-down"}, home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert ctx == hook_mod.REMINDER


def test_inject_cli_opt_in_which_resolves_appends_stubs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    hits = [{"path": "Notes/b.md", "type": "insight", "updated": "2026-01-02"}]

    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "1")
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", "1")
    monkeypatch.setattr(hook_mod.shutil, "which", lambda name: "/usr/local/bin/exomem" if name == "exomem" else None)
    monkeypatch.setattr(
        hook_mod.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"success": True, "data": hits}), stderr=""),
    )

    out = _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "e2e-cli-ok"}, home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "- Notes/b.md (insight, 2026-01-02)" in ctx


def test_inject_cli_opt_in_but_unresolvable_is_nudge_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "1")
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", "1")
    monkeypatch.setattr(hook_mod.shutil, "which", lambda name: None)

    def _boom_run(cmd, **kwargs):
        raise AssertionError("subprocess.run must not be called when no console script resolves")

    monkeypatch.setattr(hook_mod.subprocess, "run", _boom_run)

    out = _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "e2e-cli-missing"}, home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert ctx == hook_mod.REMINDER


def test_min_chars_gate_short_circuits_before_any_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "1")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "secret")

    def _boom_url(req, timeout=None):
        raise AssertionError("urlopen must not be called for a trivial prompt")

    monkeypatch.setattr(urllib_request, "urlopen", _boom_url)

    out = _call_main(monkeypatch, capsys, {"prompt": "yes go", "session_id": "e2e-short"}, home)
    assert out.strip() == ""


def test_cooldown_gate_short_circuits_second_transport_attempt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "1")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "secret")
    call_count = {"n": 0}

    def fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(200, _envelope_bytes([]))

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)

    event = {"prompt": PROMPT, "session_id": "e2e-cooldown"}
    first = _call_main(monkeypatch, capsys, event, home)
    second = _call_main(monkeypatch, capsys, event, home)

    assert "additionalContext" in first
    assert second.strip() == ""
    assert call_count["n"] == 1


def test_global_cooldown_suppresses_nearby_sessions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    first = _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "tab-a"}, home)
    second = _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "tab-b"}, home)

    assert "additionalContext" in first
    assert second.strip() == ""


def test_global_cooldown_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC", "0")

    first = _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "tab-a"}, home)
    second = _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "tab-b"}, home)

    assert "additionalContext" in first
    assert "additionalContext" in second


def test_exomem_retrieve_inject_zero_is_disabled_end_to_end(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT", "0")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "secret")

    def _boom_url(req, timeout=None):
        raise AssertionError("urlopen must not be called when EXOMEM_RETRIEVE_INJECT=0 (falsy)")

    monkeypatch.setattr(urllib_request, "urlopen", _boom_url)

    out = _call_main(monkeypatch, capsys, {"prompt": PROMPT, "session_id": "e2e-falsy"}, home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert ctx == hook_mod.REMINDER


# --- subprocess-level black box: default-off is byte-identical end-to-end --------


def _run_subprocess(event: dict, home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    for key in (
        "EXOMEM_RETRIEVE_INJECT", "EXOMEM_RETRIEVE_INJECT_CLI", "EXOMEM_REST_API_KEY", "EXOMEM_HOST",
        "EXOMEM_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC",
        "KB_RETRIEVE_INJECT", "KB_RETRIEVE_INJECT_CLI",  # legacy aliases, cleared too
        "KB_RETRIEVE_NUDGE_GLOBAL_COOLDOWN_SEC",
    ):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, str(RETRIEVE_SCRIPT)],
        input=json.dumps(event), capture_output=True, text=True, env=env,
    )


def test_subprocess_default_off_matches_reminder_only_output(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    r = _run_subprocess({"prompt": PROMPT, "session_id": "sub-1"}, home)
    payload = json.loads(r.stdout)
    assert payload == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": hook_mod.REMINDER,
        }
    }


def test_subprocess_default_off_silent_on_short_prompt(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    r = _run_subprocess({"prompt": "yes go", "session_id": "sub-2"}, home)
    assert r.stdout.strip() == ""
