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
import os
import re
import secrets
import stat
import time
from pathlib import Path

_HOOK_DIR_SRC = Path(__file__).parent / "_hooks"
# (python script, bash wrapper, hook event) — the hooks this installs.
_HOOK_SPECS = (
    ("exomem_capture_nudge.py", "exomem-capture-nudge.sh", "Stop"),
    ("exomem_retrieve_nudge.py", "exomem-retrieve-nudge.sh", "UserPromptSubmit"),
)
_CONTINUATION_SCRIPT = "exomem_continuation_checkpoint.py"
_CONTINUATION_WRAPPER = "exomem-continuation-checkpoint.sh"
_CONTINUATION_LEGACY = (
    "kb_continuation_checkpoint.py",
    "kb-continuation-checkpoint.sh",
)
_CONTINUATION_EVENTS = {
    "claude": (
        ("PreCompact", "manual|auto"),
        ("SessionEnd", None),
        ("SessionStart", "compact|resume"),
    ),
    "codex": (
        ("PreCompact", "manual|auto"),
        ("SessionStart", "compact|resume"),
    ),
}
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
    "exomem_capture_nudge", "exomem_retrieve_nudge", "exomem-capture-nudge",
    "exomem-retrieve-nudge",
)
_LEGACY_MARKERS = ("kb_capture_nudge", "kb_retrieve_nudge", "kb-capture-nudge", "kb-retrieve-nudge")


def _normalize_client(client: str) -> str:
    value = (client or DEFAULT_CLIENT).strip().lower()
    if value not in SUPPORTED_CLIENTS:
        raise ValueError(f"unsupported hook client {client!r}; expected one of {SUPPORTED_CLIENTS}")
    return value


def _default_hook_dir(client: str) -> Path:
    return _default_home(client) / "hooks"


def _default_settings(client: str) -> Path:
    client = _normalize_client(client)
    return _default_home(client) / ("hooks.json" if client == "codex" else "settings.json")


def _default_home(client: str) -> Path:
    client = _normalize_client(client)
    shared = os.environ.get("EXOMEM_HOOK_HOME")
    if shared:
        return Path(shared).expanduser()
    if client == "codex":
        return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")).expanduser()


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


def _command_windows_for(
    script: str,
    hook_dir: Path,
    *,
    client: str = DEFAULT_CLIENT,
) -> str | None:
    if _normalize_client(client) != "codex":
        return None
    return _windows_python_command(Path(hook_dir).expanduser() / script)


def _hook_entry(item: dict, timeout: int) -> dict:
    entry = {
        "type": "command",
        "command": item["command"],
        "timeout": item.get("timeout", timeout),
    }
    if item.get("commandWindows"):
        entry["commandWindows"] = item["commandWindows"]
    return entry


def _safe_file_status(path: Path) -> dict:
    safe_regular = False
    mode_ok = False
    digest = None
    try:
        listed = os.lstat(path)
        safe_regular = stat.S_ISREG(listed.st_mode) and not stat.S_ISLNK(listed.st_mode)
        mode_ok = safe_regular and (
            os.name == "nt" or not bool(stat.S_IMODE(listed.st_mode) & 0o022)
        )
        if not safe_regular:
            raise OSError("not a safe regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if os.name != "nt":
            flags |= getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        h = hashlib.sha256()
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != listed.st_dev
                or opened.st_ino != listed.st_ino
            ):
                raise OSError("deployed file identity changed")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            digest = h.hexdigest()
        finally:
            os.close(fd)
    except OSError:
        pass
    return {
        "exists": path.exists() or path.is_symlink(),
        "safe_regular": safe_regular,
        "mode_ok": mode_ok,
        "sha256": digest,
    }


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


def _continuation_command(
    hook_dir: Path,
    client: str,
) -> tuple[str, str | None]:
    if client == "codex":
        command = _command_for(
            _CONTINUATION_WRAPPER,
            hook_dir,
            client=client,
            script=_CONTINUATION_SCRIPT,
        )
        windows = _command_windows_for(_CONTINUATION_SCRIPT, hook_dir, client=client)
        return f"{command} --client codex", f"{windows} --client codex"
    command = _command_for(_CONTINUATION_WRAPPER, hook_dir, client=client)
    return f"{command} --client claude", None


