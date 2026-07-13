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
EVENT_CONTRACT_VERSION = 1
OUTPUT_CONTRACT_VERSION = 1
MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 4096
MAX_PATH_BYTES = 512
MAX_IDENTIFIER_BYTES = 512
MAX_DIRTY_PATHS = 128
MAX_ARTIFACTS = 16
MAX_OPENSPEC_ARTIFACTS = 8
MAX_INCOMPLETE_LINES = 64
MAX_ARTIFACT_READ_BYTES = 256 * 1024
MAX_ARTIFACT_CANDIDATE_READS = 64
MAX_DIRTY_ARTIFACT_CANDIDATES = MAX_ARTIFACTS
MAX_PRUNE_CANDIDATES = 16
MAX_PRUNE_LOCK_SECONDS = 0.05
MAX_METADATA_LOG_BYTES = 1024 * 1024
MAX_METADATA_DURATION_MS = 60_000
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
ADAPTER_PROVENANCE = {
    "claude": {
        "version": "2.1.207",
        "source": "https://code.claude.com/docs/en/hooks",
        "events": ("PreCompact", "SessionEnd", "SessionStart"),
    },
    "codex": {
        "version": "0.144.3",
        "source": "https://learn.chatgpt.com/docs/hooks.md",
        "source_fetched": "2026-07-13",
        "events": ("PreCompact", "SessionStart"),
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
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_ERROR_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_METADATA_EVENTS = {"PreCompact", "SessionEnd", "SessionStart"}
_METADATA_STATUSES_WITH_CHECKPOINT = {"written", "idempotent", "stale", "current", "rollback"}
_METADATA_STATUSES = _METADATA_STATUSES_WITH_CHECKPOINT | {"empty", "error"}
_ALLOWED_DEGRADATION = {
    "artifact_candidates_truncated",
    "artifact_oversized",
    "artifact_raced",
    "artifact_root_unavailable",
    "artifact_unsafe",
    "cwd_unavailable",
    "dirty_artifact_candidates_truncated",
    "git_branch_truncated",
    "git_branch_unavailable",
    "git_head_unavailable",
    "git_status_unavailable",
    "model_hashed",
    "model_truncated",
    "non_git",
    "session_id_hashed",
    "transcript_unavailable",
    "turn_id_hashed",
}
_ALLOWED_TRUNCATION = {
    "artifact_bytes",
    "artifact_candidates",
    "artifact_path_bytes",
    "artifacts",
    "branch_bytes",
    "checkpoint_artifacts",
    "checkpoint_dirty_paths",
    "dirty_artifact_candidates",
    "dirty_path_bytes",
    "dirty_paths",
    "incomplete_lines",
    "model_bytes",
    "openspec_artifacts",
    "session_id_bytes",
    "turn_id_bytes",
    "workspace_name",
}


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


def _unsafe_text(value: str) -> bool:
    return any(
        ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value
    )


def _bound_event_identifiers(event: Mapping[str, Any]) -> dict[str, Any]:
    bounded = dict(event)
    normalization = event.get("normalization")
    degradation = (
        list(normalization.get("degradation", [])) if isinstance(normalization, Mapping) else []
    )
    truncation = (
        dict(normalization.get("truncation", {})) if isinstance(normalization, Mapping) else {}
    )

    for key in ("session_id", "turn_id"):
        value = bounded.get(key)
        if not isinstance(value, str):
            continue
        raw = value.encode("utf-8", "surrogatepass")
        if _unsafe_text(value) or len(raw) > MAX_IDENTIFIER_BYTES:
            bounded[key] = "sha256:" + _sha256_bytes(raw)
            truncation[f"{key}_bytes"] = True
            degradation.append(f"{key}_hashed")

    model = bounded.get("model")
    if isinstance(model, str):
        raw = model.encode("utf-8", "surrogatepass")
        if _unsafe_text(model):
            bounded["model"] = "sha256:" + _sha256_bytes(raw)
            truncation["model_bytes"] = True
            degradation.append("model_hashed")
        elif len(raw) > MAX_IDENTIFIER_BYTES:
            bounded["model"], _ = _utf8_prefix(model, MAX_IDENTIFIER_BYTES)
            truncation["model_bytes"] = True
            degradation.append("model_truncated")
    bounded["normalization"] = {
        "degradation": sorted(set(degradation)),
        "truncation": {key: True for key in sorted(truncation) if truncation[key]},
    }
    return bounded


def _alias(payload: Mapping[str, object], logical: str) -> tuple[object | None, bool]:
    present = [(name, payload[name]) for name in _ALIASES[logical] if name in payload]
    if not present:
        return None, False
    first = present[0][1]
    if any(value != first for _, value in present[1:]):
        return None, True
    return first, False


def _normalize_event_contract(
    client: str,
    payload: Mapping[str, object],
) -> dict[str, Any] | None:
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
    return _bound_event_identifiers(
        {
            "contract_version": EVENT_CONTRACT_VERSION,
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
    )


def _adapt_claude_event(payload: Mapping[str, object]) -> dict[str, Any] | None:
    return _normalize_event_contract("claude", payload)


def _adapt_codex_event(payload: Mapping[str, object]) -> dict[str, Any] | None:
    return _normalize_event_contract("codex", payload)


_INPUT_ADAPTERS = {
    "claude": _adapt_claude_event,
    "codex": _adapt_codex_event,
}


def normalize_event(client: str, payload: Mapping[str, object]) -> dict[str, Any] | None:
    """Map one pinned client envelope to the versioned content-free contract."""
    adapter = _INPUT_ADAPTERS.get(client)
    if adapter is None or not isinstance(payload, Mapping):
        return None
    return adapter(payload)


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


def _git_environment() -> dict[str, str]:
    allowed = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TMPDIR", "TEMP", "TMP")
    environment = {name: os.environ[name] for name in allowed if os.environ.get(name)}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _git_probe(cwd: Path, *args: str) -> tuple[str | None, int | None]:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=",
                "-C",
                str(cwd),
                *args,
            ],
            capture_output=True,
            text=True,
            env=_git_environment(),
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    value = result.stdout.rstrip("\r\n") if result.returncode == 0 else None
    return value, result.returncode


def _git(cwd: Path, *args: str) -> str | None:
    value, returncode = _git_probe(cwd, *args)
    return value if returncode == 0 else None


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


def _profile_workspace_with_root(
    cwd_value: str | None,
) -> tuple[dict[str, Any], set[str], dict[str, bool], list[str], Path | None]:
    truncation: dict[str, bool] = {}
    degradation: list[str] = []
    if not cwd_value:
        return {"available": False}, set(), truncation, ["cwd_unavailable", "non_git"], None
    cwd = Path(cwd_value).expanduser()
    root_raw = _git(cwd, "rev-parse", "--show-toplevel")
    if not root_raw:
        name, cut = _bounded_path(cwd.name)
        if cut:
            truncation["workspace_name"] = True
        return (
            {
                "available": False,
                "cwd_name": name,
                "cwd_sha256": _sha256_bytes(str(cwd.absolute()).encode("utf-8")),
            },
            set(),
            truncation,
            ["non_git"],
            None,
        )
    root = Path(root_raw)
    head = _git(root, "rev-parse", "HEAD")
    branch, branch_code = _git_probe(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch_code == 0:
        detached: bool | None = False
        if branch is not None:
            branch, cut = _bounded_path(branch)
            if cut:
                truncation["branch_bytes"] = True
                degradation.append("git_branch_truncated")
    elif branch_code == 1:
        detached = True
    else:
        detached = None
        degradation.append("git_branch_unavailable")
    dirty_raw, status_code = _git_probe(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if status_code != 0:
        dirty_raw = ""
        degradation.append("git_status_unavailable")
    all_dirty: list[str] = []
    for value in _parse_porcelain_z(dirty_raw or ""):
        value, cut = _bounded_path(value)
        if cut:
            truncation["dirty_path_bytes"] = True
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
        "detached": detached,
        "head": head,
        "dirty_paths": dirty,
    }
    if head is None:
        degradation.append("git_head_unavailable")
    return workspace, set(dirty) | artifact_dirty, truncation, degradation, root


def profile_workspace(
    cwd_value: str | None,
) -> tuple[dict[str, Any], set[str], dict[str, bool], list[str]]:
    workspace, dirty, truncation, degradation, _root = _profile_workspace_with_root(cwd_value)
    return workspace, dirty, truncation, degradation


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
) -> tuple[list[str], bool, bool, bool]:
    dirty_candidates = sorted(
        value
        for value in dirty_paths
        if value.startswith("openspec/changes/") and _allowed_artifact_relative(value)
    )
    dirty_truncated = len(dirty_candidates) > MAX_DIRTY_ARTIFACT_CANDIDATES
    candidates = dirty_candidates[:MAX_DIRTY_ARTIFACT_CANDIDATES] + list(_FIXED_ARTIFACTS)
    truncated = False
    unsafe = False
    fallback: list[tuple[int, str]] = []
    try:
        with _open_secure_child_directory(root, "openspec", create=False) as openspec:
            with _open_secure_child_directory(openspec, "changes", create=False) as changes:
                entries = sorted(_list_directory(changes))
                truncated = len(entries) > 256
                for name in entries:
                    try:
                        _validate_child_name(name)
                        mode = _existing_kind(changes, name)
                    except OSError:
                        unsafe = True
                        continue
                    if mode is not None and stat.S_ISDIR(mode):
                        relative = f"openspec/changes/{name}/tasks.md"
                        try:
                            with _open_secure_child_directory(
                                changes, name, create=False
                            ) as change:
                                fd = _open_secure_file_at(change, "tasks.md", os.O_RDONLY, 0o600)
                                try:
                                    info = os.fstat(fd)
                                finally:
                                    os.close(fd)
                                if not _same_directory_entry(changes, name, change):
                                    unsafe = True
                                    continue
                                fallback.append((info.st_mtime_ns, relative))
                        except FileNotFoundError:
                            continue
                        except OSError:
                            unsafe = True
                    elif mode is not None and stat.S_ISLNK(mode):
                        unsafe = True
    except FileNotFoundError:
        pass
    except OSError:
        unsafe = True
    candidates.extend(
        relative for _mtime, relative in sorted(fallback, key=lambda item: (-item[0], item[1]))
    )
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) > MAX_ARTIFACT_CANDIDATE_READS:
        candidates = candidates[:MAX_ARTIFACT_CANDIDATE_READS]
        truncated = True
    return candidates, truncated, unsafe, dirty_truncated


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
            not _same_directory_entry(parent, name, child) for parent, name, child in retained
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
        (
            candidates,
            candidates_truncated,
            candidates_unsafe,
            dirty_candidates_truncated,
        ) = _artifact_candidate_relatives(root_handle, dirty_paths)
        if candidates_truncated:
            truncation["artifact_candidates"] = True
            degradation.append("artifact_candidates_truncated")
        if candidates_unsafe:
            degradation.append("artifact_unsafe")
        if dirty_candidates_truncated:
            truncation["dirty_artifact_candidates"] = True
            degradation.append("dirty_artifact_candidates_truncated")
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
                continue
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
            profiled.append(
                {
                    "path": bounded,
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                    "sha256": _sha256_bytes(raw),
                    "completed_count": completed,
                    "incomplete_count": incomplete,
                    "incomplete_lines": lines,
                    "dirty": is_dirty,
                    "openspec": is_open,
                }
            )
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
    event = _bound_event_identifiers(event)
    transcript, transcript_degradation = profile_transcript(event.get("transcript_path"), home)
    (
        workspace,
        dirty,
        workspace_truncation,
        workspace_degradation,
        artifact_root,
    ) = _profile_workspace_with_root(event.get("cwd"))
    artifacts: list[dict[str, Any]] = []
    artifact_truncation: dict[str, bool] = {}
    artifact_degradation: list[str] = []
    if artifact_root is not None:
        artifacts, artifact_truncation, artifact_degradation = collect_artifacts(
            artifact_root, dirty_paths=dirty
        )
    normalization = event.get("normalization", {})
    normalization_degradation = normalization.get("degradation", [])
    normalization_truncation = normalization.get("truncation", {})
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
        "degradation": sorted(
            set(
                list(normalization_degradation)
                + transcript_degradation
                + workspace_degradation
                + artifact_degradation
            )
        ),
        "truncation": {
            **normalization_truncation,
            **workspace_truncation,
            **artifact_truncation,
        },
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
    advisory = (
        "Reconcile these structural pointers with the client's compacted context. "
        "Reopen cited artifacts and continue from evidence; do not invent missing semantics. "
        "If this work reached a genuine durable stepping-stone, use normal Exomem governance "
        "to capture it; otherwise continue without a memory write. This checkpoint is advisory "
        "and does not prove capture completion."
    )
    content_budget = MAX_CONTEXT_BYTES - len(("\n" + advisory).encode("utf-8"))
    maximum_footer = (
        f"[continuation fields omitted: artifact pointers={MAX_ARTIFACTS}; "
        f"workspace=1; transcript binding=1; dirty paths={MAX_DIRTY_PATHS}]"
    )
    section_budget = content_budget - len(("\n" + maximum_footer).encode("utf-8"))
    lines = [
        "[Exomem continuation checkpoint]",
        f"checkpoint: {checkpoint['checkpoint_id']} ({status})",
    ]

    def append_if_fits(line: str) -> bool:
        candidate = "\n".join([*lines, line])
        if len(candidate.encode("utf-8")) > section_budget:
            return False
        lines.append(line)
        return True

    if structural.get("degradation"):
        append_if_fits("degraded: " + ", ".join(structural["degradation"]))
    if structural.get("truncation"):
        append_if_fits("truncated: " + ", ".join(sorted(structural["truncation"])))
    omissions: dict[str, int] = {}
    if workspace:
        if not append_if_fits(
            "workspace: "
            f"{_context_line(workspace.get('root') or workspace.get('cwd_name') or 'unavailable')} "
            f"branch={_context_line(workspace.get('branch'))} "
            f"head={_context_line(workspace.get('head'))}"
        ):
            omissions["workspace"] = 1
    if transcript.get("available"):
        if not append_if_fits(
            "transcript binding: "
            f"size={transcript.get('observed_size')} offset={transcript.get('slice_offset')} "
            f"length={transcript.get('slice_length')} sha256={transcript.get('slice_sha256')}"
        ):
            omissions["transcript binding"] = 1
    omitted_artifacts = 0
    for artifact in structural.get("artifacts", []):
        if not append_if_fits(
            f"artifact: {_context_line(artifact.get('path'))} "
            f"incomplete={artifact.get('incomplete_count')} "
            f"lines={artifact.get('incomplete_lines')}"
        ):
            omitted_artifacts += 1
    if omitted_artifacts:
        omissions["artifact pointers"] = omitted_artifacts
    dirty = workspace.get("dirty_paths") or []
    if dirty:
        shown: list[str] = []
        for item in dirty:
            candidate = shown + [_context_line(item)]
            line = "dirty paths: " + ", ".join(candidate)
            if len("\n".join([*lines, line]).encode("utf-8")) > section_budget:
                break
            shown = candidate
        if shown:
            append_if_fits("dirty paths: " + ", ".join(shown))
        if len(shown) < len(dirty):
            omissions["dirty paths"] = len(dirty) - len(shown)
    if omissions:
        order = ("artifact pointers", "workspace", "transcript binding", "dirty paths")
        footer = (
            "[continuation fields omitted: "
            + "; ".join(f"{name}={omissions[name]}" for name in order if name in omissions)
            + "]"
        )
        lines.append(footer)
    return "\n".join(lines) + "\n" + advisory


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


def _require_private_state_directory(directory: _SecureDirectory) -> None:
    if os.name == "nt":
        return
    mode = stat.S_IMODE(os.fstat(directory.fd).st_mode)
    if mode != 0o700:
        raise OSError(f"state directory permissions are too broad: {mode:04o}")


def _require_trusted_directory(directory: _SecureDirectory) -> None:
    """Reject hook/config directories writable by another local principal."""
    if os.name == "nt":
        return
    info = os.fstat(directory.fd)
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != os.geteuid() or mode & 0o022:
        raise OSError(
            errno.EPERM,
            f"unsafe writable or foreign-owned directory: {directory.path}",
        )


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


def _session_manifest(
    home: Path,
    client: str,
    session_id: str,
    created_at_ns: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "session",
        "client": client,
        "session_id": session_id,
        "state_name": session_state_dir(home, client, session_id).name,
        "state_root_binding": _state_root_binding(home, client),
        "created_at_ns": int(created_at_ns),
    }


def _ensure_session_manifest(
    state: _SecureDirectory,
    home: Path,
    client: str,
    session_id: str,
    created_at_ns: int,
) -> None:
    fd = _open_secure_file_at(state, ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        raw = os.read(fd, 4097)
        if raw not in {b"", b"\0"}:
            return
        payload = b"\0" + _canonical_bytes(
            _session_manifest(home, client, session_id, created_at_ns)
        )
        os.lseek(fd, 0, os.SEEK_SET)
        _write_all(fd, payload)
        os.ftruncate(fd, len(payload))
        os.fsync(fd)
    finally:
        os.close(fd)


def load_session_manifest_at(
    state: _SecureDirectory,
    home: Path,
    client: str,
    state_name: str,
) -> tuple[dict[str, Any] | None, str]:
    try:
        fd = _open_secure_file_at(state, ".lock", os.O_RDONLY, 0o600)
        try:
            info = os.fstat(fd)
            if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
                return None, "corrupt"
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        if not raw.startswith(b"\0") or len(raw) > 4096:
            return None, "missing"
        value = json.loads(raw[1:])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "corrupt"
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "kind",
        "client",
        "session_id",
        "state_name",
        "state_root_binding",
        "created_at_ns",
    }:
        return None, "corrupt"
    session_id = value.get("session_id")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("kind") != "session"
        or value.get("client") != client
        or not _bounded_safe_text(session_id)
        or value.get("state_name") != state_name
        or value.get("state_root_binding") != _state_root_binding(home, client)
        or session_state_dir(home, client, str(session_id)).name != state_name
        or type(value.get("created_at_ns")) is not int
        or value["created_at_ns"] < 0
    ):
        return None, "corrupt"
    return value, "valid"


