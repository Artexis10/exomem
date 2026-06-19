"""install-hook (installer) + the capture (Stop) and retrieval (UserPromptSubmit)
nudge hooks.

The hooks are the reliability fix for the KB loop: skill prose is passive, so
Stop re-arms "capture this stepping-stone" (write) and UserPromptSubmit re-arms
"consult the KB first" (read). Both are language-agnostic (structural gate +
cooldown, no English keywords) so they work on Japanese and every other language.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import kb_mcp
from kb_mcp import install_hook as hook_module

_HOOKS = Path(kb_mcp.__file__).parent / "_hooks"
CAPTURE_SCRIPT = _HOOKS / "kb_capture_nudge.py"
RETRIEVE_SCRIPT = _HOOKS / "kb_retrieve_nudge.py"


def _stop_cmds(data: dict) -> list[str]:
    return [h["command"] for g in data["hooks"].get("Stop", []) for h in g["hooks"]]


def _ups_cmds(data: dict) -> list[str]:
    return [h["command"] for g in data["hooks"].get("UserPromptSubmit", []) for h in g["hooks"]]


# --- install_hook: the installer (both hooks) -----------------------------------

def test_install_hook_copies_and_wires_both(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    r = hook_module.install_hook(hook_dir=hd, settings_path=sp)
    assert (hd / "kb_capture_nudge.py").exists()
    assert (hd / "kb_retrieve_nudge.py").exists()
    assert r["wired"] is True
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert any("kb_capture_nudge.py" in c for c in _stop_cmds(data))      # write side -> Stop
    assert any("kb_retrieve_nudge.py" in c for c in _ups_cmds(data))      # read side -> UserPromptSubmit


def test_install_hook_idempotent(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert sum("kb_capture_nudge" in c for c in _stop_cmds(data)) == 1
    assert sum("kb_retrieve_nudge" in c for c in _ups_cmds(data)) == 1


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
    assert "bash other-stop.sh" in _stop_cmds(data)                # unrelated Stop hook kept
    assert any("kb_capture_nudge" in c for c in _stop_cmds(data))  # ours added
    assert any("kb_retrieve_nudge" in c for c in _ups_cmds(data))


def test_install_hook_supersedes_old_wrapper(tmp_path: Path) -> None:
    sp = tmp_path / "settings.json"
    sp.write_text(
        json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "bash ~/.claude/hooks/kb-capture-nudge.sh"}]}
        ]}}),
        encoding="utf-8",
    )
    hook_module.install_hook(hook_dir=tmp_path / "hooks", settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    assert not any("kb-capture-nudge.sh" in c for c in _stop_cmds(data))   # old wrapper superseded
    assert sum("kb_capture_nudge" in c for c in _stop_cmds(data)) == 1


def test_install_hook_print_only_leaves_settings(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    r = hook_module.install_hook(hook_dir=hd, settings_path=sp, wire=False)
    assert (hd / "kb_capture_nudge.py").exists()
    assert (hd / "kb_retrieve_nudge.py").exists()
    assert r["wired"] is False
    assert not sp.exists()
    snip = hook_module.snippet(r["installed"])
    assert "Stop" in snip and "UserPromptSubmit" in snip


def test_install_hook_via_cli(tmp_path: Path) -> None:
    from kb_mcp.__main__ import main

    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    assert main(["install-hook", "--hook-dir", str(hd), "--settings", str(sp)]) == 0
    assert (hd / "kb_capture_nudge.py").exists()
    assert (hd / "kb_retrieve_nudge.py").exists()
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
    jp = "これは重要な結論です。" * 40  # ~400 chars, zero English keywords
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
    t = _transcript(tmp_path, "q?", "Big conclusion. " + "x" * 450,
                    assistant_tool="mcp__claude_ai_Knowledge_Base__note")
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
    jp = "去年このプロジェクトについて何を結論づけましたか？詳しく教えてください。"  # >20 chars, no English
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