def _continuation_items(hook_dir: Path, client: str) -> list[dict]:
    command, command_windows = _continuation_command(hook_dir, client)
    return [
        {
            "kind": "continuation",
            "client": client,
            "event": event,
            "matcher": matcher,
            "script": str(hook_dir / _CONTINUATION_SCRIPT),
            "wrapper": str(hook_dir / _CONTINUATION_WRAPPER),
            "command": command,
            "commandWindows": command_windows,
            "timeout": 5,
        }
        for event, matcher in _CONTINUATION_EVENTS[client]
    ]


def _command_basenames(command: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9_.-]+(?:\.py|\.sh)(?=[\"'\s]|$)", command))


def _has_client_arg(command: str, client: str) -> bool:
    return bool(re.search(rf"(?:^|\s)--client(?:=|\s+){re.escape(client)}(?:\s|$)", command))


def _is_matching_entry(hook: dict, item: dict) -> bool:
    command = f"{hook.get('command', '')} {hook.get('commandWindows', '')}"
    if item.get("kind") != "continuation":
        return any(marker in command for marker in _MARKERS)
    names = _command_basenames(command)
    if names.intersection(_CONTINUATION_LEGACY):
        return True
    current = _CONTINUATION_SCRIPT if item["client"] == "codex" else _CONTINUATION_WRAPPER
    return current in names and _has_client_arg(command, item["client"])


def _configured_item(data: dict | None, item: dict) -> bool:
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    groups = hooks.get(item["event"])
    if not isinstance(groups, list):
        return False
    if item.get("kind") != "continuation":
        return any(
            isinstance(group, dict)
            and group.get("matcher") == item.get("matcher")
            and (item.get("matcher") is not None or "matcher" not in group)
            and isinstance(group.get("hooks"), list)
            and any(
                isinstance(entry, dict) and _is_matching_entry(entry, item)
                for entry in group["hooks"]
            )
            for group in groups
        )
    if "command" not in item:
        return any(
            isinstance(group, dict)
            and isinstance(group.get("hooks"), list)
            and any(
                isinstance(entry, dict) and _is_matching_entry(entry, item)
                for entry in group["hooks"]
            )
            for group in groups
        )
    owned = 0
    exact = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        entries = group.get("hooks")
        if not isinstance(entries, list):
            continue
        matcher_exact = group.get("matcher") == item.get("matcher") and (
            item.get("matcher") is not None or "matcher" not in group
        )
        for entry in entries:
            if not isinstance(entry, dict) or not _is_matching_entry(entry, item):
                continue
            owned += 1
            command_windows_exact = entry.get("commandWindows") == item.get("commandWindows")
            if item.get("commandWindows") is None:
                command_windows_exact = command_windows_exact and "commandWindows" not in entry
            if (
                matcher_exact
                and entry.get("type") == "command"
                and entry.get("command") == item.get("command")
                and command_windows_exact
                and entry.get("timeout") == item.get("timeout")
            ):
                exact += 1
    return owned == 1 and exact == 1


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
    cache = _default_home(client) / ".cache" / "exomem-nudge"
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
    home = _default_home(client)
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
        source = _safe_file_status(src)
        deployed = _safe_file_status(dst)
        src_hash = source["sha256"]
        dst_hash = deployed["sha256"]
        items[name] = {
            "path": str(dst),
            "exists": deployed["exists"],
            "safe_regular": deployed["safe_regular"],
            "mode_ok": deployed["mode_ok"],
            "matches_bundle": bool(
                source["safe_regular"]
                and deployed["safe_regular"]
                and deployed["mode_ok"]
                and src_hash
                and dst_hash
                and src_hash == dst_hash
            ),
            "bundle_hash": src_hash,
            "deployed_hash": dst_hash,
        }
    return items


def _path_mode_ok(path: Path, expected: int, *, directory: bool) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    expected_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return (
        expected_type
        and not stat.S_ISLNK(info.st_mode)
        and (os.name == "nt" or stat.S_IMODE(info.st_mode) == expected)
    )


