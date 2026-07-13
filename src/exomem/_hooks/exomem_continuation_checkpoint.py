#!/usr/bin/env python3
"""Local structural continuation checkpoints for supported coding clients.

This module intentionally depends only on the Python standard library so the
deployed copy can run when Exomem, MCP, OAuth, and the network are unavailable.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 4096
MAX_PATH_BYTES = 512
MAX_DIRTY_PATHS = 128
MAX_ARTIFACTS = 16
MAX_OPENSPEC_ARTIFACTS = 8
MAX_INCOMPLETE_LINES = 64
MAX_ARTIFACT_READ_BYTES = 256 * 1024
TRANSCRIPT_SLICE_BYTES = 64 * 1024
RETENTION_NS = 30 * 24 * 60 * 60 * 1_000_000_000
GIT_TIMEOUT_SECONDS = 0.35

_CLIENT_EVENTS = {
    "claude": {
        "PreCompact": {"trigger": {"manual", "auto"}},
        "SessionEnd": {},
        "SessionStart": {"source": {"compact", "resume"}},
    },
    "codex": {
        "PreCompact": {"trigger": {"manual", "auto"}},
        "SessionStart": {"source": {"compact", "resume"}},
    },
}
_ALIASES = {
    "event": ("hook_event_name", "hookEventName"),
    "session_id": ("session_id", "sessionId"),
    "turn_id": ("turn_id", "turnId"),
    "transcript_path": ("transcript_path", "transcriptPath"),
    "cwd": ("cwd",),
    "trigger": ("trigger",),
    "source": ("source",),
    "model": ("model",),
}
_CHECKBOX = re.compile(r"^\s*[-*]\s+\[([ xX])\]")


def _utf8_prefix(value: str, limit: int) -> tuple[str, bool]:
    raw = value.encode("utf-8", "replace")
    if len(raw) <= limit:
        return value, False
    return raw[:limit].decode("utf-8", "ignore"), True


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _alias(payload: Mapping[str, object], logical: str) -> tuple[object | None, bool]:
    present = [(name, payload[name]) for name in _ALIASES[logical] if name in payload]
    if not present:
        return None, False
    first = present[0][1]
    if any(value != first for _, value in present[1:]):
        return None, True
    return first, False


def normalize_event(client: str, payload: Mapping[str, object]) -> dict[str, Any] | None:
    """Map one pinned client envelope to the content-free internal contract."""
    if client not in _CLIENT_EVENTS or not isinstance(payload, Mapping):
        return None
    values: dict[str, object | None] = {}
    for logical in _ALIASES:
        value, conflict = _alias(payload, logical)
        if conflict:
            return None
        values[logical] = value
    event = values["event"]
    session_id = values["session_id"]
    if not isinstance(event, str) or not isinstance(session_id, str) or not session_id:
        return None
    contract = _CLIENT_EVENTS[client].get(event)
    if contract is None:
        return None
    trigger = values["trigger"]
    source = values["source"]
    if "trigger" in contract:
        if trigger not in contract["trigger"] or source is not None:
            return None
    elif trigger is not None:
        return None
    if "source" in contract:
        if source not in contract["source"] or trigger is not None:
            return None
    elif source is not None:
        return None
    optional = ("turn_id", "cwd", "transcript_path", "model")
    if any(values[name] is not None and not isinstance(values[name], str) for name in optional):
        return None
    return {
        "client": client,
        "event": event,
        "session_id": session_id,
        "turn_id": values["turn_id"],
        "trigger": trigger,
        "source": source,
        "cwd": values["cwd"],
        "transcript_path": values["transcript_path"],
        "model": values["model"],
    }


def resolve_home(client: str, environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    shared = env.get("EXOMEM_HOOK_HOME")
    if shared:
        return Path(shared).expanduser()
    if client == "codex":
        return Path(env.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    if client == "claude":
        return Path(env.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")).expanduser()
    raise ValueError(f"unsupported client: {client}")


def client_state_root(home: Path, client: str) -> Path:
    return Path(home) / ".cache" / "exomem-continuation" / client


def session_state_dir(home: Path, client: str, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", session_id).strip("-._") or "session"
    safe, _ = _utf8_prefix(safe, 48)
    digest = _sha256_bytes(f"{client}\0{session_id}".encode())[:20]
    return client_state_root(home, client) / f"{safe}-{digest}"


def _state_root_binding(home: Path, client: str) -> str:
    value = str(client_state_root(home, client).expanduser().absolute())
    return _sha256_bytes(value.encode("utf-8", "surrogatepass"))


def _safe_regular_fd(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _path_binding(path: Path, home: Path) -> dict[str, str]:
    try:
        relative = path.absolute().relative_to(home.absolute()).as_posix()
    except ValueError:
        return {"kind": "sha256", "value": _sha256_bytes(str(path.absolute()).encode("utf-8"))}
    bounded, truncated = _utf8_prefix(relative, MAX_PATH_BYTES)
    if truncated:
        return {"kind": "sha256", "value": _sha256_bytes(relative.encode("utf-8"))}
    return {"kind": "relative", "value": bounded}


def profile_transcript(path_value: str | None, home: Path) -> tuple[dict[str, Any], list[str]]:
    if not path_value:
        return {"available": False}, ["transcript_unavailable"]
    path = Path(path_value).expanduser()
    try:
        fd = _safe_regular_fd(path)
        try:
            info = os.fstat(fd)
            offset = max(0, info.st_size - TRANSCRIPT_SLICE_BYTES)
            os.lseek(fd, offset, os.SEEK_SET)
            raw = os.read(fd, TRANSCRIPT_SLICE_BYTES)
        finally:
            os.close(fd)
    except OSError:
        return {"available": False, "path": _path_binding(path, home)}, ["transcript_unavailable"]
    return {
        "available": True,
        "path": _path_binding(path, home),
        "observed_size": info.st_size,
        "observed_mtime_ns": info.st_mtime_ns,
        "slice_offset": offset,
        "slice_length": len(raw),
        "slice_sha256": _sha256_bytes(raw),
    }, []


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.rstrip("\r\n") if result.returncode == 0 else None


def _bounded_path(value: str) -> tuple[str, bool]:
    return _utf8_prefix(value.replace("\\", "/"), MAX_PATH_BYTES)


def _parse_porcelain_z(raw: str) -> list[str]:
    """Parse porcelain v1 -z records, including paired rename/copy paths."""
    records = raw.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            continue
        status = record[:2]
        paths.append(record[3:])
        if ("R" in status or "C" in status) and index < len(records):
            paired = records[index]
            index += 1
            if paired:
                paths.append(paired)
    return paths


def profile_workspace(
    cwd_value: str | None,
) -> tuple[dict[str, Any], set[str], dict[str, bool], list[str]]:
    truncation: dict[str, bool] = {}
    degradation: list[str] = []
    if not cwd_value:
        return {"available": False}, set(), truncation, ["cwd_unavailable", "non_git"]
    cwd = Path(cwd_value).expanduser()
    root_raw = _git(cwd, "rev-parse", "--show-toplevel")
    if not root_raw:
        name, cut = _bounded_path(cwd.name)
        if cut:
            truncation["workspace_name"] = True
        return {
            "available": False,
            "cwd_name": name,
            "cwd_sha256": _sha256_bytes(str(cwd.absolute()).encode("utf-8")),
        }, set(), truncation, ["non_git"]
    root = Path(root_raw)
    head = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    dirty_raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all") or ""
    all_dirty: list[str] = []
    for value in _parse_porcelain_z(dirty_raw):
        value, cut = _bounded_path(value)
        truncation["dirty_path_bytes"] = truncation.get("dirty_path_bytes", False) or cut
        if value and value not in all_dirty:
            all_dirty.append(value)
    all_dirty.sort()
    artifact_dirty = {value for value in all_dirty if _allowed_artifact_relative(value)}
    dirty = list(all_dirty)
    if len(dirty) > MAX_DIRTY_PATHS:
        dirty = dirty[:MAX_DIRTY_PATHS]
        truncation["dirty_paths"] = True
    root_name, cut = _bounded_path(root.name)
    if cut:
        truncation["workspace_name"] = True
    workspace = {
        "available": True,
        "root": root_name,
        "root_sha256": _sha256_bytes(str(root.absolute()).encode("utf-8")),
        "branch": branch,
        "detached": branch is None,
        "head": head,
        "dirty_paths": dirty,
    }
    if head is None:
        degradation.append("git_head_unavailable")
    return workspace, set(dirty) | artifact_dirty, truncation, degradation


_FIXED_ARTIFACTS = (
    ".superpowers/sdd/progress.md",
    ".task/TASK.md",
    ".task/RESULT.md",
)


def _allowed_artifact_relative(value: str) -> bool:
    if value in _FIXED_ARTIFACTS:
        return True
    if "\\" in value:
        return False
    parts = value.split("/")
    return (
        len(parts) == 4
        and parts[:2] == ["openspec", "changes"]
        and parts[2] not in {"", ".", ".."}
        and parts[3] == "tasks.md"
    )


def _artifact_candidate_relatives(
    root: Any,
    dirty_paths: set[str],
) -> tuple[list[str], bool, bool]:
    candidates = list(_FIXED_ARTIFACTS)
    candidates.extend(sorted(
        value
        for value in dirty_paths
        if value.startswith("openspec/changes/") and _allowed_artifact_relative(value)
    ))
    truncated = False
    unsafe = False
    try:
        with _open_secure_child_directory(root, "openspec", create=False) as openspec:
            with _open_secure_child_directory(openspec, "changes", create=False) as changes:
                entries = sorted(_list_directory(changes))
                truncated = len(entries) > 256
                for name in entries[:256]:
                    try:
                        _validate_child_name(name)
                        mode = _existing_kind(changes, name)
                    except OSError:
                        unsafe = True
                        continue
                    if mode is not None and stat.S_ISDIR(mode):
                        candidates.append(f"openspec/changes/{name}/tasks.md")
                    elif mode is not None and stat.S_ISLNK(mode):
                        unsafe = True
    except FileNotFoundError:
        pass
    except OSError:
        unsafe = True
    return list(dict.fromkeys(candidates)), truncated, unsafe


def _read_regular_relative(
    root: Any,
    relative: str,
    limit: int,
) -> tuple[bytes, os.stat_result, bool]:
    if not _allowed_artifact_relative(relative):
        raise OSError("artifact path is outside the closed allowlist")
    parts = relative.split("/")
    with contextlib.ExitStack() as stack:
        directory = root
        retained: list[tuple[Any, str, Any]] = []
        for component in parts[:-1]:
            child = stack.enter_context(
                _open_secure_child_directory(directory, component, create=False)
            )
            retained.append((directory, component, child))
            directory = child
        fd = _open_secure_file_at(directory, parts[-1], os.O_RDONLY, 0o600)
        try:
            info = os.fstat(fd)
            raw = os.read(fd, limit)
            raced = not _same_file_entry(directory, parts[-1], fd)
        finally:
            os.close(fd)
        raced = raced or any(
            not _same_directory_entry(parent, name, child)
            for parent, name, child in retained
        )
        return raw, info, raced


def collect_artifacts(
    root: Path,
    *,
    dirty_paths: set[str],
) -> tuple[list[dict[str, Any]], dict[str, bool], list[str]]:
    truncation: dict[str, bool] = {}
    degradation: list[str] = []
    profiled: list[dict[str, Any]] = []
    try:
        root_context = _open_secure_directory(root, create=False)
        root_handle = root_context.__enter__()
    except OSError:
        return [], truncation, ["artifact_root_unavailable"]
    try:
        candidates, candidates_truncated, candidates_unsafe = _artifact_candidate_relatives(
            root_handle,
            dirty_paths,
        )
        if candidates_truncated:
            truncation["artifact_candidates"] = True
            degradation.append("artifact_candidates_truncated")
        if candidates_unsafe:
            degradation.append("artifact_unsafe")
        for relative in candidates:
            try:
                raw, info, raced = _read_regular_relative(
                    root_handle,
                    relative,
                    MAX_ARTIFACT_READ_BYTES + 1,
                )
            except FileNotFoundError:
                continue
            except OSError:
                degradation.append("artifact_unsafe")
                continue
            if raced:
                degradation.append("artifact_raced")
            if len(raw) > MAX_ARTIFACT_READ_BYTES:
                degradation.append("artifact_oversized")
                truncation["artifact_bytes"] = True
                continue
            completed = 0
            incomplete = 0
            lines: list[int] = []
            for number, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                match = _CHECKBOX.match(line)
                if not match:
                    continue
                if match.group(1).lower() == "x":
                    completed += 1
                else:
                    incomplete += 1
                    if len(lines) < MAX_INCOMPLETE_LINES:
                        lines.append(number)
            if incomplete > len(lines):
                truncation["incomplete_lines"] = True
            bounded, cut = _bounded_path(relative)
            if cut:
                truncation["artifact_path_bytes"] = True
            is_open = relative.startswith("openspec/changes/")
            is_dirty = relative in dirty_paths and is_open
            if is_open and not is_dirty and incomplete == 0:
                continue
            profiled.append({
                "path": bounded,
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "sha256": _sha256_bytes(raw),
                "completed_count": completed,
                "incomplete_count": incomplete,
                "incomplete_lines": lines,
                "dirty": is_dirty,
                "openspec": is_open,
            })
    finally:
        root_context.__exit__(None, None, None)
    profiled.sort(
        key=lambda row: (
            0 if row["dirty"] and row["openspec"] else 1,
            -row["mtime_ns"],
            row["path"],
        )
    )
    kept: list[dict[str, Any]] = []
    openspec_count = 0
    for row in profiled:
        if row["openspec"]:
            if openspec_count >= MAX_OPENSPEC_ARTIFACTS:
                truncation["openspec_artifacts"] = True
                continue
            openspec_count += 1
        if len(kept) >= MAX_ARTIFACTS:
            truncation["artifacts"] = True
            break
        kept.append({key: value for key, value in row.items() if key not in {"dirty", "openspec"}})
    return kept, truncation, sorted(set(degradation))


def _trim_structural(structural: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(structural))
    while len(_canonical_bytes({"structural": value})) > MAX_CHECKPOINT_BYTES - 1024:
        dirty = value.get("workspace", {}).get("dirty_paths", [])
        artifacts = value.get("artifacts", [])
        if dirty:
            dirty.pop()
            value.setdefault("truncation", {})["checkpoint_dirty_paths"] = True
        elif artifacts:
            artifacts.pop()
            value.setdefault("truncation", {})["checkpoint_artifacts"] = True
        else:
            break
    return value


def finalize_checkpoint(structural: dict[str, Any], *, observed_at_ns: int) -> dict[str, Any]:
    structural = _trim_structural(structural)
    structural_digest = _sha256_bytes(_canonical_bytes(structural))
    transcript = structural.get("transcript", {})
    identity = {
        "schema_version": structural.get("schema_version"),
        "client": structural.get("client"),
        "session_id": structural.get("session_id"),
        "turn_id": structural.get("turn_id"),
        "event": structural.get("event"),
        "trigger": structural.get("trigger"),
        "source": structural.get("source"),
        "transcript_size": transcript.get("observed_size", -1),
        "transcript_mtime_ns": transcript.get("observed_mtime_ns", -1),
        "transcript_slice_sha256": transcript.get("slice_sha256"),
        "structural_digest": structural_digest,
    }
    checkpoint_id = _sha256_bytes(_canonical_bytes(identity))
    event_order = [
        int(transcript.get("observed_mtime_ns", -1)),
        int(transcript.get("observed_size", -1)),
        int(observed_at_ns),
        checkpoint_id,
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "structural_digest": structural_digest,
        "observed_at_ns": int(observed_at_ns),
        "event_order": event_order,
        "structural": structural,
    }


def encode_checkpoint(value: dict[str, Any]) -> bytes:
    encoded = _canonical_bytes(value) + b"\n"
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise ValueError("checkpoint exceeds byte bound")
    return encoded


def build_checkpoint(
    event: Mapping[str, Any],
    home: Path,
    *,
    observed_at_ns: int | None = None,
) -> dict[str, Any]:
    transcript, transcript_degradation = profile_transcript(event.get("transcript_path"), home)
    workspace, dirty, workspace_truncation, workspace_degradation = profile_workspace(
        event.get("cwd")
    )
    artifacts: list[dict[str, Any]] = []
    artifact_truncation: dict[str, bool] = {}
    artifact_degradation: list[str] = []
    if workspace.get("available") and event.get("cwd"):
        root_value = _git(Path(str(event["cwd"])), "rev-parse", "--show-toplevel")
        if root_value:
            artifacts, artifact_truncation, artifact_degradation = collect_artifacts(
                Path(root_value), dirty_paths=dirty
            )
    structural = {
        "schema_version": SCHEMA_VERSION,
        "client": event["client"],
        "session_id": event["session_id"],
        "turn_id": event.get("turn_id"),
        "event": event["event"],
        "trigger": event.get("trigger"),
        "source": event.get("source"),
        "model": event.get("model"),
        "state_root_binding": _state_root_binding(home, event["client"]),
        "workspace": workspace,
        "transcript": transcript,
        "artifacts": artifacts,
        "degradation": sorted(set(
            transcript_degradation + workspace_degradation + artifact_degradation
        )),
        "truncation": {**workspace_truncation, **artifact_truncation},
    }
    return finalize_checkpoint(
        structural,
        observed_at_ns=time.time_ns() if observed_at_ns is None else observed_at_ns,
    )


def _context_line(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def render_continuation(checkpoint: Mapping[str, Any], *, status: str) -> str:
    structural = checkpoint["structural"]
    workspace = structural.get("workspace", {})
    transcript = structural.get("transcript", {})
    lines = [
        "[Exomem continuation checkpoint]",
        f"checkpoint: {checkpoint['checkpoint_id']} ({status})",
    ]
    if workspace:
        lines.append(
            "workspace: "
            f"{_context_line(workspace.get('root') or workspace.get('cwd_name') or 'unavailable')} "
            f"branch={_context_line(workspace.get('branch'))} "
            f"head={_context_line(workspace.get('head'))}"
        )
        dirty = workspace.get("dirty_paths") or []
        if dirty:
            lines.append("dirty paths: " + ", ".join(_context_line(item) for item in dirty))
    if transcript.get("available"):
        lines.append(
            "transcript binding: "
            f"size={transcript.get('observed_size')} offset={transcript.get('slice_offset')} "
            f"length={transcript.get('slice_length')} sha256={transcript.get('slice_sha256')}"
        )
    for artifact in structural.get("artifacts", []):
        lines.append(
            f"artifact: {_context_line(artifact.get('path'))} "
            f"incomplete={artifact.get('incomplete_count')} "
            f"lines={artifact.get('incomplete_lines')}"
        )
    if structural.get("degradation"):
        lines.append("degraded: " + ", ".join(structural["degradation"]))
    if structural.get("truncation"):
        lines.append("truncated: " + ", ".join(sorted(structural["truncation"])))
    advisory = (
        "Reconcile these structural pointers with the client's compacted context. "
        "Reopen cited artifacts and continue from evidence; do not invent missing semantics. "
        "If this work reached a genuine durable stepping-stone, use normal Exomem governance "
        "to capture it; otherwise continue without a memory write. This checkpoint is advisory "
        "and does not prove capture completion."
    )
    prefix = "\n".join(lines)
    required = "\n" + advisory
    budget = MAX_CONTEXT_BYTES - len(required.encode("utf-8"))
    bounded, cut = _utf8_prefix(prefix, max(0, budget))
    if cut:
        bounded = bounded.rstrip() + "\n[structural pointers truncated]"
        bounded, _ = _utf8_prefix(bounded, max(0, budget))
    return bounded + required


def _windows_open_path(
    path: Path,
    *,
    directory: bool,
    access: int = 0x80,
    share: int = 0x3,
    creation: int = 3,
) -> int:
    """Return a non-reparse Win32 handle for one exact path entry."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    handle = create_file(str(path), access, share, None, creation, flags, None)
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(error, f"cannot safely open {path.name}")
        if error in {80, 183}:
            raise FileExistsError(error, f"path already exists: {path.name}")
        raise OSError(error, f"cannot safely open {path.name}")

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    info = _AttributeTagInfo()
    try:
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), f"cannot inspect {path.name}")
        if info.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise OSError("reparse points are not allowed")
        is_directory = bool(info.attributes & 0x10)
        if is_directory != directory:
            raise OSError("unexpected path type")
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    return int(handle)


