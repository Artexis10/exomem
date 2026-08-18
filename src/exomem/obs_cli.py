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
    "ledger": "ledger.jsonl",
}

# Every file `trace()` joins across, in the order they are read (the final
# sort by timestamp is what actually orders the report).
#
# `ledger` is the only one of these that covers *every* tool call rather than
# one family, so it is what makes a trace complete: a request whose tool is
# neither a read, a query, nor a mutation used to join nothing but prose.
_TRACE_FILES: tuple[str, ...] = (
    "server",
    "ledger",
    "queries",
    "writes",
    "reads",
    "mutations",
)


def file_aliases() -> tuple[str, ...]:
    """The `--file` aliases, sorted. One list, so the CLI's `choices` cannot
    drift away from what `resolve_log_file` actually accepts."""
    return tuple(sorted(_FILE_ALIASES))


def resolve_log_file(name: str) -> Path:
    """Resolve a `--file` alias to its path under the current log directory."""
    from .logging_config import resolve_log_dir

    filename = _FILE_ALIASES.get(name)
    if filename is None:
        raise ValueError(
            f"unknown log file {name!r}; choose one of {sorted(_FILE_ALIASES)}"
        )
    return resolve_log_dir() / filename


def _ledger_archive_generations() -> list[Path]:
    """The call ledger's archived segments, oldest first.

    The other logs rotate in place to `<name>.1`; the ledger moves its oldest
    rows into a *content-addressed* archive, so filename order says nothing
    about age. The rows carry the order, and the first row of each segment is
    enough to place it -- which matters because a trace that only knew about
    `.1` would silently stop at the live file's first row, the exact gap the
    ledger exists to close.
    """
    from . import call_ledger

    try:
        archive = call_ledger.archive_dir()
        if not archive.is_dir():
            return []
        placed: list[tuple[int, Path]] = []
        for candidate in archive.glob("ledger-*.jsonl"):
            try:
                with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                    first = handle.readline().strip()
                sequence = int(json.loads(first)["sequence"]) if first else 0
            except (OSError, ValueError, KeyError, TypeError):
                sequence = 0
            placed.append((sequence, candidate))
        return [path for _sequence, path in sorted(placed, key=lambda item: item[0])]
    except (OSError, ImportError):
        return []


def _iter_lines(path: Path, *, include_rotated: bool = True) -> list[str]:
    candidates = [path]
    if include_rotated:
        older = (
            _ledger_archive_generations()
            if path.name == _FILE_ALIASES["ledger"]
            else [path.with_name(path.name + ".1")]
        )
        candidates = [*older, path]
    lines: list[str] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            lines.extend(candidate.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
    return lines


def verify_ledger() -> list[str]:
    """Every way the call ledger's hash chain fails to verify. Empty means intact.

    Walks archive-then-live as one sequence, so a break at a rotation boundary
    is reported rather than hidden by verifying each segment in isolation.
    """
    from . import call_ledger

    return call_ledger.verify_lines(
        _iter_lines(call_ledger.ledger_path()), anchored=True
    )


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