def _metadata_log_runtime_summary(root: Path) -> dict:
    from ._hooks import exomem_continuation_checkpoint as safe

    path = root / "events.log"
    if not path.exists() and not path.is_symlink():
        return {"path": str(path), "exists": False, "status": "missing", "mode_ok": True}
    mode_ok = _path_mode_ok(path, 0o600, directory=False)
    if not mode_ok:
        return {"path": str(path), "exists": True, "status": "unsafe", "mode_ok": False}
    try:
        with safe._open_secure_directory(root, create=False) as root_handle:
            fd = safe._open_secure_file_at(root_handle, "events.log", os.O_RDONLY, 0o600)
            try:
                raw = os.read(fd, 1024 * 1024 + 1)
            finally:
                os.close(fd)
    except OSError:
        return {"path": str(path), "exists": True, "status": "unsafe", "mode_ok": mode_ok}
    valid = bool(raw) and len(raw) <= 1024 * 1024
    allowed = {"event", "status", "duration_ms", "checkpoint_id", "error_class"}
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            valid = False
            break
        if (
            not isinstance(row, dict)
            or not set(row).issubset(allowed)
            or not isinstance(row.get("event"), str)
            or not isinstance(row.get("status"), str)
            or not isinstance(row.get("duration_ms"), int)
            or any(
                key in row and not isinstance(row[key], str)
                for key in ("checkpoint_id", "error_class")
            )
        ):
            valid = False
            break
    return {
        "path": str(path),
        "exists": True,
        "status": "valid" if valid else "corrupt",
        "mode_ok": mode_ok,
    }