@contextlib.contextmanager
def _session_lock(
    home: Path,
    client: str,
    session_id: str,
    *,
    create: bool,
    created_at_ns: int | None = None,
) -> Any:
    root = client_state_root(home, client)
    state_name = session_state_dir(home, client, session_id).name
    with _open_secure_directory(root, create=create) as root_handle:
        _require_private_state_directory(root_handle)
        with _advisory_lock_at(root_handle, ".root.lock"):
            state_existed = _existing_kind(root_handle, state_name) is not None
            state_context = _open_secure_child_directory(
                root_handle,
                state_name,
                create=create,
            )
            state_handle = state_context.__enter__()
            try:
                _require_private_state_directory(state_handle)
                if create and not state_existed:
                    _ensure_session_manifest(
                        state_handle,
                        home,
                        client,
                        session_id,
                        time.time_ns() if created_at_ns is None else int(created_at_ns),
                    )
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


def _bounded_safe_text(value: object, limit: int = MAX_IDENTIFIER_BYTES) -> bool:
    return (
        isinstance(value, str) and not _unsafe_text(value) and len(value.encode("utf-8")) <= limit
    )


def _safe_relative_path(value: object) -> bool:
    if not _bounded_safe_text(value, MAX_PATH_BYTES) or not isinstance(value, str):
        return False
    if value.startswith(("/", "\\")) or "\\" in value:
        return False
    parts = value.split("/")
    return bool(parts) and all(part not in {"", ".", ".."} for part in parts)


