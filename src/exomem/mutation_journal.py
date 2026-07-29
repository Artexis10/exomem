"""`logs/mutations.jsonl` — one record per mutation attempt.

Written at the terminal/interrupted/replayed seam in `writer_lease.invoke()`.
Best-effort: a journal write failure must never break a mutation — it is
swallowed and counted in `exomem_log_write_errors_total` instead.

`targets` is content-classified: a hosted cell (`content_private_logging_enabled()`)
records only `target_count`, never the target identifiers themselves.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_JOURNAL_FILENAME = "mutations.jsonl"
_DEFAULT_MAX_MB = 64.0
_STAT_RESYNC_EVERY = 100
_size_cache: dict[str, int] = {}
_append_counts: dict[str, int] = {}


def journal_path() -> Path:
    from .logging_config import resolve_log_dir

    return resolve_log_dir() / _JOURNAL_FILENAME


def _max_bytes() -> int:
    raw = os.environ.get("EXOMEM_JSONL_MAX_MB", "").strip()
    try:
        mb = float(raw) if raw else _DEFAULT_MAX_MB
    except ValueError:
        mb = _DEFAULT_MAX_MB
    return max(1, int(mb * 1024 * 1024))


def _rotate_if_needed(path: Path) -> None:
    key = str(path)
    count = _append_counts.get(key, 0) + 1
    _append_counts[key] = count
    if key not in _size_cache or count % _STAT_RESYNC_EVERY == 0:
        try:
            _size_cache[key] = path.stat().st_size
        except OSError:
            _size_cache[key] = 0
    if _size_cache[key] < _max_bytes():
        return
    try:
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    _size_cache[key] = 0


def record_mutation(
    *,
    request_id: str,
    tool: str,
    command: str,
    receipt_id: str | None,
    outcome: str,
    error_code: str | None,
    duration_ms: float | None,
    boundary_wait_ms: float | None = None,
    boundary_hold_ms: float | None = None,
    lease_role: str | None = None,
    fencing_token: int | None = None,
    replica_id: str | None = None,
    scope: str | None = None,
    targets: list[str] | None = None,
) -> None:
    """Append one mutation-journal record. Never raises, never blocks the
    mutation it describes on a slow or failing disk."""
    try:
        from .privacy_log import content_private_logging_enabled

        hosted = content_private_logging_enabled()
        path = journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        target_list = list(targets or [])
        record: dict[str, Any] = {
            "ts_utc": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
            "request_id": request_id,
            "tool": tool,
            "command": command,
            "receipt_id": receipt_id,
            "outcome": outcome,
            "error_code": error_code,
            "duration_ms": duration_ms,
            "boundary_wait_ms": boundary_wait_ms,
            "boundary_hold_ms": boundary_hold_ms,
            "lease_role": lease_role,
            "fencing_token": fencing_token,
            "replica_id": replica_id,
            "scope": scope,
            "target_count": len(target_list),
        }
        if not hosted:
            record["targets"] = target_list
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
        _size_cache[str(path)] = _size_cache.get(str(path), 0) + len(line.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - a journal write must never break a mutation
        try:
            from . import metrics

            metrics.inc_counter("exomem_log_write_errors_total", {"where": "mutation_journal"})
        except Exception:  # noqa: BLE001
            pass
        log.debug("mutation_journal record failed: %s", exc)