def _continuation_runtime_summary(client: str) -> dict:
    from ._hooks import exomem_continuation_checkpoint as safe

    root = _default_home(client) / ".cache" / "exomem-continuation" / client
    sessions: list[Path] = []
    permission_violations: list[str] = []
    root_exists = root.exists() or root.is_symlink()
    try:
        root_info = os.lstat(root)
        root_safe_directory = stat.S_ISDIR(root_info.st_mode) and not stat.S_ISLNK(
            root_info.st_mode
        )
    except OSError:
        root_safe_directory = False
    if root_exists and not _path_mode_ok(root, 0o700, directory=True):
        permission_violations.append("root")
    try:
        if root_safe_directory:
            sessions = [
                item for item in root.iterdir()
                if not item.is_symlink() and item.is_dir() and not item.name.startswith(".")
            ]
    except OSError:
        sessions = []
    root_lock = root / ".root.lock"
    if root_lock.exists() or root_lock.is_symlink():
        if not _path_mode_ok(root_lock, 0o600, directory=False):
            permission_violations.append(".root.lock")
    observed_times: list[int] = []
    session_states: list[dict] = []
    now = time.time_ns()
    for session in sessions:
        if not _path_mode_ok(session, 0o700, directory=True):
            permission_violations.append("session")
        lock = session / ".lock"
        if not _path_mode_ok(lock, 0o600, directory=False):
            permission_violations.append(".lock")
        for name in ("current.json", "previous.json"):
            path = session / name
            if (path.exists() or path.is_symlink()) and not _path_mode_ok(
                path, 0o600, directory=False
            ):
                permission_violations.append(name)
        try:
            with safe._open_secure_directory(session, create=False) as state_handle:
                current, current_raw = safe.load_checkpoint_status_at(
                    state_handle, "current.json"
                )
                previous, previous_raw = safe.load_checkpoint_status_at(
                    state_handle, "previous.json"
                )
        except OSError:
            current = previous = None
            current_raw = previous_raw = "corrupt"

        def generation_status(value: dict | None, raw_status: str) -> str:
            if raw_status != "valid" or value is None:
                return raw_status
            observed = value.get("observed_at_ns")
            if not isinstance(observed, int):
                return "corrupt"
            observed_times.append(observed)
            return "stale" if now - observed > safe.RETENTION_NS else "valid"

        current_status = generation_status(current, current_raw)
        previous_status = generation_status(previous, previous_raw)
        if current_status == "valid":
            selection = "valid_current"
        elif current_status == "stale":
            selection = "stale"
        elif current_status in {"missing", "corrupt"} and previous_status == "valid":
            selection = "rollback_previous"
        elif current_status == "corrupt" or previous_status == "corrupt":
            selection = "corrupt"
        elif previous_status == "stale":
            selection = "stale"
        else:
            selection = "missing"
        session_states.append({
            "name": session.name,
            "current": current_status,
            "previous": previous_status,
            "selection": selection,
        })
    metadata_log = _metadata_log_runtime_summary(root) if root_safe_directory else {
        "path": str(root / "events.log"),
        "exists": False,
        "status": "missing" if not root_exists else "unsafe",
        "mode_ok": not root_exists,
    }
    if metadata_log["exists"] and not metadata_log["mode_ok"]:
        permission_violations.append("events.log")
    state_counts: dict[str, int] = {}
    for row in session_states:
        state_counts[row["selection"]] = state_counts.get(row["selection"], 0) + 1
    return {
        "path": str(root),
        "exists": root_exists,
        "sessions": len(sessions),
        "latest_age": _fmt_age(max(observed_times, default=None) / 1_000_000_000)
        if observed_times else None,
        "permissions_ok": not permission_violations,
        "permission_violations": sorted(set(permission_violations)),
        "session_states": session_states,
        "state_counts": state_counts,
        "metadata_log": metadata_log,
    }


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
    strict_single_client = len(normalized) == 1
    for client in normalized:
        hd = Path(hook_dir).expanduser() if hook_dir else _default_hook_dir(client)
        sp = Path(settings_path).expanduser() if settings_path else _default_settings(client)
        has_client_footprint = sp.exists() or hd.exists() or sp.parent.exists()
        if not has_client_footprint and not strict_single_client:
            reports.append({
                "client": client,
                "status": "skipped",
                "success": True,
                "hook_dir": str(hd),
                "settings_path": str(sp),
                "scripts": {},
                "logs": {},
                "cache": {},
                "checks": [{
                    "id": "client.installation",
                    "status": "skip",
                    "message": f"{client} is not installed; hook checks skipped",
                }],
            })
            continue
        data, parse_error = (None, "file does not exist")
        if sp.exists():
            data, parse_error = _read_json(sp)

        checks: list[dict] = []

        def add(
            id_: str,
            status: str,
            message: str,
            details: dict | None = None,
            _checks: list[dict] = checks,
        ) -> None:
            row = {"id": id_, "status": status, "message": message}
            if details is not None:
                row["details"] = details
            _checks.append(row)

        add(
            "config.file",
            "pass" if data is not None else "fail",
            (
                f"hook config readable at {sp}"
                if data is not None
                else f"hook config unavailable at {sp}: {parse_error}"
            ),
            {"path": str(sp), "exists": sp.exists(), "parse_error": parse_error},
        )

        any_legacy = False
        for _py, _sh, event in _HOOK_SPECS:
            for hook in _commands_for_event(data, event):
                if _contains_any(hook, _LEGACY_MARKERS):
                    any_legacy = True
        if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
            for event in data["hooks"]:
                for hook in _commands_for_event(data, event):
                    command = f"{hook.get('command', '')} {hook.get('commandWindows', '')}"
                    if _command_basenames(command).intersection(_CONTINUATION_LEGACY):
                        any_legacy = True
        add(
            "config.legacy",
            "fail" if any_legacy else "pass",
            (
                "legacy kb_* hook entries are still configured"
                if any_legacy
                else "no legacy kb_* hook entries configured"
            ),
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
                add(
                    f"scripts.{event}",
                    "pass",
                    f"{event} deployed hook files match bundled source",
                    status,
                )

        continuation_items = _continuation_items(hd, client)
        for item in continuation_items:
            configured = _configured_item(data, item)
            add(
                f"config.{item['event']}",
                "pass" if configured else "fail",
                (
                    f"{item['event']} matches the pinned {client} continuation contract"
                    if configured
                    else f"{item['event']} does not match the pinned {client} continuation contract"
                ),
                {"matcher": item.get("matcher")},
            )
        if client == "codex":
            unsupported = {
                "kind": "continuation",
                "client": "codex",
                "event": "SessionEnd",
                "matcher": None,
            }
            configured = _configured_item(data, unsupported)
            add(
                "config.SessionEnd",
                "fail" if configured else "pass",
                (
                    "Codex SessionEnd must remain unsupported for pinned 0.144.3"
                    if configured
                    else "Codex 0.144.3 has no Exomem SessionEnd registration"
                ),
            )

        continuation_status = _script_status(
            hd, _CONTINUATION_SCRIPT, _CONTINUATION_WRAPPER
        )
        scripts.update(continuation_status)
        continuation_stale = [
            name for name, row in continuation_status.items() if not row["matches_bundle"]
        ]
        add(
            "scripts.continuation",
            "fail" if continuation_stale else "pass",
            (
                "continuation deployed hook files differ or are missing: "
                + ", ".join(continuation_stale)
                if continuation_stale
                else "continuation deployed hook files match bundled source"
            ),
            continuation_status,
        )

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

        continuation_runtime = _continuation_runtime_summary(client)
        corrupt_state = any(
            row["current"] == "corrupt" or row["previous"] == "corrupt"
            for row in continuation_runtime["session_states"]
        )
        degraded_state = any(
            row["selection"] in {"stale", "missing"}
            for row in continuation_runtime["session_states"]
        )
        invalid_log = continuation_runtime["metadata_log"]["status"] in {
            "corrupt", "unsafe"
        }
        if not continuation_runtime["permissions_ok"]:
            runtime_status = "fail"
            runtime_message = "continuation checkpoint permissions are too broad"
        elif corrupt_state or invalid_log:
            runtime_status = "fail"
            runtime_message = "continuation runtime state or metadata log is invalid"
        elif not continuation_runtime["exists"] or continuation_runtime["sessions"] == 0:
            runtime_status = "warn"
            runtime_message = "no continuation checkpoint exists yet; first write event has not run"
        elif degraded_state:
            runtime_status = "warn"
            runtime_message = "continuation runtime state is stale or incomplete"
        else:
            runtime_status = "pass"
            runtime_message = "continuation runtime state is valid and permissioned"
        add(
            "runtime.continuation",
            runtime_status,
            runtime_message,
            continuation_runtime,
        )

        reports.append({
            "client": client,
            "status": (
                "failed" if any(c["status"] == "fail" for c in checks) else "healthy"
            ),
            "success": not any(c["status"] == "fail" for c in checks),
            "hook_dir": str(hd),
            "settings_path": str(sp),
            "scripts": scripts,
            "logs": logs,
            "cache": cache,
            "continuation": continuation_runtime,
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
        if client.get("status") == "skipped":
            lines.append("- SKIP client.installation: client is not installed")
            continue
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
        group = {"hooks": [_hook_entry(item, timeout)]}
        if item.get("matcher") is not None:
            group["matcher"] = item["matcher"]
        hooks.setdefault(item["event"], []).append(group)
    return json.dumps({"hooks": hooks}, indent=2)


def _deploy_file(source: Path, destination: Path) -> None:
    from ._hooks import exomem_continuation_checkpoint as safe

    with safe._open_secure_directory(destination.parent, create=True) as parent:
        existing = safe._existing_kind(parent, destination.name)
        if existing is not None and not stat.S_ISREG(existing):
            raise OSError(f"refusing unsafe hook destination {destination.name}")
        temporary = f".{destination.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
        source_mode = stat.S_IMODE(source.stat().st_mode)
        mode = 0o700 if source_mode & 0o111 else 0o600
        fd = safe._open_secure_file_at(
            parent,
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        try:
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    safe._write_all(fd, chunk)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                safe._unlink_at(parent, temporary)
            except OSError:
                pass
            raise
        os.close(fd)
        safe._replace_at(parent, temporary, destination.name)


def _snapshot_config_at(directory, name: str, display_path: Path) -> dict:
    from ._hooks import exomem_continuation_checkpoint as safe

    kind = safe._existing_kind(directory, name)
    if kind is None:
        return {
            "exists": False,
            "raw": b"",
            "data": {},
            "mode": 0o600,
            "identity": None,
            "digest": hashlib.sha256(b"").hexdigest(),
        }
    if not stat.S_ISREG(kind):
        raise OSError(f"hook config is not a regular file: {display_path}")
    fd = safe._open_secure_file_at(directory, name, os.O_RDONLY, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"hook config is not a regular file: {display_path}")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid hook config at {display_path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(
            f"invalid hook config at {display_path}: top-level JSON is not an object"
        )
    return {
        "exists": True,
        "raw": raw,
        "data": data,
        "mode": info.st_mode & 0o777,
        "identity": (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ),
        "digest": hashlib.sha256(raw).hexdigest(),
    }


def _snapshot_config(path: Path) -> dict:
    from ._hooks import exomem_continuation_checkpoint as safe

    try:
        with safe._open_secure_directory(path.parent, create=False) as directory:
            return _snapshot_config_at(directory, path.name, path)
    except FileNotFoundError:
        return {
            "exists": False,
            "raw": b"",
            "data": {},
            "mode": 0o600,
            "identity": None,
            "digest": hashlib.sha256(b"").hexdigest(),
        }


def _same_snapshot(left: dict, right: dict) -> bool:
    return (
        left["exists"] == right["exists"]
        and left["identity"] == right["identity"]
        and left["digest"] == right["digest"]
    )


def _merged_config(source: dict, installed: list[dict], timeout: int) -> dict:
    data = json.loads(json.dumps(source))
    existing_hooks = data.get("hooks")
    if existing_hooks is None:
        hooks = data["hooks"] = {}
    elif not isinstance(existing_hooks, dict):
        raise ValueError("invalid hook config: 'hooks' is not an object")
    else:
        hooks = existing_hooks

    for item in installed:
        event = item["event"]
        existing = hooks.get(event)
        if existing is None:
            groups = []
        elif not isinstance(existing, list):
            raise ValueError(f"invalid hook config: hooks.{event} is not a list")
        else:
            groups = existing
        kept: list = []
        inserted = False
        replacement: dict = {"hooks": [_hook_entry(item, timeout)]}
        if item.get("matcher") is not None:
            replacement["matcher"] = item["matcher"]
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept.append(group)
                continue
            original = group["hooks"]
            remaining = [
                hook
                for hook in original
                if not (isinstance(hook, dict) and _is_matching_entry(hook, item))
            ]
            removed = len(remaining) != len(original)
            if remaining:
                kept.append({**group, "hooks": remaining})
            if removed and not inserted:
                kept.append(replacement)
                inserted = True
        if not inserted:
            kept.append(replacement)
        hooks[event] = kept
    return data


def _write_unique_at(directory, name: str, raw: bytes, mode: int) -> None:
    from ._hooks import exomem_continuation_checkpoint as safe

    fd = safe._open_secure_file_at(
        directory,
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        mode,
    )
    try:
        safe._write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_unique(path: Path, raw: bytes, mode: int) -> None:
    from ._hooks import exomem_continuation_checkpoint as safe

    with safe._open_secure_directory(path.parent, create=True) as directory:
        _write_unique_at(directory, path.name, raw, mode)


def _merge_hooks(path: Path, installed: list[dict], timeout: int) -> dict:
    """Fail-closed, drift-aware, same-directory atomic hook-config migration."""
    from ._hooks import exomem_continuation_checkpoint as safe

    path = Path(path).expanduser()
    with safe._open_secure_directory(path.parent, create=True) as parent:
        for _attempt in range(3):
            initial = _snapshot_config_at(parent, path.name, path)
            merged = _merged_config(initial["data"], installed, timeout)
            if merged == initial["data"]:
                return {"changed": False, "backup": None}
            observed = _snapshot_config_at(parent, path.name, path)
            if not _same_snapshot(initial, observed):
                continue
            raw = (json.dumps(merged, indent=2) + "\n").encode("utf-8")
            temporary = f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
            _write_unique_at(parent, temporary, raw, initial["mode"])
            latest = _snapshot_config_at(parent, path.name, path)
            if not _same_snapshot(initial, latest):
                safe._unlink_at(parent, temporary)
                continue
            backup_name: str | None = None
            if initial["exists"]:
                stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
                backup_name = f"{path.name}.backup-{stamp}-{secrets.token_hex(6)}"
                _write_unique_at(parent, backup_name, initial["raw"], initial["mode"])
            final = _snapshot_config_at(parent, path.name, path)
            if not _same_snapshot(initial, final):
                safe._unlink_at(parent, temporary)
                if backup_name:
                    safe._unlink_at(parent, backup_name)
                continue
            try:
                safe._replace_at(parent, temporary, path.name)
            except BaseException:
                committed: bool | None = None
                try:
                    replacement = _snapshot_config_at(parent, path.name, path)
                    committed = replacement["raw"] == raw
                    if not committed and not _same_snapshot(initial, replacement):
                        committed = None
                except (OSError, ValueError):
                    committed = None
                try:
                    safe._unlink_at(parent, temporary)
                    if backup_name and committed is False:
                        safe._unlink_at(parent, backup_name)
                except OSError:
                    pass
                raise
            backup = path.parent / backup_name if backup_name else None
            return {"changed": True, "backup": str(backup) if backup else None}
    raise RuntimeError(f"concurrent hook config changes persisted at {path}")


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
    source_specs = list(specs)
    bundled = {
        name
        for py_name, sh_name, _event in source_specs
        for name in (py_name, sh_name)
    }
    if specs is _HOOK_SPECS:
        bundled.update({_CONTINUATION_SCRIPT, _CONTINUATION_WRAPPER})
    for name in bundled:
        if not (_HOOK_DIR_SRC / name).exists():
            raise FileNotFoundError(
                f"bundled hook file missing at {_HOOK_DIR_SRC / name} — "
                "is the exomem install intact?"
            )
    hook_dir = (Path(hook_dir).expanduser() if hook_dir else _default_hook_dir(client))
    from ._hooks import exomem_continuation_checkpoint as safe

    hook_fd = safe._ensure_secure_dir(hook_dir)
    if hook_fd is not None:
        os.close(hook_fd)

    installed: list[dict] = []
    for py_name, sh_name, event in source_specs:
        _deploy_file(_HOOK_DIR_SRC / py_name, hook_dir / py_name)
        _deploy_file(_HOOK_DIR_SRC / sh_name, hook_dir / sh_name)
        installed.append({
            "kind": "nudge",
            "event": event,
            "script": str(hook_dir / py_name),
            "wrapper": str(hook_dir / sh_name),
            "command": _command_for(sh_name, hook_dir, client=client, script=py_name),
            "commandWindows": _command_windows_for(py_name, hook_dir, client=client),
        })
    if specs is _HOOK_SPECS:
        _deploy_file(_HOOK_DIR_SRC / _CONTINUATION_SCRIPT, hook_dir / _CONTINUATION_SCRIPT)
        _deploy_file(_HOOK_DIR_SRC / _CONTINUATION_WRAPPER, hook_dir / _CONTINUATION_WRAPPER)
        installed.extend(_continuation_items(hook_dir, client))

    result = {
        "installed": installed,
        "wired": False,
        "settings": None,
        "client": client,
        "config_changed": False,
        "backup": None,
    }
    if wire:
        sp = (Path(settings_path).expanduser() if settings_path else _default_settings(client))
        migration = _merge_hooks(sp, installed, timeout)
        result["wired"] = True
        result["settings"] = str(sp)
        result["config_changed"] = migration["changed"]
        result["backup"] = migration["backup"]
    return result


def install_all_hooks(*, wire: bool = True, timeout: int = 10) -> dict:
    reports: list[dict] = []
    for client in SUPPORTED_CLIENTS:
        try:
            result = install_hook(client=client, wire=wire, timeout=timeout)
            reports.append({"client": client, "success": True, "result": result})
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
            reports.append({
                "client": client,
                "success": False,
                "error": str(error),
                "error_class": type(error).__name__,
            })
    return {"success": all(row["success"] for row in reports), "clients": reports}
