"""install-hook: wire the KB capture-reliability Stop hook into Claude Code.

Ships the bundled hook (`_hooks/kb_capture_nudge.py`) into `~/.claude/hooks` and
registers it as a `Stop` hook in `~/.claude/settings.json`, so a friend gets
reliable auto-capture with one command. The hook is language-agnostic (structural
gate + cooldown), so it works regardless of the language you write in.

By default it copies the script AND merges settings.json. `wire=False`
(`--print-only`) copies the script and returns the snippet to paste instead, for
anyone who'd rather edit their config by hand.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

_HOOK_SRC = Path(__file__).parent / "_hooks" / "kb_capture_nudge.py"
_HOOK_NAME = "kb_capture_nudge.py"
DEFAULT_HOOK_DIR = Path.home() / ".claude" / "hooks"
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"

# Substrings identifying a previously-installed kb capture-nudge Stop entry, so
# re-running is idempotent and supersedes an older hand-wired wrapper.
_MARKERS = ("kb_capture_nudge", "kb-capture-nudge")


def _command_for(script: Path) -> str:
    """Invoke via the interpreter that ran install-hook (absolute path), so the
    hook doesn't depend on `python` being on PATH later. Quoted for spaces."""
    return f'"{sys.executable}" "{script}"'


def snippet(command: str, timeout: int = 10) -> str:
    """The settings.json fragment to merge into `hooks.Stop` (for --print-only)."""
    return json.dumps(
        {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": command, "timeout": timeout}
        ]}]}},
        indent=2,
    )


def install_hook(
    *,
    hook_dir: Path | None = None,
    settings_path: Path | None = None,
    wire: bool = True,
    timeout: int = 10,
) -> dict:
    """Install the capture-nudge hook script and (optionally) wire settings.json.

    Returns {"script", "command", "wired", "settings"}.
    Raises FileNotFoundError if the bundled hook is missing.
    """
    if not _HOOK_SRC.exists():
        raise FileNotFoundError(
            f"bundled hook missing at {_HOOK_SRC} — is the kb-mcp install intact?"
        )
    hook_dir = (Path(hook_dir).expanduser() if hook_dir else DEFAULT_HOOK_DIR)
    hook_dir.mkdir(parents=True, exist_ok=True)
    script = hook_dir / _HOOK_NAME
    shutil.copy2(_HOOK_SRC, script)
    command = _command_for(script)

    result = {"script": str(script), "command": command, "wired": False, "settings": None}
    if wire:
        sp = (Path(settings_path).expanduser() if settings_path else DEFAULT_SETTINGS)
        _merge_stop_hook(sp, command, timeout)
        result["wired"] = True
        result["settings"] = str(sp)
    return result


def _merge_stop_hook(path: Path, command: str, timeout: int) -> None:
    """Add our Stop hook to settings.json, preserving every other key and hook.

    Idempotent: strips any prior kb capture-nudge entry first (so re-running, or
    superseding an older hand-wired wrapper, never duplicates)."""
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = data["hooks"] = {}
    stop = hooks.get("Stop") if isinstance(hooks.get("Stop"), list) else []

    kept: list = []
    for group in stop:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        ghooks = [
            h for h in group.get("hooks", [])
            if not any(m in str(h.get("command", "")) for m in _MARKERS)
        ]
        if ghooks:  # group still has non-ours hooks — keep it, minus ours
            kept.append({**group, "hooks": ghooks})
        # a group whose only hook was ours is dropped entirely

    kept.append({"hooks": [{"type": "command", "command": command, "timeout": timeout}]})
    hooks["Stop"] = kept

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
