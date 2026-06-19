"""install-hook (the installer) + the capture-nudge Stop hook (the gate).

The hook is the reliability fix for auto-capture: skill prose is passive, so a
Stop hook re-arms the "is this a stepping-stone? capture it" check each turn. It's
language-agnostic (structural gate + cooldown, no English keywords) so it works on
Japanese and every other language.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import kb_mcp
from kb_mcp import install_hook as hook_module

HOOK_SCRIPT = Path(kb_mcp.__file__).parent / "_hooks" / "kb_capture_nudge.py"


# --- install_hook: the installer ------------------------------------------------

def test_install_hook_copies_and_wires(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    r = hook_module.install_hook(hook_dir=hd, settings_path=sp)
    assert (hd / "kb_capture_nudge.py").exists()
    assert r["wired"] is True
    data = json.loads(sp.read_text(encoding="utf-8"))
    cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert any("kb_capture_nudge.py" in c for c in cmds)


def test_install_hook_idempotent(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    hook_module.install_hook(hook_dir=hd, settings_path=sp)
    data = json.loads(sp.read_text(encoding="utf-8"))
    ours = [
        h["command"]
        for g in data["hooks"]["Stop"]
        for h in g["hooks"]
        if "kb_capture_nudge" in h["command"]
    ]
    assert len(ours) == 1  # re-running must not duplicate


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
    stop = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert "bash other-stop.sh" in stop  # unrelated Stop hook kept
    assert any("kb_capture_nudge" in c for c in stop)  # ours added


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
    stop = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert not any("kb-capture-nudge.sh" in c for c in stop)  # old wrapper superseded
    assert sum("kb_capture_nudge" in c for c in stop) == 1


def test_install_hook_print_only_leaves_settings(tmp_path: Path) -> None:
    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    r = hook_module.install_hook(hook_dir=hd, settings_path=sp, wire=False)
    assert (hd / "kb_capture_nudge.py").exists()
    assert r["wired"] is False
    assert not sp.exists()


def test_install_hook_via_cli(tmp_path: Path) -> None:
    from kb_mcp.__main__ import main

    hd, sp = tmp_path / "hooks", tmp_path / "settings.json"
    assert main(["install-hook", "--hook-dir", str(hd), "--settings", str(sp)]) == 0
    assert (hd / "kb_capture_nudge.py").exists()
    assert sp.exists()


# --- the hook gate: real Stop-hook contract via subprocess ----------------------

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


def _run(event: dict, home: Path):
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}  # redirect Path.home()
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(event), capture_output=True, text=True, env=env,
    )


def test_hook_fires_on_substantial_turn(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    t = _transcript(tmp_path, "q?", "We landed on a clear decision. " + "x" * 450)
    r = _run({"transcript_path": str(t), "session_id": "s1"}, home)
    assert '"decision": "block"' in r.stdout


def test_hook_fires_language_agnostic_japanese(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    jp = "これは重要な結論です。" * 40  # >400 chars, zero English keywords
    t = _transcript(tmp_path, "質問", jp)
    r = _run({"transcript_path": str(t), "session_id": "jp"}, home)
    assert '"decision": "block"' in r.stdout


def test_hook_silent_on_trivial_turn(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    t = _transcript(tmp_path, "q?", "Done.")
    r = _run({"transcript_path": str(t), "session_id": "s2"}, home)
    assert r.stdout.strip() == ""


def test_hook_silent_when_already_saved(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    t = _transcript(tmp_path, "q?", "Big conclusion. " + "x" * 450,
                    assistant_tool="mcp__claude_ai_Knowledge_Base__note")
    r = _run({"transcript_path": str(t), "session_id": "s3"}, home)
    assert r.stdout.strip() == ""


def test_hook_silent_when_stop_hook_active(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    t = _transcript(tmp_path, "q?", "Big conclusion. " + "x" * 450)
    r = _run({"transcript_path": str(t), "session_id": "s4", "stop_hook_active": True}, home)
    assert r.stdout.strip() == ""


def test_hook_cooldown_suppresses_second_fire(tmp_path: Path) -> None:
    home = tmp_path / "home"; home.mkdir()
    t = _transcript(tmp_path, "q?", "Big conclusion. " + "x" * 450)
    ev = {"transcript_path": str(t), "session_id": "cd"}
    first = _run(ev, home)
    second = _run(ev, home)
    assert '"decision": "block"' in first.stdout
    assert second.stdout.strip() == ""  # cooldown bounds cost