def _valid_path_binding(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"kind", "value"}:
        return False
    if value["kind"] == "sha256":
        return isinstance(value["value"], str) and bool(_HEX_64.fullmatch(value["value"]))
    return value["kind"] == "relative" and _safe_relative_path(value["value"])


def _valid_workspace(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("available"), bool):
        return False
    if not value["available"]:
        if not set(value).issubset({"available", "cwd_name", "cwd_sha256"}):
            return False
        if "cwd_name" in value and not _bounded_safe_text(value["cwd_name"], MAX_PATH_BYTES):
            return False
        return "cwd_sha256" not in value or (
            isinstance(value["cwd_sha256"], str) and bool(_HEX_64.fullmatch(value["cwd_sha256"]))
        )
    if set(value) != {
        "available",
        "root",
        "root_sha256",
        "branch",
        "detached",
        "head",
        "dirty_paths",
    }:
        return False
    branch = value["branch"]
    head = value["head"]
    dirty = value["dirty_paths"]
    return (
        _bounded_safe_text(value["root"], MAX_PATH_BYTES)
        and isinstance(value["root_sha256"], str)
        and bool(_HEX_64.fullmatch(value["root_sha256"]))
        and (branch is None or _bounded_safe_text(branch, MAX_PATH_BYTES))
        and (value["detached"] is None or isinstance(value["detached"], bool))
        and (
            head is None
            or isinstance(head, str)
            and (bool(_HEX_40.fullmatch(head)) or bool(_HEX_64.fullmatch(head)))
        )
        and isinstance(dirty, list)
        and len(dirty) <= MAX_DIRTY_PATHS
        and dirty == sorted(set(dirty))
        and all(_safe_relative_path(path) for path in dirty)
    )


