"""install-hook (installer) + the capture (Stop) and retrieval (UserPromptSubmit)
nudge hooks.

The hooks are the reliability fix for the KB loop: skill prose is passive, so Stop
re-arms "capture this stepping-stone" (write) and UserPromptSubmit re-arms "consult
the KB first" (read). Both are language-agnostic (structural gate + cooldown). The
registered command is machine-agnostic (`bash ~/.claude/hooks/<name>.sh`) so a
yadm-synced ~/.claude works across Windows / WSL / Linux / macOS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import exomem
from exomem import install_hook as hook_module

_HOOKS = Path(exomem.__file__).parent / "_hooks"
CAPTURE_SCRIPT = _HOOKS / "exomem_capture_nudge.py"
RETRIEVE_SCRIPT = _HOOKS / "exomem_retrieve_nudge.py"


def _stop_cmds(data: dict) -> list[str]:
    return [h["command"] for g in data["hooks"].get("Stop", []) for h in g["hooks"]]


def _ups_cmds(data: dict) -> list[str]:
    return [h["command"] for g in data["hooks"].get("UserPromptSubmit", []) for h in g["hooks"]]


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


# --- shared subprocess helper ---------------------------------------------------

def _run(script: Path, event: dict, home: Path):
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}  # redirect Path.home()
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