def _windows_close_handle(handle: int) -> None:
    import ctypes

    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


def _windows_final_path(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_path = kernel32.GetFinalPathNameByHandleW
    get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_path.restype = wintypes.DWORD
    size = get_path(handle, None, 0, 0)
    if not size:
        raise OSError(ctypes.get_last_error(), "cannot resolve retained Windows handle")
    buffer = ctypes.create_unicode_buffer(size + 1)
    written = get_path(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise OSError(ctypes.get_last_error(), "cannot resolve retained Windows handle")
    return buffer.value


def _windows_handle_identity(handle: int) -> tuple[int, int, int]:
    import ctypes
    from ctypes import wintypes

    class _FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("access_time", wintypes.FILETIME),
            ("write_time", wintypes.FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    info = _FileInfo()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    get_info.restype = wintypes.BOOL
    if not get_info(handle, ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), "cannot identify retained Windows handle")
    return info.volume_serial, info.file_index_high, info.file_index_low


def _windows_delete_directory_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    class _Disposition(ctypes.Structure):
        _fields_ = [("delete", wintypes.BOOLEAN)]

    disposition = _Disposition(True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    if not set_info(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
        raise OSError(ctypes.get_last_error(), "handle-relative Windows deletion failed")


class _SecureDirectory:
    def __init__(
        self,
        path: Path,
        *,
        fd: int | None = None,
        windows_handles: list[int] | None = None,
    ) -> None:
        self.path = path
        self.fd = fd
        self.windows_handles = windows_handles or []

    @property
    def windows_handle(self) -> int:
        if not self.windows_handles:
            raise OSError("secure Windows directory handle is unavailable")
        return self.windows_handles[-1]

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        while self.windows_handles:
            _windows_close_handle(self.windows_handles.pop())


def _open_posix_directory(path: Path, *, create: bool, mode: int) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(path.anchor or "/", flags)
    try:
        for part in path.parts[1:]:
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=mode, dir_fd=current_fd)
                child_fd = os.open(part, flags, dir_fd=current_fd)
            if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                os.close(child_fd)
                raise OSError("non-directory path component")
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


@contextlib.contextmanager
def _open_secure_directory(
    path: Path,
    *,
    create: bool,
    mode: int = 0o700,
) -> Any:
    """Pin a non-reparse directory and every Windows ancestor for an operation."""
    absolute = path.expanduser().absolute()
    if os.name != "nt":
        directory = _SecureDirectory(
            absolute,
            fd=_open_posix_directory(absolute, create=create, mode=mode),
        )
    else:
        handles: list[int] = []
        current = Path(absolute.anchor)
        try:
            handles.append(_windows_open_path(current, directory=True))
            for part in absolute.parts[1:]:
                current /= part
                try:
                    handles.append(_windows_open_path(current, directory=True))
                except FileNotFoundError:
                    if not create:
                        raise
                    current.mkdir(mode=mode)
                    handles.append(_windows_open_path(current, directory=True))
            directory = _SecureDirectory(absolute, windows_handles=handles)
        except BaseException:
            while handles:
                _windows_close_handle(handles.pop())
            raise
    try:
        yield directory
    finally:
        directory.close()


def _ensure_secure_dir(path: Path, mode: int = 0o700) -> int | None:
    """Create/open a directory safely; retain a duplicated POSIX descriptor."""
    with _open_secure_directory(path, create=True, mode=mode) as directory:
        return os.dup(directory.fd) if directory.fd is not None else None


def _validate_child_name(name: str) -> None:
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise OSError("state operations require one child basename")


def _normalized_windows_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return value.rstrip("\\/").replace("/", "\\").casefold()


@contextlib.contextmanager
def _open_secure_child_directory(
    parent: _SecureDirectory,
    name: str,
    *,
    create: bool,
    mode: int = 0o700,
    delete_access: bool = False,
) -> Any:
    """Open one child relative to a retained, already-validated parent."""
    _validate_child_name(name)
    child_path = parent.path / name
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=parent.fd)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(name, mode=mode, dir_fd=parent.fd)
            fd = os.open(name, flags, dir_fd=parent.fd)
        directory = _SecureDirectory(child_path, fd=fd)
    else:
        parent_final = _normalized_windows_path(_windows_final_path(parent.windows_handle))
        access = 0x80 | (0x00010000 if delete_access else 0)
        share = 0x3
        try:
            handle = _windows_open_path(
                child_path,
                directory=True,
                access=access,
                share=share,
            )
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(child_path, mode=mode)
            handle = _windows_open_path(
                child_path,
                directory=True,
                access=access,
                share=share,
            )
        child_final = _normalized_windows_path(_windows_final_path(handle))
        if child_final.rsplit("\\", 1)[0] != parent_final:
            _windows_close_handle(handle)
            raise OSError("Windows child escaped its retained parent")
        directory = _SecureDirectory(child_path, windows_handles=[handle])
    try:
        yield directory
    finally:
        directory.close()


def _same_directory_entry(
    parent: _SecureDirectory,
    name: str,
    child: _SecureDirectory,
) -> bool:
    return _directory_entry_identity(parent, name) == _directory_identity(child)


def _directory_identity(directory: _SecureDirectory) -> tuple[object, ...]:
    if os.name != "nt":
        retained = os.fstat(directory.fd)
        return "posix", retained.st_dev, retained.st_ino
    return ("windows", *_windows_handle_identity(directory.windows_handle))


def _directory_entry_identity(
    parent: _SecureDirectory,
    name: str,
) -> tuple[object, ...] | None:
    try:
        if os.name != "nt":
            current = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode):
                return None
            return "posix", current.st_dev, current.st_ino
        current_handle = _windows_open_path(parent.path / name, directory=True)
        try:
            return "windows", *_windows_handle_identity(current_handle)
        finally:
            _windows_close_handle(current_handle)
    except OSError:
        return None


def _same_file_entry(directory: _SecureDirectory, name: str, fd: int) -> bool:
    try:
        if os.name != "nt":
            current = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
            retained = os.fstat(fd)
            return (
                stat.S_ISREG(current.st_mode)
                and current.st_dev == retained.st_dev
                and current.st_ino == retained.st_ino
            )
        import msvcrt

        current_handle = _windows_open_path(directory.path / name, directory=False)
        try:
            return _windows_handle_identity(current_handle) == _windows_handle_identity(
                msvcrt.get_osfhandle(fd)
            )
        finally:
            _windows_close_handle(current_handle)
    except OSError:
        return False


def _open_secure_file_at(
    directory: _SecureDirectory,
    name: str,
    flags: int,
    mode: int = 0o600,
) -> int:
    _validate_child_name(name)
    actual_flags = flags | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if os.name != "nt":
        actual_flags |= getattr(os, "O_NONBLOCK", 0)
        fd = os.open(name, actual_flags, mode, dir_fd=directory.fd)
    else:
        import msvcrt

        access = 0x80000000  # GENERIC_READ
        if flags & os.O_RDWR:
            access = 0xC0000000  # GENERIC_READ | GENERIC_WRITE
        elif flags & os.O_WRONLY:
            access = 0x40000000  # GENERIC_WRITE
        if flags & os.O_CREAT and flags & os.O_EXCL:
            creation = 1  # CREATE_NEW
        elif flags & os.O_CREAT:
            creation = 4  # OPEN_ALWAYS
        else:
            creation = 3  # OPEN_EXISTING
        handle = _windows_open_path(
            directory.path / name,
            directory=False,
            access=access,
            creation=creation,
        )
        crt_flags = flags | getattr(os, "O_BINARY", 0)
        try:
            fd = msvcrt.open_osfhandle(handle, crt_flags)
        except BaseException:
            _windows_close_handle(handle)
            raise
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise OSError("state path is not a regular file")
    if flags & os.O_CREAT:
        try:
            os.fchmod(fd, mode)
        except (AttributeError, OSError):
            pass
    return fd


def _open_secure_file(path: Path, flags: int, mode: int = 0o600) -> int:
    with _open_secure_directory(path.parent, create=True) as directory:
        return _open_secure_file_at(directory, path.name, flags, mode)


def _existing_kind(directory: _SecureDirectory, name: str) -> int | None:
    _validate_child_name(name)
    try:
        if os.name == "nt":
            return os.lstat(directory.path / name).st_mode
        return os.stat(name, dir_fd=directory.fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None


def _windows_rename_at(
    directory: _SecureDirectory,
    source: str,
    destination: str,
    *,
    source_is_directory: bool,
    replace: bool,
) -> None:
    import ctypes
    from ctypes import wintypes

    source_handle = _windows_open_path(
        directory.path / source,
        directory=source_is_directory,
        access=0x00010000 | 0x80,  # DELETE | FILE_READ_ATTRIBUTES
        share=0x7,
    )
    try:
        encoded = destination.encode("utf-16-le")

        class _RenameHeader(ctypes.Structure):
            _fields_ = [
                ("replace", wintypes.BOOLEAN),
                ("root", wintypes.HANDLE),
                ("length", wintypes.DWORD),
            ]

        offset = _RenameHeader.length.offset + ctypes.sizeof(wintypes.DWORD)
        buffer = ctypes.create_string_buffer(offset + len(encoded))
        header = _RenameHeader.from_buffer(buffer)
        header.replace = bool(replace)
        header.root = wintypes.HANDLE(directory.windows_handle)
        header.length = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        rename = kernel32.SetFileInformationByHandle
        rename.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        rename.restype = wintypes.BOOL
        if not rename(source_handle, 3, buffer, len(buffer)):  # FileRenameInfo
            error = ctypes.get_last_error()
            if error in {5, 32}:
                raise PermissionError(error, "Windows entry is held open")
            raise OSError(error, "handle-relative Windows rename failed")
    finally:
        _windows_close_handle(source_handle)


def _replace_at(directory: _SecureDirectory, source: str, destination: str) -> None:
    source_mode = _existing_kind(directory, source)
    destination_mode = _existing_kind(directory, destination)
    if source_mode is None or not stat.S_ISREG(source_mode):
        raise OSError("replacement source is not regular")
    if destination_mode is not None and not stat.S_ISREG(destination_mode):
        raise OSError("replacement destination is not regular")
    if os.name == "nt":
        _windows_rename_at(
            directory,
            source,
            destination,
            source_is_directory=False,
            replace=True,
        )
    else:
        os.replace(
            source,
            destination,
            src_dir_fd=directory.fd,
            dst_dir_fd=directory.fd,
        )
        os.fsync(directory.fd)


def _safe_replace(source: Path, destination: Path) -> None:
    if source.parent.absolute() != destination.parent.absolute():
        raise OSError("atomic replacement must remain in one directory")
    with _open_secure_directory(source.parent, create=True) as directory:
        _replace_at(directory, source.name, destination.name)


def _unlink_at(directory: _SecureDirectory, name: str) -> None:
    mode = _existing_kind(directory, name)
    if mode is None:
        return
    if not stat.S_ISREG(mode):
        raise OSError("refusing to unlink non-regular state")
    if os.name == "nt":
        os.unlink(directory.path / name)
    else:
        os.unlink(name, dir_fd=directory.fd)


def _safe_unlink(path: Path) -> None:
    with _open_secure_directory(path.parent, create=True) as directory:
        _unlink_at(directory, path.name)


class _AdvisoryLock:
    def __init__(
        self,
        path: Path,
        timeout: float,
        *,
        directory: _SecureDirectory | None = None,
        name: str | None = None,
    ) -> None:
        self.path = path
        self.directory = directory
        self.name = name
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> _AdvisoryLock:
        if self.directory is None:
            self.fd = _open_secure_file(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        else:
            self.fd = _open_secure_file_at(
                self.directory,
                str(self.name),
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
        if os.fstat(self.fd).st_size < 1:
            os.lseek(self.fd, 0, os.SEEK_SET)
            os.write(self.fd, b"\0")
            os.fsync(self.fd)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                os.lseek(self.fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    self.__exit__(None, None, None)
                    raise
                if time.monotonic() >= deadline:
                    self.__exit__(None, None, None)
                    raise TimeoutError(f"timed out acquiring {self.path.name}") from None
                time.sleep(0.005 + secrets.randbelow(5) / 1000)

    def __exit__(self, *_: object) -> None:
        if self.fd is None:
            return
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self.fd)
            self.fd = None


def advisory_lock(path: Path, *, timeout: float = 0.5) -> _AdvisoryLock:
    return _AdvisoryLock(path, timeout)


def _advisory_lock_at(
    directory: _SecureDirectory,
    name: str,
    *,
    timeout: float = 0.5,
) -> _AdvisoryLock:
    return _AdvisoryLock(directory.path / name, timeout, directory=directory, name=name)


@contextlib.contextmanager
def _session_lock(home: Path, client: str, session_id: str, *, create: bool) -> Any:
    root = client_state_root(home, client)
    state_name = session_state_dir(home, client, session_id).name
    with _open_secure_directory(root, create=create) as root_handle:
        with _advisory_lock_at(root_handle, ".root.lock"):
            state_context = _open_secure_child_directory(
                root_handle,
                state_name,
                create=create,
            )
            state_handle = state_context.__enter__()
            try:
                session = _advisory_lock_at(state_handle, ".lock")
                session.__enter__()
            except BaseException:
                state_context.__exit__(*sys.exc_info())
                raise
        try:
            yield state_handle
        finally:
            session.__exit__(None, None, None)
            state_context.__exit__(None, None, None)


def _recomputed(checkpoint: Mapping[str, Any]) -> dict[str, Any] | None:
    structural = checkpoint.get("structural")
    observed = checkpoint.get("observed_at_ns")
    if not isinstance(structural, dict) or not isinstance(observed, int):
        return None
    try:
        return finalize_checkpoint(structural, observed_at_ns=observed)
    except (KeyError, TypeError, ValueError):
        return None


def _decode_checkpoint(raw: bytes) -> dict[str, Any] | None:
    try:
        if len(raw) > MAX_CHECKPOINT_BYTES:
            return None
        loaded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict) or loaded.get("schema_version") != SCHEMA_VERSION:
        return None
    recomputed = _recomputed(loaded)
    if recomputed is None:
        return None
    for key in ("checkpoint_id", "structural_digest", "event_order"):
        if loaded.get(key) != recomputed.get(key):
            return None
    return loaded


def load_checkpoint_at(directory: _SecureDirectory, name: str) -> dict[str, Any] | None:
    try:
        fd = _open_secure_file_at(directory, name, os.O_RDONLY, 0o600)
        try:
            raw = os.read(fd, MAX_CHECKPOINT_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        return None
    return _decode_checkpoint(raw)


def load_checkpoint(path: Path) -> dict[str, Any] | None:
    try:
        with _open_secure_directory(path.parent, create=False) as directory:
            return load_checkpoint_at(directory, path.name)
    except OSError:
        return None


def _write_all(fd: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short state write")
        view = view[written:]


def _write_temp(state: _SecureDirectory, checkpoint: Mapping[str, Any]) -> str:
    temporary = f"current.json.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    fd = _open_secure_file_at(
        state,
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        _write_all(fd, encode_checkpoint(dict(checkpoint)))
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            _unlink_at(state, temporary)
        except OSError:
            pass
        raise
    os.close(fd)
    return temporary


def _list_directory(directory: _SecureDirectory) -> list[str]:
    if os.name == "nt":
        return os.listdir(directory.path)
    return os.listdir(directory.fd)


def _cleanup_temporaries(state: _SecureDirectory) -> None:
    try:
        names = _list_directory(state)
    except OSError:
        return
    for name in names:
        if not name.startswith("current.json.tmp-"):
            continue
        try:
            _unlink_at(state, name)
        except OSError:
            continue


_STATE_TEMPORARY = re.compile(r"^current\.json\.tmp-[0-9]+-[0-9a-f]{16}$")


def _recognized_state_name(name: str) -> bool:
    return name in {".lock", "current.json", "previous.json"} or bool(
        _STATE_TEMPORARY.fullmatch(name)
    )


def _recognized_state_directory(state: _SecureDirectory) -> bool:
    try:
        names = _list_directory(state)
    except OSError:
        return False
    for name in names:
        if not _recognized_state_name(name):
            return False
        mode = _existing_kind(state, name)
        if mode is None or not stat.S_ISREG(mode):
            return False
    return True


def _prune_candidate_authorized(
    candidate: Mapping[str, Any],
    home: Path,
    client: str,
    enumerated_name: str,
) -> bool:
    structural = candidate.get("structural")
    if not isinstance(structural, Mapping):
        return False
    session_id = structural.get("session_id")
    return (
        isinstance(session_id, str)
        and bool(session_id)
        and structural.get("client") == client
        and structural.get("state_root_binding") == _state_root_binding(home, client)
        and session_state_dir(home, client, session_id).name == enumerated_name
    )


def write_checkpoint(
    event: Mapping[str, Any],
    home: Path,
    *,
    observed_at_ns: int | None = None,
) -> dict[str, Any]:
    observed = time.time_ns() if observed_at_ns is None else int(observed_at_ns)
    candidate = build_checkpoint(event, home, observed_at_ns=observed)
    with _session_lock(home, str(event["client"]), str(event["session_id"]), create=True) as state:
        _cleanup_temporaries(state)
        current = load_checkpoint_at(state, "current.json")
        if current is not None and current["checkpoint_id"] == candidate["checkpoint_id"]:
            return {"status": "idempotent", "checkpoint_id": candidate["checkpoint_id"]}
        if current is not None and tuple(current["event_order"]) > tuple(candidate["event_order"]):
            return {"status": "stale", "checkpoint_id": candidate["checkpoint_id"]}
        temporary = _write_temp(state, candidate)
        try:
            if current is not None:
                _replace_at(state, "current.json", "previous.json")
            _replace_at(state, temporary, "current.json")
        except BaseException:
            try:
                _unlink_at(state, temporary)
            except OSError:
                pass
            raise
        return {"status": "written", "checkpoint_id": candidate["checkpoint_id"]}


def _binding_matches(candidate: Mapping[str, Any], event: Mapping[str, Any], home: Path) -> bool:
    saved = candidate.get("structural", {}).get("transcript", {})
    if not saved.get("available"):
        return True
    path_value = event.get("transcript_path")
    if not path_value:
        return True
    path = Path(str(path_value)).expanduser()
    if _path_binding(path, home) != saved.get("path"):
        return False
    try:
        fd = _safe_regular_fd(path)
        try:
            info = os.fstat(fd)
            if info.st_size < int(saved["observed_size"]):
                return False
            os.lseek(fd, int(saved["slice_offset"]), os.SEEK_SET)
            raw = os.read(fd, int(saved["slice_length"]))
        finally:
            os.close(fd)
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return len(raw) == int(saved["slice_length"]) and _sha256_bytes(raw) == saved.get(
        "slice_sha256"
    )


def _candidate_matches(
    candidate: Mapping[str, Any],
    event: Mapping[str, Any],
    home: Path,
    now_ns: int,
) -> bool:
    structural = candidate.get("structural", {})
    observed = candidate.get("observed_at_ns")
    if not isinstance(observed, int) or now_ns - observed > RETENTION_NS:
        return False
    if structural.get("client") != event.get("client"):
        return False
    if structural.get("session_id") != event.get("session_id"):
        return False
    if structural.get("state_root_binding") != _state_root_binding(home, str(event["client"])):
        return False
    return _binding_matches(candidate, event, home)


def select_checkpoint(
    event: Mapping[str, Any],
    home: Path,
    *,
    now_ns: int | None = None,
) -> tuple[dict[str, Any], str] | None:
    now = time.time_ns() if now_ns is None else int(now_ns)
    try:
        with _session_lock(
            home,
            str(event["client"]),
            str(event["session_id"]),
            create=False,
        ) as state:
            current = load_checkpoint_at(state, "current.json")
            if current is not None and _candidate_matches(current, event, home, now):
                return current, "current"
            previous = load_checkpoint_at(state, "previous.json")
            if previous is not None and _candidate_matches(previous, event, home, now):
                return previous, "rollback"
    except (OSError, TimeoutError):
        return None
    return None


def _rename_directory_at(root: _SecureDirectory, source: str, destination: str) -> None:
    source_mode = _existing_kind(root, source)
    if source_mode is None or not stat.S_ISDIR(source_mode):
        raise OSError("prune source is not a directory")
    if _existing_kind(root, destination) is not None:
        raise FileExistsError(destination)
    if os.name == "nt":
        _windows_rename_at(
            root,
            source,
            destination,
            source_is_directory=True,
            replace=False,
        )
    else:
        os.replace(source, destination, src_dir_fd=root.fd, dst_dir_fd=root.fd)


def _delete_tombstone_at(
    root: _SecureDirectory,
    name: str,
    expected_identity: tuple[object, ...] | None = None,
) -> bool:
    if not name.startswith(".tombstone-"):
        return False
    try:
        context = _open_secure_child_directory(
            root,
            name,
            create=False,
            delete_access=os.name == "nt",
        )
        tombstone = context.__enter__()
    except OSError:
        return False
    try:
        if expected_identity is not None and _directory_identity(tombstone) != expected_identity:
            return False
        children = _list_directory(tombstone)
        if any(
            not _recognized_state_name(child)
            or not stat.S_ISREG(_existing_kind(tombstone, child) or 0)
            for child in children
        ):
            return False
        for child in children:
            _unlink_at(tombstone, child)
        if os.name == "nt":
            _windows_delete_directory_handle(tombstone.windows_handle)
            return True
        if not _same_directory_entry(root, name, tombstone):
            return False
        os.rmdir(name, dir_fd=root.fd)
        return True
    except OSError:
        return False
    finally:
        context.__exit__(None, None, None)


def prune_expired(
    home: Path,
    client: str,
    *,
    current_session: str,
    now_ns: int | None = None,
    force_fallback: bool = False,
) -> int:
    root = client_state_root(home, client)
    now = time.time_ns() if now_ns is None else int(now_ns)
    current_name = session_state_dir(home, client, current_session).name
    tombstones: list[tuple[str, tuple[object, ...]]] = []
    try:
        root_context = _open_secure_directory(root, create=False)
        root_handle = root_context.__enter__()
    except OSError:
        return 0
    try:
        with _advisory_lock_at(root_handle, ".root.lock"):
            try:
                entries = sorted(_list_directory(root_handle))
            except OSError:
                return 0
            for name in entries:
                if name == current_name or name.startswith("."):
                    continue
                state_context = None
                try:
                    state_context = _open_secure_child_directory(
                        root_handle,
                        name,
                        create=False,
                    )
                    state_handle = state_context.__enter__()
                    lock = _advisory_lock_at(state_handle, ".lock", timeout=0.05)
                    lock.__enter__()
                except (OSError, TimeoutError):
                    if state_context is not None:
                        state_context.__exit__(*sys.exc_info())
                    continue
                locked = True
                state_open = True
                try:
                    if not _recognized_state_directory(state_handle):
                        continue
                    candidates = (
                        load_checkpoint_at(state_handle, "current.json"),
                        load_checkpoint_at(state_handle, "previous.json"),
                    )
                    candidate = next(
                        (
                            value
                            for value in candidates
                            if value is not None
                            and _prune_candidate_authorized(value, home, client, name)
                        ),
                        None,
                    )
                    if (
                        candidate is None
                        or not isinstance(candidate.get("observed_at_ns"), int)
                        or now - int(candidate["observed_at_ns"]) <= RETENTION_NS
                    ):
                        continue
                    state_identity = _directory_identity(state_handle)
                    if _directory_entry_identity(root_handle, name) != state_identity:
                        continue
                    tombstone = f".tombstone-{name}-{secrets.token_hex(8)}"
                    if force_fallback:
                        lock.__exit__(None, None, None)
                        locked = False
                        state_context.__exit__(None, None, None)
                        state_open = False
                    try:
                        _rename_directory_at(root_handle, name, tombstone)
                    except PermissionError:
                        if locked:
                            lock.__exit__(None, None, None)
                            locked = False
                        if state_open:
                            state_context.__exit__(None, None, None)
                            state_open = False
                        _rename_directory_at(root_handle, name, tombstone)
                    if _directory_entry_identity(root_handle, tombstone) != state_identity:
                        if _existing_kind(root_handle, name) is None:
                            try:
                                _rename_directory_at(root_handle, tombstone, name)
                            except OSError:
                                pass
                        continue
                    tombstones.append((tombstone, state_identity))
                finally:
                    if locked:
                        lock.__exit__(None, None, None)
                    if state_open:
                        state_context.__exit__(None, None, None)
        return sum(
            _delete_tombstone_at(root_handle, item, identity)
            for item, identity in tombstones
        )
    finally:
        root_context.__exit__(None, None, None)


def _metadata_log(
    home: Path,
    client: str,
    event: str,
    status: str,
    duration_ms: int,
    *,
    checkpoint_id: str | None = None,
    error_class: str | None = None,
    create_root: bool = True,
) -> None:
    root = client_state_root(home, client)
    row: dict[str, object] = {
        "event": event,
        "status": status,
        "duration_ms": duration_ms,
    }
    if checkpoint_id:
        row["checkpoint_id"] = checkpoint_id
    if error_class:
        row["error_class"] = error_class
    with _open_secure_directory(root, create=create_root) as root_handle:
        fd = _open_secure_file_at(
            root_handle,
            "events.log",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            _write_all(fd, _canonical_bytes(row) + b"\n")
        finally:
            os.close(fd)


def _disabled(environ: Mapping[str, str]) -> bool:
    return environ.get("EXOMEM_CONTINUATION_DISABLE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def dispatch_event(
    client: str,
    payload: Mapping[str, object],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    env = os.environ if environ is None else environ
    event = normalize_event(client, payload)
    if event is None or _disabled(env):
        return None
    home = resolve_home(client, env)
    started = time.monotonic_ns()
    if event["event"] in {"PreCompact", "SessionEnd"}:
        try:
            outcome = write_checkpoint(event, home)
            try:
                prune_expired(home, client, current_session=event["session_id"])
            except BaseException:  # noqa: BLE001 - hook must contain every failure
                pass
            try:
                _metadata_log(
                    home,
                    client,
                    event["event"],
                    str(outcome.get("status", "written")),
                    (time.monotonic_ns() - started) // 1_000_000,
                    checkpoint_id=outcome.get("checkpoint_id"),
                )
            except BaseException:  # noqa: BLE001 - diagnostics cannot block the hook
                pass
        except BaseException as error:  # noqa: BLE001 - hook soft-fail boundary
            try:
                _metadata_log(
                    home,
                    client,
                    event["event"],
                    "error",
                    (time.monotonic_ns() - started) // 1_000_000,
                    error_class=type(error).__name__,
                )
            except BaseException:  # noqa: BLE001 - diagnostics cannot block the hook
                pass
        return None
    try:
        selected = select_checkpoint(event, home)
        if selected is None:
            try:
                _metadata_log(
                    home,
                    client,
                    event["event"],
                    "empty",
                    (time.monotonic_ns() - started) // 1_000_000,
                    create_root=False,
                )
            except BaseException:  # noqa: BLE001 - diagnostics cannot block the hook
                pass
            return None
        candidate, status = selected
        context = render_continuation(candidate, status=status)
        try:
                _metadata_log(
                home,
                client,
                event["event"],
                status,
                (time.monotonic_ns() - started) // 1_000_000,
                checkpoint_id=candidate.get("checkpoint_id"),
            )
        except BaseException:  # noqa: BLE001 - diagnostics cannot block the hook
            pass
    except BaseException as error:  # noqa: BLE001 - hook soft-fail boundary
        try:
            _metadata_log(
                home,
                client,
                event["event"],
                "error",
                (time.monotonic_ns() - started) // 1_000_000,
                error_class=type(error).__name__,
                create_root=False,
            )
        except BaseException:  # noqa: BLE001 - diagnostics cannot block the hook
            pass
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--client", choices=("claude", "codex"), required=True)
    try:
        args = parser.parse_args(argv)
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        output = dispatch_event(args.client, payload)
        if output is not None:
            sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    except BaseException:  # noqa: BLE001 - process must never block the client
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
