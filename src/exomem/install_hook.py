"""install-hook: wire the KB capture + retrieval hooks into supported agents.

Ships two bundled hooks (each a Python script + a bash wrapper) and registers them
in the target client's hook config, so a friend gets the full reliable KB loop
with one command:

- `_hooks/exomem_capture_nudge.py`  (via `exomem-capture-nudge.sh`)  → a `Stop` hook (WRITE):
  captures durable conclusions at stepping-stones instead of waiting to be told.
- `_hooks/exomem_retrieve_nudge.py` (via `exomem-retrieve-nudge.sh`) → a `UserPromptSubmit`
  hook (READ): reminds the agent to consult the KB before answering.

The registered command is **machine-agnostic** — `bash ~/.claude/hooks/<name>.sh`,
matching the convention of other Claude Code hooks; Codex gets the same Python
scripts directly with `command` + `commandWindows` entries in `~/.codex/hooks.json`.

Both gates are language-agnostic (structural + cooldown, no English keywords). By
default this copies the scripts AND merges settings.json; `wire=False`
(`--print-only`) copies them and returns the snippet to paste instead.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

_HOOK_DIR_SRC = Path(__file__).parent / "_hooks"
# (python script, bash wrapper, hook event) — the hooks this installs.
_HOOK_SPECS = (
    ("exomem_capture_nudge.py", "exomem-capture-nudge.sh", "Stop"),
    ("exomem_retrieve_nudge.py", "exomem-retrieve-nudge.sh", "UserPromptSubmit"),
)
DEFAULT_CLIENT = "claude"
SUPPORTED_CLIENTS = ("claude", "codex")
DEFAULT_CLAUDE_HOOK_DIR = Path.home() / ".claude" / "hooks"
DEFAULT_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
DEFAULT_CODEX_HOOK_DIR = Path.home() / ".codex" / "hooks"
DEFAULT_CODEX_SETTINGS = Path.home() / ".codex" / "hooks.json"

# Back-compat for existing tests and callers.
DEFAULT_HOOK_DIR = DEFAULT_CLAUDE_HOOK_DIR
DEFAULT_SETTINGS = DEFAULT_CLAUDE_SETTINGS

# Substrings identifying a previously-installed nudge entry, so re-running is
# idempotent and supersedes older entries (incl. the absolute-python-path form).
# BOTH the legacy `kb_*`/`kb-*` names AND the current `exomem_*`/`exomem-*` names are
# listed, so re-running install-hook cleanly migrates a machine off the old kb entry.
_MARKERS = (
    "kb_capture_nudge", "kb_retrieve_nudge", "kb-capture-nudge", "kb-retrieve-nudge",
    "exomem_capture_nudge", "exomem_retrieve_nudge", "exomem-capture-nudge", "exomem-retrieve-nudge",
)


def _normalize_client(client: str) -> str:
    value = (client or DEFAULT_CLIENT).strip().lower()
    if value not in SUPPORTED_CLIENTS:
        raise ValueError(f"unsupported hook client {client!r}; expected one of {SUPPORTED_CLIENTS}")
    return value


def _default_hook_dir(client: str) -> Path:
    return DEFAULT_CODEX_HOOK_DIR if _normalize_client(client) == "codex" else DEFAULT_CLAUDE_HOOK_DIR


def _default_settings(client: str) -> Path:
    return DEFAULT_CODEX_SETTINGS if _normalize_client(client) == "codex" else DEFAULT_CLAUDE_SETTINGS


def _windows_python_command(script: Path) -> str:
    return f'python "{script}"'


def _command_for(
    wrapper: str,
    hook_dir: Path,
    *,
    client: str = DEFAULT_CLIENT,
    script: str | None = None,
) -> str:
    """Machine-agnostic `bash` invocation of the wrapper. For the default location
    use the `~`-relative form so the SAME settings.json works on every machine
    (yadm-synced); for a custom dir (tests) use a POSIX absolute path.

    Codex runs the same bundled Python hook directly. On Unix-like hosts `command`
    uses `python3`; on Windows the sibling `commandWindows` entry carries the
    native `python` invocation.
    """
    client = _normalize_client(client)
    hook_dir = Path(hook_dir).expanduser()
    if client == "codex":
        py_name = script or wrapper.removesuffix(".sh").replace("-", "_") + ".py"
        if hook_dir == DEFAULT_CODEX_HOOK_DIR:
            return f"python3 ~/.codex/hooks/{py_name}"
        return f'python3 "{(hook_dir / py_name).as_posix()}"'
    if hook_dir == DEFAULT_CLAUDE_HOOK_DIR:
        return f"bash ~/.claude/hooks/{wrapper}"
    return f'bash "{(hook_dir / wrapper).as_posix()}"'


def _command_windows_for(script: str, hook_dir: Path, *, client: str = DEFAULT_CLIENT) -> str | None:
    if _normalize_client(client) != "codex":
        return None
    return _windows_python_command(Path(hook_dir).expanduser() / script)


def _hook_entry(item: dict, timeout: int) -> dict:
    entry = {"type": "command", "command": item["command"], "timeout": timeout}
    if item.get("commandWindows"):
        entry["commandWindows"] = item["commandWindows"]
    return entry


def snippet(installed: list[dict], timeout: int = 10) -> str:
    """The hook config fragment to merge by hand (for --print-only)."""
    hooks: dict[str, list] = {}
    for item in installed:
        hooks[item["event"]] = [{"hooks": [_hook_entry(item, timeout)]}]
    return json.dumps({"hooks": hooks}, indent=2)


def install_hook(
    *,
    hook_dir: Path | None = None,
    settings_path: Path | None = None,
    wire: bool = True,
    timeout: int = 10,
    specs: tuple = _HOOK_SPECS,
    client: str = DEFAULT_CLIENT,
) -> dict:
    """Install the bundled hook scripts + wrappers and (optionally) wire config.

    Returns {"installed": [{event, script, wrapper, command}], "wired", "settings",
    "client"}. Raises FileNotFoundError if a bundled hook file is missing.
    """
    client = _normalize_client(client)
    for py_name, sh_name, _event in specs:
        for name in (py_name, sh_name):
            if not (_HOOK_DIR_SRC / name).exists():
                raise FileNotFoundError(
                    f"bundled hook file missing at {_HOOK_DIR_SRC / name} — is the exomem install intact?"
                )
    hook_dir = (Path(hook_dir).expanduser() if hook_dir else _default_hook_dir(client))
    hook_dir.mkdir(parents=True, exist_ok=True)

    installed: list[dict] = []
    for py_name, sh_name, event in specs:
        shutil.copy2(_HOOK_DIR_SRC / py_name, hook_dir / py_name)
        shutil.copy2(_HOOK_DIR_SRC / sh_name, hook_dir / sh_name)
        installed.append({
            "event": event,
            "script": str(hook_dir / py_name),
            "wrapper": str(hook_dir / sh_name),
            "command": _command_for(sh_name, hook_dir, client=client, script=py_name),
            "commandWindows": _command_windows_for(py_name, hook_dir, client=client),
        })

    result = {"installed": installed, "wired": False, "settings": None, "client": client}
    if wire:
        sp = (Path(settings_path).expanduser() if settings_path else _default_settings(client))
        _merge_hooks(sp, installed, timeout)
        result["wired"] = True
        result["settings"] = str(sp)
    return result


def _merge_hooks(path: Path, installed: list[dict], timeout: int) -> None:
    """Add each hook to its event in settings.json, preserving every other key and
    hook. Idempotent: strips any prior exomem/kb nudge entry from the target event
    first (so re-running, migrating off the legacy `kb-*` names, or superseding the
    old absolute-path command, never duplicates)."""
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
        event = item["event"]
        arr = hooks.get(event) if isinstance(hooks.get(event), list) else []
        kept: list = []
        inserted = False
        replacement = {"hooks": [_hook_entry(item, timeout)]}
        for group in arr:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            original = group.get("hooks", [])
            ghooks = [
                h for h in original
                if not any(
                    m in f"{h.get('command', '')} {h.get('commandWindows', '')}"
                    for m in _MARKERS
                )
            ]
            removed_ours = len(ghooks) != len(original)
            if ghooks:  # group still has non-ours hooks — keep it, minus ours
                kept.append({**group, "hooks": ghooks})
            if removed_ours and not inserted:
                kept.append(replacement)
                inserted = True
            # duplicate groups whose only hook was ours are dropped entirely
        if not inserted:
            kept.append(replacement)
        hooks[event] = kept

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
