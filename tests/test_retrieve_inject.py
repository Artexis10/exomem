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
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from a known-clean env — a real EXOMEM_REST_API_KEY or
    EXOMEM_RETRIEVE_INJECT already set on the host machine must never leak in. The
    legacy KB_RETRIEVE_* names are cleared too: they still alias onto the EXOMEM_*
    names at startup (back-compat), so a host-set old name would leak just the same."""
    for var in (
        "EXOMEM_RETRIEVE_NUDGE_DISABLE",
        "EXOMEM_RETRIEVE_NUDGE_MIN_CHARS",
        "EXOMEM_RETRIEVE_NUDGE_COOLDOWN_SEC",
        "EXOMEM_RETRIEVE_INJECT",
        "EXOMEM_RETRIEVE_INJECT_CLI",
        "EXOMEM_REST_API_KEY",
        "EXOMEM_HOST",
        # Legacy aliases (still honored via _normalize_env_aliases).
        "KB_RETRIEVE_NUDGE_DISABLE",
        "KB_RETRIEVE_NUDGE_MIN_CHARS",
        "KB_RETRIEVE_NUDGE_COOLDOWN_SEC",
        "KB_RETRIEVE_INJECT",
        "KB_RETRIEVE_INJECT_CLI",
    ):
        monkeypatch.delenv(var, raising=False)


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


def test_format_inject_block_truncates_oversized_block() -> None:
    long_path = "Knowledge Base/" + ("very-long-segment-" * 15) + "note.md"
    hits = [{"path": long_path, "type": "note", "updated": "2026-01-01"} for _ in range(3)]
    block = hook_mod._format_inject_block(hits)
    assert len(block) <= hook_mod._STUB_BLOCK_MAX_CHARS
    assert block.endswith("…")


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
    assert captured["url"] == "http://127.0.0.1:8765/api/find"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["body"] == {
        "query": "what did I conclude about X?",
        "detail": "compact",
        "limit": 3,
        "mode": "keyword",
    }


def test_fetch_via_rest_respects_exomem_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_HOST", "10.0.0.5")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResponse(200, _envelope_bytes([]))

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)
    hook_mod._fetch_via_rest("prompt", "key")
    assert captured["url"] == "http://10.0.0.5:8765/api/find"


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
        "/usr/local/bin/exomem", "find",
        "--detail", "compact",
        "--limit", "3",
        "--mode", "keyword",
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
    assert hook_mod._gather_hits("prompt") == hits


def test_gather_hits_rest_failing_cli_unset_is_nudge_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "secret")
    monkeypatch.setattr(hook_mod, "_fetch_via_rest", lambda prompt, api_key, **kw: None)

    def _boom(*a, **kw):
        raise AssertionError("_fetch_via_cli must not be called when EXOMEM_RETRIEVE_INJECT_CLI is unset")

    monkeypatch.setattr(hook_mod, "_fetch_via_cli", _boom)
    assert hook_mod._gather_hits("prompt") == []


def test_gather_hits_rest_unconfigured_cli_opted_in_uses_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [{"path": "Notes/b.md", "type": "insight", "updated": "2026-01-02"}]
    monkeypatch.setenv("EXOMEM_RETRIEVE_INJECT_CLI", "1")

    def _boom(*a, **kw):
        raise AssertionError("_fetch_via_rest must not be called when EXOMEM_REST_API_KEY is unset")

    monkeypatch.setattr(hook_mod, "_fetch_via_rest", _boom)
    monkeypatch.setattr(hook_mod, "_fetch_via_cli", lambda prompt, **kw: hits)
    assert hook_mod._gather_hits("prompt") == hits


def test_gather_hits_neither_configured_calls_neither_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **kw):
        raise AssertionError("no transport seam should be called when neither is configured")

    monkeypatch.setattr(hook_mod, "_fetch_via_rest", _boom)
    monkeypatch.setattr(hook_mod, "_fetch_via_cli", _boom)
    assert hook_mod._gather_hits("prompt") == []


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
        "KB_RETRIEVE_INJECT", "KB_RETRIEVE_INJECT_CLI",  # legacy aliases, cleared too
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
