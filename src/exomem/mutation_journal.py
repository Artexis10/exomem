"""`logs/mutations.jsonl` — one record per mutation attempt.

Written at the terminal/interrupted/replayed seam in `writer_lease.invoke()`,
and for a refusal that reaches a caller without passing it
(`writer_lease.record_caller_refusal`). Best-effort: a journal write failure
must never break a mutation — it is swallowed and counted in
`exomem_log_write_errors_total` instead.

`targets` is content-classified: a hosted cell (`content_private_logging_enabled()`)
records only `target_count`, never the target identifiers themselves.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_JOURNAL_FILENAME = "mutations.jsonl"
_DEFAULT_MAX_MB = 64.0
_STAT_RESYNC_EVERY = 100
_size_cache: dict[str, int] = {}
_append_counts: dict[str, int] = {}


_CLAIMED_ATTR = "_exomem_refusal_claimed"


def _causes(error: BaseException):
    """`error` and the explicit causes it was re-raised from, each once."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__


def claim_refusal(error: BaseException) -> bool:
    """Claim the one row and count a refusal gets, once: `False` if it, or a cause, was claimed.

    A refusal passes several seams on its way to a caller (the dispatcher, the
    store queue, an upload session's commit). The first seam to claim it records
    it. The mark lives on the exception instance, never in `OpError.details`,
    which is the public payload.
    """
    if any(getattr(cause, _CLAIMED_ATTR, False) for cause in _causes(error)):
        return False
    try:
        setattr(error, _CLAIMED_ATTR, True)
    except Exception:  # noqa: BLE001 - an exception that refuses attributes is recorded again, not broken
        pass
    return True


# The code of the park an upload session's retry is repeating, while that retry runs.
_PARKED_CODE: ContextVar[str | None] = ContextVar("exomem_parked_refusal_code", default=None)


@contextmanager
def retrying_park(code: str | None) -> Iterator[None]:
    """Mark the refusals with this code, made while it runs, as repeats of a park already recorded."""
    token = _PARKED_CODE.set(code)
    try:
        yield
    finally:
        _PARKED_CODE.reset(token)


def repeats_parked_refusal(code: str | None) -> bool:
    """Whether a refusal with this code only repeats the park that a retry started from."""
    return code is not None and _PARKED_CODE.get() == code


def refusal_cause(error: BaseException) -> str | None:
    """The `cause` a `MUTATION_BUSY` carries on itself or on its cause: why the boundary was not available."""
    for cause in _causes(error):
        details = getattr(cause, "details", None)
        if isinstance(details, dict) and isinstance(details.get("cause"), str):
            return details["cause"]
    return None


def boundary_wait_ms(error: BaseException) -> float | None:
    """The boundary wait a `MUTATION_BUSY` carries on itself or on its cause.

    A `MUTATION_BUSY` that waited out contention first carries `waited_ms`, its caller's whole
    wait, in place of one attempt's `wait_ms`. Other errors' `waited_ms` (a namespace lock's
    wait) is not a boundary wait.
    """
    for cause in _causes(error):
        details = getattr(cause, "details", None)
        if not isinstance(details, dict):
            continue
        wait = details.get("wait_ms")
        if getattr(cause, "code", None) == "MUTATION_BUSY":
            wait = details.get("waited_ms", wait)
        if isinstance(wait, (int, float)) and not isinstance(wait, bool):
            return float(wait)
    return None


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


def outcomes_for_request(request_id: str) -> list[str]:
    """The outcomes `logs/mutations.jsonl` holds for one request id, oldest first. Empty when it holds none or cannot be read.

    Crash recovery reads this as evidence of what an applier that died did; it never decides from it.
    """
    outcomes: list[str] = []
    path = journal_path()
    for candidate in (path.with_name(path.name + ".1"), path):
        try:
            with candidate.open(encoding="utf-8") as handle:
                for line in handle:
                    if request_id not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("request_id") == request_id and isinstance(row.get("outcome"), str):
                        outcomes.append(row["outcome"])
        except OSError:
            continue
    return outcomes


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
    cause: str | None = None,
    intent_id: str | None = None,
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
        # Only rows that have them carry these, so every earlier row keeps its shape.
        if cause is not None:
            record["cause"] = cause
        if intent_id is not None:
            record["intent_id"] = intent_id
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


#: The outcome each end state of an intent logs, linked to its `accepted` row by `intent_id` (design D10).
INTENT_END_OUTCOMES = {  # nosemgrep: ep-word-set -- a closed map from the journal's own end states to log outcomes
    "committed": "applied",
    "rejected": "rejected",
    "conflicted": "conflicted",
    "discarded": "discarded",
}


def record_intent_end(intent: Any, *, cause: str | None = None) -> None:
    """Log one row when an intent ends, with its accept-to-end time. Never raises."""
    outcome = INTENT_END_OUTCOMES.get(intent.state)
    if outcome is None:
        return
    end = intent.terminal_at or intent.updated_at
    record_mutation(
        request_id=intent.request_id,
        tool=intent.command,
        command=intent.command,
        receipt_id=None,
        outcome=outcome,
        error_code=None,
        duration_ms=round((end - intent.accepted_at) * 1000, 2),
        targets=list(intent.planned_paths or ()),
        cause=cause,
        intent_id=intent.intent_id,
    )
