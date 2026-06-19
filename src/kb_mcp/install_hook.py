"""install-hook: wire the KB capture + retrieval hooks into Claude Code.

Ships two bundled hooks and registers them in `~/.claude/settings.json`, so a
friend gets the full reliable KB loop with one command:

- `_hooks/kb_capture_nudge.py`  → a `Stop` hook (WRITE side): captures durable
  conclusions at stepping-stones instead of waiting to be told.
- `_hooks/kb_retrieve_nudge.py` → a `UserPromptSubmit` hook (READ side): reminds
  Claude to consult the KB before answering, so it functions as the source of
  truth.

Both are language-agnostic (structural gate + cooldown, no English keywords). By
default this copies the scripts AND merges settings.json. `wire=False`
(`--print-only`) copies the scripts and returns the snippet to paste instead.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

_HOOK_DIR_SRC = Path(__file__).parent / "_hooks"
# (script filename, Claude Code event) — the hooks this installs.
_HOOK_SPECS = (
    ("kb_capture_nudge.py", "Stop"),
    ("kb_retrieve_nudge.py", "UserPromptSubmit"),
)
DEFAULT_HOOK_DIR = Path.home() / ".claude" / "hooks"
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"

# Substrings identifying a previously-installed kb nudge entry, so re-running is
# idempotent and supersedes older hand-wired wrappers.
_MARKERS = ("kb_capture_nudge", "kb_retrieve_nudge", "kb-capture-nudge", "kb-retrieve-nudge")


def _command_for(script: Path) -> str:
    """Invoke via the interpreter that ran install-hook (absolute path), so the
    hook doesn't depend on `python` being on PATH later. Quoted for spaces."""
    return f'"{sys.executable}" "{script}"'


def snippet(installed: list[dict], timeout: int = 10) -> str:
    """The settings.json fragment to merge by hand (for --print-only)."""
    hooks: dict[str, list] = {}
    for item in installed:
        hooks[item["event"]] = [
            {"hooks": [{"type": "command", "command": item["command"], "timeout": timeout}]}
        ]
    return json.dumps({"hooks": hooks}, indent=2)


def install_hook(
    *,
    hook_dir: Path | None = None,
    settings_path: Path | None = None,
    wire: bool = True,
    timeout: int = 10,
    specs: tuple = _HOOK_SPECS,
) -> dict:
    """Install the bundled hook scripts and (optionally) wire settings.json.

    Returns {"installed": [{event, script, command}], "wired", "settings"}.
    Raises FileNotFoundError if a bundled hook is missing.
    """
    for name, _event in specs:
        if not (_HOOK_DIR_SRC / name).exists():
            raise FileNotFoundError(
                f"bundled hook missing at {_HOOK_DIR_SRC / name} — is the kb-mcp install intact?"
            )
    hook_dir = (Path(hook_dir).expanduser() if hook_dir else DEFAULT_HOOK_DIR)
    hook_dir.mkdir(parents=True, exist_ok=True)

    installed: list[dict] = []
    for name, event in specs:
        dest = hook_dir / name
        shutil.copy2(_HOOK_DIR_SRC / name, dest)
        installed.append({"event": event, "script": str(dest), "command": _command_for(dest)})

    result = {"installed": installed, "wired": False, "settings": None}
    if wire:
        sp = (Path(settings_path).expanduser() if settings_path else DEFAULT_SETTINGS)
        _merge_hooks(sp, installed, timeout)
        result["wired"] = True
        result["settings"] = str(sp)
    return result


def _merge_hooks(path: Path, installed: list[dict], timeout: int) -> None:
    """Add each hook to its event in settings.json, preserving every other key
    and hook. Idempotent: strips any prior kb nudge entry from the target event
    first (so re-running, or superseding an older hand-wired wrapper, never
    duplicates)."""
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

    for item in installed:
        event, command = item["event"], item["command"]
        arr = hooks.get(event) if isinstance(hooks.get(event), list) else []
        kept: list = []
        for group in arr:
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
        hooks[event] = kept

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