def _valid_transcript(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("available"), bool):
        return False
    if not value["available"]:
        return set(value).issubset({"available", "path"}) and (
            "path" not in value or _valid_path_binding(value["path"])
        )
    if set(value) != {
        "available",
        "path",
        "observed_size",
        "observed_mtime_ns",
        "slice_offset",
        "slice_length",
        "slice_sha256",
    }:
        return False
    numeric = (
        value["observed_size"],
        value["observed_mtime_ns"],
        value["slice_offset"],
        value["slice_length"],
    )
    if any(type(item) is not int or item < 0 for item in numeric):
        return False
    return (
        _valid_path_binding(value["path"])
        and value["slice_length"] <= TRANSCRIPT_SLICE_BYTES
        and value["slice_offset"] + value["slice_length"] <= value["observed_size"]
        and isinstance(value["slice_sha256"], str)
        and bool(_HEX_64.fullmatch(value["slice_sha256"]))
    )


def _valid_artifact(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "path",
        "size",
        "mtime_ns",
        "sha256",
        "completed_count",
        "incomplete_count",
        "incomplete_lines",
    }:
        return False
    path = value["path"]
    numbers = (
        value["size"],
        value["mtime_ns"],
        value["completed_count"],
        value["incomplete_count"],
    )
    lines = value["incomplete_lines"]
    return (
        isinstance(path, str)
        and _safe_relative_path(path)
        and _allowed_artifact_relative(path)
        and all(type(number) is int and number >= 0 for number in numbers)
        and isinstance(value["sha256"], str)
        and bool(_HEX_64.fullmatch(value["sha256"]))
        and isinstance(lines, list)
        and len(lines) <= MAX_INCOMPLETE_LINES
        and all(type(line) is int and line > 0 for line in lines)
        and lines == sorted(set(lines))
        and value["incomplete_count"] >= len(lines)
    )


