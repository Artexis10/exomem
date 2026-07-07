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

import hashlib
import json
import shutil
import time
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
_LEGACY_MARKERS = ("kb_capture_nudge", "kb_retrieve_nudge", "kb-capture-nudge", "kb-retrieve-nudge")


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


def _sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - diagnostic boundary
        return None, str(e)
    return (loaded, None) if isinstance(loaded, dict) else (None, "top-level JSON is not an object")


def _commands_for_event(data: dict | None, event: str) -> list[dict]:
    if not isinstance(data, dict):
        return []
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return []
    entries: list[dict] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []):
            if isinstance(hook, dict):
                entries.append(hook)
    return entries


def _contains_any(hook: dict, markers: tuple[str, ...]) -> bool:
    haystack = f"{hook.get('command', '')} {hook.get('commandWindows', '')}"
    return any(marker in haystack for marker in markers)


def _fmt_age(ts: float | None) -> str | None:
    if ts is None:
        return None
    age = max(0, int(time.time() - ts))
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    if age < 86400:
        return f"{age // 3600}h ago"
    return f"{age // 86400}d ago"


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _cache_summary(client: str) -> dict:
    cache = Path.home() / (".codex" if client == "codex" else ".claude") / ".cache" / "exomem-nudge"
    entries: list[Path] = []
    try:
        if cache.exists():
            entries = [p for p in cache.iterdir() if p.is_file()]
    except OSError:
        entries = []
    latest = max((_file_mtime(p) for p in entries), default=None)
    return {
        "path": str(cache),
        "exists": cache.exists(),
        "entries": len(entries),
        "latest_age": _fmt_age(latest),
    }


def _log_summary(client: str, kind: str) -> dict:
    home = Path.home() / (".codex" if client == "codex" else ".claude")
    path = home / f"exomem-{kind}-nudge.log"
    mtime = _file_mtime(path)
    return {
        "path": str(path),
        "exists": path.exists(),
        "last_trigger_age": _fmt_age(mtime),
    }


def _script_status(hook_dir: Path, script: str, wrapper: str) -> dict:
    items = {}
    for name in (script, wrapper):
        src = _HOOK_DIR_SRC / name
        dst = hook_dir / name
        src_hash = _sha256(src)
        dst_hash = _sha256(dst)
        items[name] = {
            "path": str(dst),
            "exists": dst.exists(),
            "matches_bundle": bool(src_hash and dst_hash and src_hash == dst_hash),
            "bundle_hash": src_hash,
            "deployed_hash": dst_hash,
        }
    return items


def check_hooks(
    *,
    clients: tuple[str, ...] = SUPPORTED_CLIENTS,
    hook_dir: Path | None = None,
    settings_path: Path | None = None,
) -> dict:
    """Read-only hook health report.

    Checks deployed hook copies against bundled source, verifies client configs point
    at current `exomem_*` hooks instead of legacy `kb_*` hooks, and reports where
    logs/cooldown state land. Returns a JSON-serializable report.
    """
    normalized = tuple(_normalize_client(c) for c in clients)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"duplicate clients requested: {clients!r}")
    if (hook_dir or settings_path) and len(normalized) != 1:
        raise ValueError("hook_dir/settings_path overrides require exactly one client")

    reports = []
    for client in normalized:
        hd = Path(hook_dir).expanduser() if hook_dir else _default_hook_dir(client)
        sp = Path(settings_path).expanduser() if settings_path else _default_settings(client)
        data, parse_error = (None, "file does not exist")
        if sp.exists():
            data, parse_error = _read_json(sp)

        checks: list[dict] = []

        def add(id_: str, status: str, message: str, details: dict | None = None) -> None:
            row = {"id": id_, "status": status, "message": message}
            if details is not None:
                row["details"] = details
            checks.append(row)

        add(
            "config.file",
            "pass" if data is not None else "fail",
            f"hook config readable at {sp}" if data is not None else f"hook config unavailable at {sp}: {parse_error}",
            {"path": str(sp), "exists": sp.exists(), "parse_error": parse_error},
        )

        any_legacy = False
        for _py, _sh, event in _HOOK_SPECS:
            for hook in _commands_for_event(data, event):
                if _contains_any(hook, _LEGACY_MARKERS):
                    any_legacy = True
        add(
            "config.legacy",
            "fail" if any_legacy else "pass",
            "legacy kb_* hook entries are still configured" if any_legacy else "no legacy kb_* hook entries configured",
        )

        scripts = {}
        for script, wrapper, event in _HOOK_SPECS:
            entries = _commands_for_event(data, event)
            configured = any(_contains_any(h, (script, wrapper)) for h in entries)
            legacy = [h for h in entries if _contains_any(h, _LEGACY_MARKERS)]
            add(
                f"config.{event}",
                "pass" if configured and not legacy else "fail",
                (
                    f"{event} points at current Exomem hook"
                    if configured and not legacy
                    else f"{event} does not point cleanly at current Exomem hook"
                ),
                {"entries": entries},
            )

            status = _script_status(hd, script, wrapper)
            scripts.update(status)
            stale = [name for name, row in status.items() if not row["matches_bundle"]]
            missing = [name for name, row in status.items() if not row["exists"]]
            if missing:
                add(
                    f"scripts.{event}",
                    "fail",
                    f"{event} deployed hook file(s) missing: {', '.join(missing)}",
                    status,
                )
            elif stale:
                add(
                    f"scripts.{event}",
                    "fail",
                    f"{event} deployed hook file(s) differ from bundled source: {', '.join(stale)}",
                    status,
                )
            else:
                add(f"scripts.{event}", "pass", f"{event} deployed hook files match bundled source", status)

        logs = {
            "capture": _log_summary(client, "capture"),
            "retrieve": _log_summary(client, "retrieve"),
        }
        for kind, row in logs.items():
            add(
                f"log.{kind}",
                "pass" if row["exists"] else "warn",
                (
                    f"{kind} hook log exists at {row['path']}"
                    if row["exists"]
                    else f"{kind} hook log has not been created yet at {row['path']}"
                ),
                row,
            )

        cache = _cache_summary(client)
        add(
            "cache.cooldown",
            "pass" if cache["exists"] else "warn",
            (
                f"cooldown cache exists with {cache['entries']} entries"
                if cache["exists"]
                else f"cooldown cache has not been created yet at {cache['path']}"
            ),
            cache,
        )

        reports.append({
            "client": client,
            "success": not any(c["status"] == "fail" for c in checks),
            "hook_dir": str(hd),
            "settings_path": str(sp),
            "scripts": scripts,
            "logs": logs,
            "cache": cache,
            "checks": checks,
        })

    return {
        "success": not any(not c["success"] for c in reports),
        "clients": reports,
    }


def render_check_human(report: dict) -> str:
    lines = [
        "exomem hook check",
        f"overall: {'PASS' if report.get('success') else 'FAIL'}",
    ]
    for client in report.get("clients", []):
        lines.append("")
        lines.append(client["client"].upper())
        lines.append(f"- config: {client['settings_path']}")
        lines.append(f"- hooks:  {client['hook_dir']}")
        for check in client.get("checks", []):
            label = check["status"].upper()
            lines.append(f"- {label} {check['id']}: {check['message']}")
    return "\n".join(lines)


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
