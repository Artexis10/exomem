"""Operator tooling over the observability surface: `exomem trace <request_id>`
joins every correlated source for one request; `exomem logs tail|grep` reads
the per-process JSONL log files directly. Read-only, best-effort, and never
touches the network.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_FILE_ALIASES: dict[str, str] = {
    "server": "exomem.log",
    "cli": "exomem-cli.log",
    "media": "exomem-media.log",
    "queries": "queries.jsonl",
    "writes": "writes.jsonl",
    "reads": "reads.jsonl",
    "mutations": "mutations.jsonl",
}

# Every file `trace()` joins across, in the order they are read (the final
# sort by timestamp is what actually orders the report).
_TRACE_FILES: tuple[str, ...] = (
    "server",
    "queries",
    "writes",
    "reads",
    "mutations",
)


def resolve_log_file(name: str) -> Path:
    """Resolve a `--file` alias to its path under the current log directory."""
    from .logging_config import resolve_log_dir

    filename = _FILE_ALIASES.get(name)
    if filename is None:
        raise ValueError(
            f"unknown log file {name!r}; choose one of {sorted(_FILE_ALIASES)}"
        )
    return resolve_log_dir() / filename


def _iter_lines(path: Path, *, include_rotated: bool = True) -> list[str]:
    candidates = [path.with_name(path.name + ".1"), path] if include_rotated else [path]
    lines: list[str] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            lines.extend(candidate.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
    return lines


def tail_lines(path: Path, n: int = 20) -> list[str]:
    """The last `n` lines of `path` (live generation only, no rotated tail)."""
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []


def grep_lines(path: Path, pattern: str) -> list[str]:
    """Lines matching `pattern` (regex) across the live and one rotated
    generation, oldest first."""
    regex = re.compile(pattern)
    return [line for line in _iter_lines(path) if regex.search(line)]


def follow_lines(path: Path, *, poll_interval: float = 0.5):
    """Yield newly appended lines forever. Caller controls the loop lifetime
    (e.g. until KeyboardInterrupt)."""
    position = path.stat().st_size if path.exists() else 0
    while True:
        if not path.exists():
            time.sleep(poll_interval)
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                f.seek(position)
                new_text = f.read()
                position = f.tell()
        except OSError:
            time.sleep(poll_interval)
            continue
        if new_text:
            for line in new_text.splitlines():
                if line.strip():
                    yield line
        else:
            time.sleep(poll_interval)


def _record_request_id(record: dict[str, Any]) -> str | None:
    direct = record.get("request_id")
    if isinstance(direct, str):
        return direct
    fields = record.get("fields")
    if isinstance(fields, dict):
        nested = fields.get("request_id")
        if isinstance(nested, str):
            return nested
    return None


def _record_timestamp(record: dict[str, Any]) -> str:
    for key in ("ts_utc", "ts"):
        value = record.get(key)
        if isinstance(value, str):
            return value
    return ""


def trace(request_id: str) -> list[dict[str, Any]]:
    """Join every JSONL source for one request id, time-ordered.

    Best-effort: an unparseable or missing source file is skipped, never
    raised, so a partial log directory still produces a partial trace.
    """
    matched: list[dict[str, Any]] = []
    for alias in _TRACE_FILES:
        path = resolve_log_file(alias)
        for line in _iter_lines(path):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if _record_request_id(record) != request_id:
                continue
            record = dict(record)
            record["_source"] = alias
            matched.append(record)
    matched.sort(key=_record_timestamp)
    return matched