def _valid_structural_contract(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "client",
        "session_id",
        "turn_id",
        "event",
        "trigger",
        "source",
        "model",
        "state_root_binding",
        "workspace",
        "transcript",
        "artifacts",
        "degradation",
        "truncation",
    }:
        return False
    client = value["client"]
    event = value["event"]
    if value["schema_version"] != SCHEMA_VERSION or client not in _CLIENT_EVENTS:
        return False
    if not isinstance(event, str) or event not in _CLIENT_EVENTS[client]:
        return False
    contract = _CLIENT_EVENTS[client][event]
    trigger = value["trigger"]
    source = value["source"]
    if "trigger" in contract:
        if trigger not in contract["trigger"] or source is not None:
            return False
    elif "source" in contract:
        if source not in contract["source"] or trigger is not None:
            return False
    elif trigger is not None or source is not None:
        return False
    if not _bounded_safe_text(value["session_id"]):
        return False
    if value["turn_id"] is not None and not _bounded_safe_text(value["turn_id"]):
        return False
    if value["model"] is not None and not _bounded_safe_text(value["model"]):
        return False
    artifacts = value["artifacts"]
    degradation = value["degradation"]
    truncation = value["truncation"]
    return (
        isinstance(value["state_root_binding"], str)
        and bool(_HEX_64.fullmatch(value["state_root_binding"]))
        and _valid_workspace(value["workspace"])
        and _valid_transcript(value["transcript"])
        and isinstance(artifacts, list)
        and len(artifacts) <= MAX_ARTIFACTS
        and all(_valid_artifact(artifact) for artifact in artifacts)
        and isinstance(degradation, list)
        and degradation == sorted(set(degradation))
        and all(item in _ALLOWED_DEGRADATION for item in degradation)
        and isinstance(truncation, dict)
        and set(truncation).issubset(_ALLOWED_TRUNCATION)
        and all(flag is True for flag in truncation.values())
    )


