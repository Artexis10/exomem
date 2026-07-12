"""install-hook (installer) + the capture (Stop) and retrieval (UserPromptSubmit)
nudge hooks.

The hooks are the reliability fix for the KB loop: skill prose is passive, so Stop
re-arms "capture this stepping-stone" (write) and UserPromptSubmit re-arms "consult
the KB first" (read). Both are language-agnostic (structural gate + cooldown). The
registered command is machine-agnostic (`bash ~/.claude/hooks/<name>.sh`) so a
yadm-synced ~/.claude works across Windows / WSL / Linux / macOS. Codex installs
the same bundled Python scripts into ~/.codex/hooks with command + commandWindows
wiring.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import exomem
from exomem import install_hook as hook_module

_HOOKS = Path(exomem.__file__).parent / "_hooks"
CAPTURE_SCRIPT = _HOOKS / "exomem_capture_nudge.py"
RETRIEVE_SCRIPT = _HOOKS / "exomem_retrieve_nudge.py"


def _stop_cmds(data: dict) -> list[str]:
    return [h["command"] for g in data["hooks"].get("Stop", []) for h in g["hooks"]]


def _ups_cmds(data: dict) -> list[str]:
    return [h["command"] for g in data["hooks"].get("UserPromptSubmit", []) for h in g["hooks"]]


def _hook_entries(data: dict, event: str) -> list[dict]:
    return [h for g in data["hooks"].get(event, []) for h in g["hooks"]]


# --- install_hook: the installer (both hooks, py + wrapper) ----------------------

def test_install_hook_copies_scripts_and_wrappers_and_wires_both(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    r = hook_module.install_hook(hook_dir=hd, settings_path=sp)
    for f in ("exomem_capture_nudge.py", "exomem-capture-nudge.sh", "exomem_retrieve_nudge.py", "exomem-retrieve-nudge.sh"):
        assert (hd / f).exists(), f
    assert r["wired"] is True
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert any("exomem-capture-nudge.sh" in c for c in _stop_cmds(data))     # write -> Stop, via wrapper
    assert any("exomem-retrieve-nudge.sh" in c for c in _ups_cmds(data))     # read -> UserPromptSubmit


def test_command_is_machine_agnostic(tmp_path: Path) -> None:
    # Default location -> ~-relative bash command (no abs path / interpreter / backslash),
    # so the same settings.json works on every machine after a yadm sync.
    cmd = hook_module._command_for("exomem-capture-nudge.sh", hook_module.DEFAULT_HOOK_DIR)
    assert cmd == "bash ~/.claude/hooks/exomem-capture-nudge.sh"
    assert "\\" not in cmd and "python" not in cmd.lower() and ":" not in cmd
    # Custom dir -> POSIX (forward-slash) bash command, never Windows backslashes.
    custom = hook_module._command_for("exomem-capture-nudge.sh", tmp_path / "hooks")
    assert custom.startswith('bash "') and custom.endswith('.sh"') and "\\" not in custom

    codex = hook_module._command_for(
        "exomem-retrieve-nudge.sh",
        hook_module.DEFAULT_CODEX_HOOK_DIR,
        client="codex",
        script="exomem_retrieve_nudge.py",
    )
    assert codex == "python3 ~/.codex/hooks/exomem_retrieve_nudge.py"


def test_install_hook_idempotent(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert sum("exomem-capture-nudge" in c for c in _stop_cmds(data)) == 1
    assert sum("exomem-retrieve-nudge" in c for c in _ups_cmds(data)) == 1


def test_install_hook_supersedes_prior_absolute_path_entry(tmp_path: Path) -> None:
    # The old (buggy) form baked an absolute Windows python path; re-running must
    # replace it with the machine-agnostic wrapper command, exactly once.
    sp = tmp_path / "settings.json"
    sp.write_text(
        json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": '"C:\\Python\\python.exe" "C:\\Users\\x\\.claude\\hooks\\kb_capture_nudge.py"'}]}
        ]}}),
        encoding="utf-8",
    )
    hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    stop = _stop_cmds(data)
    assert not any("python.exe" in c for c in stop)             # absolute-path form gone
    assert sum("exomem-capture-nudge" in c for c in stop) == 1  # exactly one, the wrapper


def test_install_hook_migrates_old_kb_entry(tmp_path: Path) -> None:
    # A machine that installed the pre-rename hook has a `kb-capture-nudge.sh`
    # wrapper command wired in. Re-running install-hook must STRIP that legacy entry
    # (via the retained old _MARKERS) and leave only the new `exomem-capture-nudge`
    # one — a clean migration, not a duplicate.
    sp = tmp_path / "settings.json"
    sp.write_text(
        json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "bash ~/.claude/hooks/kb-capture-nudge.sh"}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "bash ~/.claude/hooks/kb-retrieve-nudge.sh"}]}],
        }}),
        encoding="utf-8",
    )
    hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    stop, ups = _stop_cmds(data), _ups_cmds(data)
    assert not any("kb-capture-nudge" in c for c in stop)        # legacy entry gone
    assert not any("kb-retrieve-nudge" in c for c in ups)
    assert sum("exomem-capture-nudge" in c for c in stop) == 1   # new entry present, once
    assert sum("exomem-retrieve-nudge" in c for c in ups) == 1


def test_install_hook_codex_migrates_old_kb_entries_and_preserves_other_hooks(tmp_path: Path) -> None:
    hd, sp = tmp_path / "codex-hooks", tmp_path / "hooks.json"
    sp.write_text(
        json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python guard.py"}]}],
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "python3 ~/.codex/hooks/kb_retrieve_nudge.py"}]},
                {"hooks": [{"type": "command", "command": "python3 ~/.codex/hooks/zellij_tab_context_rename.py"}]},
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "python3 ~/.codex/hooks/kb_capture_nudge.py"}]}],
        }}),
        encoding="utf-8",
    )

    r = hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")

    assert r["client"] == "codex"
    for f in ("exomem_capture_nudge.py", "exomem-capture-nudge.sh", "exomem_retrieve_nudge.py", "exomem-retrieve-nudge.sh"):
        assert (hd / f).exists(), f
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert _stop_cmds(data).count("python3 \"" + (hd / "exomem_capture_nudge.py").as_posix() + "\"") == 1
    ups = _ups_cmds(data)
    assert sum("exomem_retrieve_nudge.py" in c for c in ups) == 1
    assert not any("kb_retrieve_nudge" in c for c in ups)
    assert not any("kb_capture_nudge" in c for c in _stop_cmds(data))
    assert "exomem_retrieve_nudge.py" in ups[0]
    assert "zellij_tab_context_rename.py" in ups[1]
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "python guard.py"
    retrieve = [h for h in _hook_entries(data, "UserPromptSubmit") if "exomem_retrieve_nudge.py" in h["command"]][0]
    assert retrieve["commandWindows"].endswith("exomem_retrieve_nudge.py\"")


def test_install_hook_preserves_other_hooks_and_keys(tmp_path: Path) -> None:
    sp = tmp_path / "settings.json"
    sp.write_text(
        json.dumps({
            "theme": "dark",
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "bash guard.sh"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "bash other-stop.sh"}]}],
            },
        }),
        encoding="utf-8",
    )
    hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "bash guard.sh"
    assert "bash other-stop.sh" in _stop_cmds(data)                     # unrelated Stop hook kept
    assert any("exomem-capture-nudge" in c for c in _stop_cmds(data))   # ours added
    assert any("exomem-retrieve-nudge" in c for c in _ups_cmds(data))


def test_install_hook_print_only_leaves_settings(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    r = hook_module.install_hook(hook_dir=hd, settings_path=sp, wire=False)
    assert (hd / "exomem_capture_nudge.py").exists() and (hd / "exomem-capture-nudge.sh").exists()
    assert r["wired"] is False
    assert not sp.exists()
    snip = hook_module.snippet(r["installed"])
    assert "Stop" in snip and "UserPromptSubmit" in snip


def test_install_hook_via_cli(tmp_path: Path) -> None:
    from exomem.__main__ import main

    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    assert main(["install-hook", "--hook-dir", str(hd), "--settings", str(sp)]) == 0
    assert (hd / "exomem_capture_nudge.py").exists() and (hd / "exomem-retrieve-nudge.sh").exists()
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert data["hooks"]["Stop"] and data["hooks"]["UserPromptSubmit"]


def test_install_hook_via_cli_for_codex(tmp_path: Path) -> None:
    from exomem.__main__ import main

    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    assert main(["install-hook", "--client", "codex", "--hook-dir", str(hd), "--settings", str(sp)]) == 0
    assert (hd / "exomem_capture_nudge.py").exists() and (hd / "exomem_retrieve_nudge.py").exists()
    data = json.loads(sp.read_text(encoding="utf-8"))
    entries = _hook_entries(data, "UserPromptSubmit")
    assert any("exomem_retrieve_nudge.py" in h["command"] and h.get("commandWindows") for h in entries)


def test_install_hook_check_reports_healthy_codex_install(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")

    report = hook_module.check_hooks(clients=("codex",), hook_dir=hd, settings_path=sp)

    assert report["success"] is True
    client = report["clients"][0]
    assert client["client"] == "codex"
    assert any(c["id"] == "config.UserPromptSubmit" and c["status"] == "pass" for c in client["checks"])
    assert any(c["id"] == "scripts.Stop" and c["status"] == "pass" for c in client["checks"])
    assert client["logs"]["retrieve"]["path"].startswith(str(home / ".codex"))


def test_install_hook_check_flags_stale_deployed_copy(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    (hd / "exomem_retrieve_nudge.py").write_text("# stale\n", encoding="utf-8")

    report = hook_module.check_hooks(clients=("claude",), hook_dir=hd, settings_path=sp)

    assert report["success"] is False
    checks = report["clients"][0]["checks"]
    stale = [c for c in checks if c["id"] == "scripts.UserPromptSubmit"][0]
    assert stale["status"] == "fail"
    assert "differ from bundled source" in stale["message"]


def test_install_hook_check_flags_legacy_kb_config(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    data["hooks"]["UserPromptSubmit"] = [
        {"hooks": [{"type": "command", "command": "bash ~/.claude/hooks/kb-retrieve-nudge.sh"}]}
    ]
    sp.write_text(json.dumps(data), encoding="utf-8")

    report = hook_module.check_hooks(clients=("claude",), hook_dir=hd, settings_path=sp)

    assert report["success"] is False
    checks = report["clients"][0]["checks"]
    assert [c for c in checks if c["id"] == "config.legacy"][0]["status"] == "fail"
    assert [c for c in checks if c["id"] == "config.UserPromptSubmit"][0]["status"] == "fail"


def test_install_hook_check_via_cli_json(tmp_path: Path, capsys) -> None:
    from exomem.__main__ import main

    hd, sp = tmp_path / "hooks", tmp_path / "hooks.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp, client="codex")

    assert main([
        "install-hook", "--check", "--client", "codex",
        "--hook-dir", str(hd), "--settings", str(sp), "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["clients"][0]["client"] == "codex"


def test_hook_check_skips_absent_client_in_multi_client_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_hooks = tmp_path / ".claude" / "hooks"
    claude_settings = tmp_path / ".claude" / "settings.json"
    hook_module.install_hook(
        hook_dir=claude_hooks,
        settings_path=claude_settings,
        client="claude",
    )

    monkeypatch.setattr(
        hook_module,
        "_default_hook_dir",
        lambda client: claude_hooks if client == "claude" else tmp_path / ".codex" / "hooks",
    )
    monkeypatch.setattr(
        hook_module,
        "_default_settings",
        lambda client: claude_settings if client == "claude" else tmp_path / ".codex" / "hooks.json",
    )

    report = hook_module.check_hooks(clients=("claude", "codex"))
    assert report["success"] is True
    codex = next(item for item in report["clients"] if item["client"] == "codex")
    assert codex["status"] == "skipped"
    assert codex["success"] is True


def test_hook_check_explicit_absent_client_remains_strict(tmp_path: Path) -> None:
    report = hook_module.check_hooks(
        clients=("codex",),
        hook_dir=tmp_path / "hooks",
        settings_path=tmp_path / "hooks.json",
    )
    assert report["success"] is False
    assert report["clients"][0]["status"] == "failed"


# --- shared subprocess helper ---------------------------------------------------

def _run(script: Path, event: dict, home: Path, extra_env: dict | None = None):
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        **(extra_env or {}),
    }  # redirect Path.home()
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(event), capture_output=True, text=True, env=env,
    )


def _transcript(tmp_path: Path, user_text: str, assistant_text: str | None = None,
                assistant_tool: str | None = None) -> Path:
    content: list[dict] = []
    if assistant_tool:
        content.append({"type": "tool_use", "name": assistant_tool})
    if assistant_text is not None:
        content.append({"type": "text", "text": assistant_text})
    lines = [
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": user_text}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": content}},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return p


# --- capture (Stop) gate --------------------------------------------------------

def test_capture_fires_on_substantial_turn(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    t = _transcript(tmp_path, "q?", "We landed on a clear decision. " + "x" * 450)
    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "s1"}, home)
    assert '"decision": "block"' in r.stdout


def test_capture_fires_language_agnostic_japanese(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    jp = "これは重要な結論です。" * 40
    t = _transcript(tmp_path, "質問", jp)
    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "jp"}, home)
    assert '"decision": "block"' in r.stdout


def test_capture_silent_on_trivial_turn(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    t = _transcript(tmp_path, "q?", "Done.")
    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "s2"}, home)
    assert r.stdout.strip() == ""


def test_capture_silent_when_already_saved(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    # Real post-rename connector tool name is Exomem (not Knowledge_Base) — the guard
    # regex must recognise it, or the nudge misfires after every real capture.
    t = _transcript(tmp_path, "q?", "Big conclusion. " + "x" * 450,
                    assistant_tool="mcp__claude_ai_Exomem__note")
    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "s3"}, home)
    assert r.stdout.strip() == ""


def test_capture_silent_when_stop_hook_active(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    t = _transcript(tmp_path, "q?", "Big conclusion. " + "x" * 450)
    r = _run(CAPTURE_SCRIPT, {"transcript_path": str(t), "session_id": "s4", "stop_hook_active": True}, home)
    assert r.stdout.strip() == ""


def test_capture_cooldown_suppresses_second_fire(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    t = _transcript(tmp_path, "q?", "Big conclusion. " + "x" * 450)
    ev = {"transcript_path": str(t), "session_id": "cd"}
    first = _run(CAPTURE_SCRIPT, ev, home)
    second = _run(CAPTURE_SCRIPT, ev, home)
    assert '"decision": "block"' in first.stdout
    assert second.stdout.strip() == ""


def test_capture_codex_client_accepts_camel_case_and_uses_codex_state(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    t = _transcript(tmp_path, "q?", "We landed on a clear decision. " + "x" * 450)
    r = _run(
        CAPTURE_SCRIPT,
        {"transcriptPath": str(t), "sessionId": "codex-cap"},
        home,
        {"EXOMEM_HOOK_CLIENT": "codex"},
    )
    assert '"decision": "block"' in r.stdout
    assert (home / ".codex" / ".cache" / "exomem-nudge" / "codex-cap").exists()
    assert (home / ".codex" / "exomem-capture-nudge.log").exists()
    assert not (home / ".claude" / "exomem-capture-nudge.log").exists()


# --- retrieval (UserPromptSubmit) gate ------------------------------------------

def test_retrieve_fires_on_substantial_prompt(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    r = _run(RETRIEVE_SCRIPT, {"prompt": "what did I conclude about the kb hook design earlier?", "session_id": "r1"}, home)
    assert "additionalContext" in r.stdout


def test_retrieve_fires_language_agnostic_japanese(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    jp = "去年このプロジェクトについて何を結論づけましたか？詳しく教えてください。"
    r = _run(RETRIEVE_SCRIPT, {"prompt": jp, "session_id": "rjp"}, home)
    assert "additionalContext" in r.stdout


def test_retrieve_silent_on_short_prompt(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    r = _run(RETRIEVE_SCRIPT, {"prompt": "yes go", "session_id": "r2"}, home)
    assert r.stdout.strip() == ""


def test_retrieve_cooldown_suppresses_second_fire(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    ev = {"prompt": "what did I conclude about the architecture decisions here?", "session_id": "rc"}
    first = _run(RETRIEVE_SCRIPT, ev, home)
    second = _run(RETRIEVE_SCRIPT, ev, home)
    assert "additionalContext" in first.stdout
    assert second.stdout.strip() == ""


def test_retrieve_codex_client_accepts_camel_case_and_uses_codex_state(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    r = _run(
        RETRIEVE_SCRIPT,
        {"userPrompt": "what did I conclude about the Codex hook setup earlier?", "sessionId": "codex-ret"},
        home,
        {"EXOMEM_HOOK_CLIENT": "codex"},
    )
    assert "additionalContext" in r.stdout
    assert (home / ".codex" / ".cache" / "exomem-nudge" / "retrieve_codex-ret").exists()
    assert (home / ".codex" / "exomem-retrieve-nudge.log").exists()
    assert not (home / ".claude" / "exomem-retrieve-nudge.log").exists()
