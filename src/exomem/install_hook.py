"""install-hook: wire the KB capture + retrieval hooks into Claude Code.

Ships two bundled hooks (each a Python script + a bash wrapper) and registers them
in `~/.claude/settings.json`, so a friend gets the full reliable KB loop with one
command:

- `_hooks/kb_capture_nudge.py`  (via `kb-capture-nudge.sh`)  → a `Stop` hook (WRITE):
  captures durable conclusions at stepping-stones instead of waiting to be told.
- `_hooks/kb_retrieve_nudge.py` (via `kb-retrieve-nudge.sh`) → a `UserPromptSubmit`
  hook (READ): reminds Claude to consult the KB before answering.

The registered command is **machine-agnostic** — `bash ~/.claude/hooks/<name>.sh`,
matching the convention of other hooks — so the same `~/.claude` works across
machines (Windows Git Bash, WSL, Linux, macOS) and survives yadm/dotfile sync. The
wrapper resolves python per machine; an absolute interpreter path would break the
moment `~/.claude` is shared between a Windows box and WSL.

Both gates are language-agnostic (structural + cooldown, no English keywords). By
default this copies the scripts AND merges settings.json; `wire=False`
(`--print-only`) copies them and returns the snippet to paste instead.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

_HOOK_DIR_SRC = Path(__file__).parent / "_hooks"
# (python script, bash wrapper, Claude Code event) — the hooks this installs.
_HOOK_SPECS = (
    ("kb_capture_nudge.py", "kb-capture-nudge.sh", "Stop"),
    ("kb_retrieve_nudge.py", "kb-retrieve-nudge.sh", "UserPromptSubmit"),
)
DEFAULT_HOOK_DIR = Path.home() / ".claude" / "hooks"
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"

# Substrings identifying a previously-installed kb nudge entry, so re-running is
# idempotent and supersedes older entries (incl. the absolute-python-path form).
_MARKERS = ("kb_capture_nudge", "kb_retrieve_nudge", "kb-capture-nudge", "kb-retrieve-nudge")


def _command_for(wrapper: str, hook_dir: Path) -> str:
    """Machine-agnostic `bash` invocation of the wrapper. For the default location
    use the `~`-relative form so the SAME settings.json works on every machine
    (yadm-synced); for a custom dir (tests) use a POSIX absolute path."""
    if hook_dir == DEFAULT_HOOK_DIR:
        return f"bash ~/.claude/hooks/{wrapper}"
    return f'bash "{(hook_dir / wrapper).as_posix()}"'


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
    """Install the bundled hook scripts + wrappers and (optionally) wire settings.json.

    Returns {"installed": [{event, script, wrapper, command}], "wired", "settings"}.
    Raises FileNotFoundError if a bundled hook file is missing.
    """
    for py_name, sh_name, _event in specs:
        for name in (py_name, sh_name):
            if not (_HOOK_DIR_SRC / name).exists():
                raise FileNotFoundError(
                    f"bundled hook file missing at {_HOOK_DIR_SRC / name} — is the exomem install intact?"
                )
    hook_dir = (Path(hook_dir).expanduser() if hook_dir else DEFAULT_HOOK_DIR)
    hook_dir.mkdir(parents=True, exist_ok=True)

    installed: list[dict] = []
    for py_name, sh_name, event in specs:
        shutil.copy2(_HOOK_DIR_SRC / py_name, hook_dir / py_name)
        shutil.copy2(_HOOK_DIR_SRC / sh_name, hook_dir / sh_name)
        installed.append({
            "event": event,
            "script": str(hook_dir / py_name),
            "wrapper": str(hook_dir / sh_name),
            "command": _command_for(sh_name, hook_dir),
        })

    result = {"installed": installed, "wired": False, "settings": None}
    if wire:
        sp = (Path(settings_path).expanduser() if settings_path else DEFAULT_SETTINGS)
        _merge_hooks(sp, installed, timeout)
        result["wired"] = True
        result["settings"] = str(sp)
    return result


def _merge_hooks(path: Path, installed: list[dict], timeout: int) -> None:
    """Add each hook to its event in settings.json, preserving every other key and
    hook. Idempotent: strips any prior kb nudge entry from the target event first
    (so re-running, or superseding the old absolute-path command, never duplicates)."""
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