def _decode_checkpoint(raw: bytes) -> dict[str, Any] | None:
    try:
        if len(raw) > MAX_CHECKPOINT_BYTES:
            return None
        loaded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(loaded, dict)
        or set(loaded)
        != {
            "schema_version",
            "checkpoint_id",
            "structural_digest",
            "observed_at_ns",
            "event_order",
            "structural",
        }
        or loaded.get("schema_version") != SCHEMA_VERSION
        or not isinstance(loaded.get("checkpoint_id"), str)
        or _HEX_64.fullmatch(loaded["checkpoint_id"]) is None
        or not isinstance(loaded.get("structural_digest"), str)
        or _HEX_64.fullmatch(loaded["structural_digest"]) is None
        or type(loaded.get("observed_at_ns")) is not int
        or loaded["observed_at_ns"] < 0
        or not _valid_structural_contract(loaded.get("structural"))
    ):
        return None
    recomputed = _recomputed(loaded)
    if recomputed is None:
        return None
    for key in ("checkpoint_id", "structural_digest", "event_order"):
        if loaded.get(key) != recomputed.get(key):
            return None
    return loaded


def load_checkpoint_status_at(
    directory: _SecureDirectory,
    name: str,
) -> tuple[dict[str, Any] | None, str]:
    if _existing_kind(directory, name) is None:
        return None, "missing"
    try:
        fd = _open_secure_file_at(directory, name, os.O_RDONLY, 0o600)
        try:
            info = os.fstat(fd)
            if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
                return None, "corrupt"
            raw = os.read(fd, MAX_CHECKPOINT_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        return None, "corrupt"
    decoded = _decode_checkpoint(raw)
    return (decoded, "valid") if decoded is not None else (None, "corrupt")


def load_checkpoint_at(directory: _SecureDirectory, name: str) -> dict[str, Any] | None:
    return load_checkpoint_status_at(directory, name)[0]


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
    event = _bound_event_identifiers(event)
    observed = time.time_ns() if observed_at_ns is None else int(observed_at_ns)
    candidate = build_checkpoint(event, home, observed_at_ns=observed)
    with _session_lock(
        home,
        str(event["client"]),
        str(event["session_id"]),
        create=True,
        created_at_ns=observed,
    ) as state:
        _cleanup_temporaries(state)
        current = load_checkpoint_at(state, "current.json")
        if current is not None and current["checkpoint_id"] == candidate["checkpoint_id"]:
            if tuple(candidate["event_order"]) > tuple(current["event_order"]):
                temporary = _write_temp(state, candidate)
                try:
                    _replace_at(state, temporary, "current.json")
                except BaseException:
                    try:
                        _unlink_at(state, temporary)
                    except OSError:
                        pass
                    raise
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
    event = _bound_event_identifiers(event)
    now = time.time_ns() if now_ns is None else int(now_ns)
    try:
        with _session_lock(
            home,
            str(event["client"]),
            str(event["session_id"]),
            create=False,
        ) as state:
            current, current_status = load_checkpoint_status_at(state, "current.json")
            previous, previous_status = load_checkpoint_status_at(state, "previous.json")
            if current_status == "valid":
                if (
                    previous_status == "valid"
                    and current is not None
                    and previous is not None
                    and not _generation_pair_ordered(current, previous)
                ):
                    return None
                if current is not None and _candidate_matches(current, event, home, now):
                    return current, "current"
                return None
            if (
                current_status in {"missing", "corrupt"}
                and previous_status == "valid"
                and previous is not None
                and _candidate_matches(previous, event, home, now)
            ):
                return previous, "rollback"
    except (OSError, TimeoutError):
        return None
    return None


def _generation_pair_ordered(current: Mapping[str, Any], previous: Mapping[str, Any]) -> bool:
    return tuple(current["event_order"]) >= tuple(previous["event_order"])


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


def _tombstone_expired_candidate(
    root_handle: _SecureDirectory,
    home: Path,
    client: str,
    name: str,
    now: int,
    *,
    force_fallback: bool,
) -> tuple[str, tuple[object, ...]] | None:
    state_context = None
    state_open = False
    try:
        state_context = _open_secure_child_directory(root_handle, name, create=False)
        state_handle = state_context.__enter__()
        state_open = True
        _require_private_state_directory(state_handle)
        lock = _advisory_lock_at(state_handle, ".lock", timeout=0.0)
        lock.__enter__()
    except (OSError, TimeoutError):
        if state_open and state_context is not None:
            state_context.__exit__(*sys.exc_info())
        return None
    locked = True
    try:
        if not _recognized_state_directory(state_handle):
            return None
        current = load_checkpoint_at(state_handle, "current.json")
        previous = load_checkpoint_at(state_handle, "previous.json")
        current_authorized = current is not None and _prune_candidate_authorized(
            current, home, client, name
        )
        previous_authorized = previous is not None and _prune_candidate_authorized(
            previous, home, client, name
        )
        if current_authorized and isinstance(current.get("observed_at_ns"), int):
            current_expired = now - int(current["observed_at_ns"]) > RETENTION_NS
            if not current_expired:
                if (
                    previous_authorized
                    and isinstance(previous.get("observed_at_ns"), int)
                    and now - int(previous["observed_at_ns"]) > RETENTION_NS
                ):
                    _unlink_at(state_handle, "previous.json")
                return None
        elif previous_authorized and isinstance(previous.get("observed_at_ns"), int):
            if now - int(previous["observed_at_ns"]) <= RETENTION_NS:
                return None
        else:
            manifest, manifest_status = load_session_manifest_at(state_handle, home, client, name)
            if (
                manifest_status != "valid"
                or manifest is None
                or now - int(manifest["created_at_ns"]) <= RETENTION_NS
            ):
                return None
        state_identity = _directory_identity(state_handle)
        if _directory_entry_identity(root_handle, name) != state_identity:
            return None
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
            return None
        return tombstone, state_identity
    finally:
        if locked:
            lock.__exit__(None, None, None)
        if state_open:
            state_context.__exit__(None, None, None)


def _read_prune_sequence(lock: _AdvisoryLock) -> int:
    if lock.fd is None:
        return 0
    os.lseek(lock.fd, 1, os.SEEK_SET)
    raw = os.read(lock.fd, 8)
    return int.from_bytes(raw, "big") if len(raw) == 8 else 0


def _write_prune_sequence(lock: _AdvisoryLock, value: int) -> None:
    if lock.fd is None:
        return
    os.lseek(lock.fd, 1, os.SEEK_SET)
    _write_all(lock.fd, (value % (1 << 64)).to_bytes(8, "big"))


_TOMBSTONE = re.compile(r"^\.tombstone-(.+)-[0-9a-f]{16}$")


def _authorize_recovery_tombstone(
    root: _SecureDirectory,
    home: Path,
    client: str,
    tombstone_name: str,
    now: int,
) -> tuple[str, tuple[object, ...]] | None:
    match = _TOMBSTONE.fullmatch(tombstone_name)
    if match is None:
        return None
    original_name = match.group(1)
    context = None
    state_open = False
    lock = None
    locked = False
    try:
        context = _open_secure_child_directory(root, tombstone_name, create=False)
        state = context.__enter__()
        state_open = True
        _require_private_state_directory(state)
        lock = _advisory_lock_at(state, ".lock", timeout=0.0)
        lock.__enter__()
        locked = True
    except (OSError, TimeoutError):
        if state_open and context is not None:
            context.__exit__(*sys.exc_info())
        return None
    try:
        if not _recognized_state_directory(state):
            return None
        candidates = (
            load_checkpoint_at(state, "current.json"),
            load_checkpoint_at(state, "previous.json"),
        )
        authorized = next(
            (
                candidate
                for candidate in candidates
                if candidate is not None
                and _prune_candidate_authorized(candidate, home, client, original_name)
                and isinstance(candidate.get("observed_at_ns"), int)
                and now - int(candidate["observed_at_ns"]) > RETENTION_NS
            ),
            None,
        )
        if authorized is None:
            manifest, status_value = load_session_manifest_at(state, home, client, original_name)
            if (
                status_value != "valid"
                or manifest is None
                or now - int(manifest["created_at_ns"]) <= RETENTION_NS
            ):
                return None
        return tombstone_name, _directory_identity(state)
    finally:
        if locked and lock is not None:
            lock.__exit__(None, None, None)
        if state_open and context is not None:
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
    lock_deadline = time.monotonic() + MAX_PRUNE_LOCK_SECONDS
    try:
        root_context = _open_secure_directory(root, create=False)
        root_handle = root_context.__enter__()
        _require_private_state_directory(root_handle)
    except OSError:
        return 0
    try:
        try:
            remaining = max(0.0, lock_deadline - time.monotonic())
            with _advisory_lock_at(root_handle, ".root.lock", timeout=remaining) as root_lock:
                all_entries = [
                    name
                    for name in sorted(_list_directory(root_handle))
                    if name != current_name and not name.startswith(".")
                ]
                if all_entries:
                    sequence = _read_prune_sequence(root_lock)
                    start = sequence % len(all_entries)
                    rotated = all_entries[start:] + all_entries[:start]
                    entries = rotated[:MAX_PRUNE_CANDIDATES]
                    _write_prune_sequence(root_lock, sequence + len(entries))
                else:
                    entries = []
                recovery_names = [
                    name
                    for name in sorted(_list_directory(root_handle))
                    if name.startswith(".tombstone-")
                ][:MAX_PRUNE_CANDIDATES]
                for recovery_name in recovery_names:
                    if time.monotonic() >= lock_deadline:
                        break
                    recovered = _authorize_recovery_tombstone(
                        root_handle, home, client, recovery_name, now
                    )
                    if recovered is not None:
                        tombstones.append(recovered)
        except (OSError, TimeoutError):
            return 0
        for name in entries:
            if time.monotonic() >= lock_deadline:
                break
            try:
                remaining = max(0.0, lock_deadline - time.monotonic())
                with _advisory_lock_at(root_handle, ".root.lock", timeout=remaining):
                    tombstone = _tombstone_expired_candidate(
                        root_handle,
                        home,
                        client,
                        name,
                        now,
                        force_fallback=force_fallback,
                    )
                    if tombstone is not None:
                        tombstones.append(tombstone)
            except (OSError, TimeoutError):
                continue
            if time.monotonic() >= lock_deadline:
                break
            time.sleep(0.001)
        return sum(
            _delete_tombstone_at(root_handle, item, identity) for item, identity in tombstones
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
    if not _valid_metadata_record(row):
        raise ValueError("invalid continuation metadata record")
    with _open_secure_directory(root, create=create_root) as root_handle:
        _require_private_state_directory(root_handle)
        with _advisory_lock_at(root_handle, ".events.lock", timeout=0.5):
            row_bytes = _canonical_bytes(row) + b"\n"
            if _append_metadata_if_room(root_handle, row_bytes):
                return
            records = _read_metadata_records(root_handle)
            encoded = [_canonical_bytes(record) + b"\n" for record in records]
            encoded.append(row_bytes)
            if sum(map(len, encoded)) > MAX_METADATA_LOG_BYTES:
                retained: list[bytes] = []
                retained_bytes = 0
                target = MAX_METADATA_LOG_BYTES // 2
                for item in reversed(encoded):
                    if retained and retained_bytes + len(item) > target:
                        break
                    retained.append(item)
                    retained_bytes += len(item)
                encoded = list(reversed(retained))
            payload = b"".join(encoded)
            temporary = f".events.log.tmp-{os.getpid()}-{secrets.token_hex(8)}"
            fd = _open_secure_file_at(
                root_handle,
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                _write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                _replace_at(root_handle, temporary, "events.log")
            except BaseException:
                _unlink_at(root_handle, temporary, missing_ok=True)
                raise


def _append_metadata_if_room(root_handle: _SecureDirectory, row_bytes: bytes) -> bool:
    kind = _existing_kind(root_handle, "events.log")
    if kind is None:
        return False
    if not stat.S_ISREG(kind):
        raise OSError(errno.EINVAL, "unsafe continuation metadata log")
    fd = _open_secure_file_at(root_handle, "events.log", os.O_RDWR, 0o600)
    try:
        info = os.fstat(fd)
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
            raise OSError(errno.EPERM, "unsafe continuation metadata log mode")
        if info.st_size <= 0 or info.st_size + len(row_bytes) > MAX_METADATA_LOG_BYTES:
            return False
        os.lseek(fd, -1, os.SEEK_END)
        if os.read(fd, 1) != b"\n":
            return False
        os.lseek(fd, 0, os.SEEK_END)
        _write_all(fd, row_bytes)
        return True
    finally:
        os.close(fd)


def _valid_metadata_record(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    required = {"event", "status", "duration_ms"}
    if not required <= value.keys():
        return False
    if value["event"] not in _METADATA_EVENTS or value["status"] not in _METADATA_STATUSES:
        return False
    duration = value["duration_ms"]
    if type(duration) is not int or not 0 <= duration <= MAX_METADATA_DURATION_MS:
        return False
    status = value["status"]
    if status in _METADATA_STATUSES_WITH_CHECKPOINT:
        return (
            set(value) == required | {"checkpoint_id"}
            and isinstance(value["checkpoint_id"], str)
            and _HEX_64.fullmatch(value["checkpoint_id"]) is not None
        )
    if status == "empty":
        return set(value) == required
    return (
        status == "error"
        and set(value) == required | {"error_class"}
        and isinstance(value["error_class"], str)
        and _ERROR_CLASS.fullmatch(value["error_class"]) is not None
    )


def _read_metadata_records(root_handle: _SecureDirectory) -> list[dict[str, object]]:
    kind = _existing_kind(root_handle, "events.log")
    if kind is None:
        return []
    if not stat.S_ISREG(kind):
        raise OSError(errno.EINVAL, "unsafe continuation metadata log")
    fd = _open_secure_file_at(root_handle, "events.log", os.O_RDONLY)
    try:
        info = os.fstat(fd)
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
            raise OSError(errno.EPERM, "unsafe continuation metadata log mode")
        read_limit = MAX_METADATA_LOG_BYTES // 2
        offset = max(0, info.st_size - read_limit)
        os.lseek(fd, offset, os.SEEK_SET)
        raw = os.read(fd, read_limit)
    finally:
        os.close(fd)
    if offset:
        separator = raw.find(b"\n")
        raw = raw[separator + 1 :] if separator >= 0 else b""
    if raw and not raw.endswith(b"\n"):
        separator = raw.rfind(b"\n")
        raw = raw[: separator + 1] if separator >= 0 else b""
    records: list[dict[str, object]] = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if _valid_metadata_record(record):
            records.append(record)
    return records


def _disabled(environ: Mapping[str, str]) -> bool:
    return environ.get("EXOMEM_CONTINUATION_DISABLE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _emit_continuation_output(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if (
        value.get("contract_version") != OUTPUT_CONTRACT_VERSION
        or value.get("kind") != "continuation"
        or value.get("event") != "SessionStart"
        or not isinstance(value.get("additional_context"), str)
    ):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": value["additional_context"],
        }
    }


def _emit_claude_output(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return _emit_continuation_output(value)


def _emit_codex_output(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return _emit_continuation_output(value)


_OUTPUT_ADAPTERS = {
    "claude": _emit_claude_output,
    "codex": _emit_codex_output,
}


def _dispatch_core(
    event: Mapping[str, Any],
    home: Path,
) -> dict[str, Any] | None:
    if event.get("contract_version") != EVENT_CONTRACT_VERSION:
        return None
    client = str(event["client"])
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
        "contract_version": OUTPUT_CONTRACT_VERSION,
        "kind": "continuation",
        "event": "SessionStart",
        "additional_context": context,
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
    neutral = _dispatch_core(event, resolve_home(client, env))
    return _OUTPUT_ADAPTERS[client](neutral)


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
