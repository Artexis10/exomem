"""Rolling recall-latency watch, fed where the call-ledger row is written.

Two latency regressions on the personal service went unnoticed for days
although every row in `ledger.jsonl` carried the evidence (`total_ms` and the
per-stage `spans` that named the cause). Nothing read the ledger back. This
module keeps a bounded, in-memory, content-free ring of recent recall calls,
turns it into a verdict per (tool, client, deep) over a trailing window, and
puts that verdict where it is seen: the `bootstrap` response of the client
that is paying for the latency, one structured log event per breach per hour,
and `exomem doctor`, which reads the ledger on disk through the same
summariser so it sees across restarts.

The ceilings are PROVISIONAL and live here, in one place, on purpose. There is
no environment override: a ceiling that can be raised from the environment is
a ceiling nobody trusts. Revise the constants through a spec change.

Everything here follows the ledger's own rule: a failure in the watch never
raises into, and never measurably slows, the call it observed.
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("exomem.latency_watch")

# --- PROVISIONAL constants (see openspec change watch-recall-latency-from-the-ledger, D2)
RECALL_P90_CEILING_MS = 1000.0
"""`ask_memory` without `deep`, `read_memory`, `find`: the sub-second target."""
DEEP_RECALL_P90_CEILING_MS = 5000.0
"""`ask_memory` with `deep`: the pack stage alone is 3.5 to 4.5 s today."""
MIN_SAMPLES = 20
WINDOW_SECONDS = 86400.0
STARTUP_GRACE_SECONDS = 600.0
REPORT_INTERVAL_SECONDS = 3600.0
RING_SIZE = 2048
DOMINANT_SPANS = 5
WATCHED_TOOLS = frozenset({"ask_memory", "read_memory", "find"})
EVENT = "latency_ceiling_exceeded"


@dataclass(frozen=True, slots=True)
class Sample:
    """One observed call. Content-free by construction: no argument value, path,
    query or excerpt can be stored here because no field exists to hold one."""

    at: float
    tool: str
    client: str
    deep: bool
    total_ms: float
    spans: tuple[tuple[str, float], ...]


def deep_flag(arguments: Any) -> bool:
    """Whether a call's real arguments asked for a deep recall.

    Read from the arguments while the call still has them: the ledger reduces
    every value to a length and a hash, so this is the one point where `deep`
    is a boolean and not a digest. A string spelling of true counts, since a
    connector may send it that way; anything else is not deep.
    """
    if not isinstance(arguments, dict):
        return False
    value = arguments.get("deep")
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"}


def ceiling_for(tool: str, deep: bool) -> float:
    if tool == "ask_memory" and deep:
        return DEEP_RECALL_P90_CEILING_MS
    return RECALL_P90_CEILING_MS


def _top_spans(spans: Iterable[Any] | None) -> tuple[tuple[str, float], ...]:
    out: list[tuple[str, float]] = []
    for span in spans or ():
        if not isinstance(span, dict):
            continue
        name = span.get("name")
        if not isinstance(name, str) or not name:
            continue
        try:
            ms = float(span.get("ms") or 0.0)
        except (TypeError, ValueError):
            continue
        out.append((name, ms))
    out.sort(key=lambda item: -item[1])
    return tuple(out[:DOMINANT_SPANS])


def make_sample(
    *,
    at: float,
    tool: str,
    client: str | None,
    deep: bool,
    total_ms: float,
    spans: Iterable[Any] | None,
) -> Sample:
    return Sample(
        at=float(at),
        tool=str(tool),
        client=str(client or "unknown"),
        deep=bool(deep),
        total_ms=float(total_ms),
        spans=_top_spans(spans),
    )


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending list (non-empty)."""
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[rank - 1]


def summarize(
    samples: Iterable[Sample],
    *,
    now: float,
    window_seconds: float = WINDOW_SECONDS,
    min_samples: int = MIN_SAMPLES,
) -> list[dict[str, Any]]:
    """Per (tool, client, deep) figures over the trailing window, with the
    verdict and the spans that dominate the calls over the ceiling.

    Pure: the ring and the doctor's file reader both come through here, so a
    figure means the same thing on every surface.
    """
    floor = now - window_seconds
    grouped: dict[tuple[str, str, bool], list[Sample]] = collections.defaultdict(list)
    for sample in samples:
        if sample.at < floor or sample.at > now:
            continue
        if sample.tool not in WATCHED_TOOLS:
            continue
        grouped[(sample.tool, sample.client, sample.deep)].append(sample)

    out: list[dict[str, Any]] = []
    for (tool, client, deep), group in sorted(grouped.items()):
        values = sorted(item.total_ms for item in group)
        p50 = _percentile(values, 50)
        p90 = _percentile(values, 90)
        ceiling = ceiling_for(tool, deep)
        breach = len(values) >= min_samples and p90 > ceiling
        by_name: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0])
        for item in group:
            if item.total_ms <= ceiling:
                continue
            for name, ms in item.spans:
                entry = by_name[name]
                entry[0] += ms
                entry[1] += 1
        dominant = sorted(by_name.items(), key=lambda kv: (-kv[1][0], kv[0]))[:DOMINANT_SPANS]
        out.append(
            {
                "tool": tool,
                "client": client,
                "deep": deep,
                "samples": len(values),
                "p50_ms": round(p50),
                "p90_ms": round(p90),
                "ceiling_ms": round(ceiling),
                "breach": breach,
                "dominant_spans": [
                    {"name": name, "ms": round(ms), "calls": int(calls)}
                    for name, (ms, calls) in dominant
                ],
            }
        )
    return out


class Watch:
    """The bounded ring plus the once-per-interval breach event."""

    def __init__(self, *, started_at: float | None = None, clock=time.time) -> None:
        self._clock = clock
        self._started_at = float(clock() if started_at is None else started_at)
        self._ring: collections.deque[Sample] = collections.deque(maxlen=RING_SIZE)
        self._lock = threading.Lock()
        self._reported: dict[tuple[str, str, bool], float] = {}
        self.failures = 0

    @property
    def started_at(self) -> float:
        return self._started_at

    def observe(
        self,
        *,
        tool: str,
        client: str | None,
        deep: bool,
        total_ms: float,
        spans: Iterable[Any] | None = None,
        at: float | None = None,
    ) -> None:
        """Record one call. Never raises; a failure is counted, not surfaced."""
        try:
            self._observe(tool=tool, client=client, deep=deep, total_ms=total_ms, spans=spans, at=at)
        except Exception:  # noqa: BLE001 - the watch must never break a call
            self.failures += 1

    def _observe(self, *, tool, client, deep, total_ms, spans, at) -> None:
        if tool not in WATCHED_TOOLS:
            return
        now = float(self._clock() if at is None else at)
        sample = make_sample(at=now, tool=tool, client=client, deep=deep, total_ms=total_ms, spans=spans)
        with self._lock:
            self._ring.append(sample)
        self._maybe_report(sample, now)

    def _eligible(self, now: float) -> list[Sample]:
        grace_until = self._started_at + STARTUP_GRACE_SECONDS
        with self._lock:
            return [sample for sample in self._ring if sample.at >= grace_until]

    def verdicts(self, *, now: float | None = None, client: str | None = None) -> list[dict[str, Any]]:
        """Figures per (tool, client, deep) for the window, startup grace applied."""
        current = float(self._clock() if now is None else now)
        rows = summarize(self._eligible(current), now=current)
        if client is None:
            return rows
        wanted = str(client or "unknown")
        return [row for row in rows if row["client"] == wanted]

    def breaches(self, *, client: str | None, now: float | None = None) -> list[dict[str, Any]]:
        """This one client's breaches. An unnamed client is the `unknown` bucket,
        never every client: a bootstrap must not report another client's slowness."""
        wanted = str(client or "unknown")
        return [row for row in self.verdicts(now=now, client=wanted) if row["breach"]]

    def _maybe_report(self, sample: Sample, now: float) -> None:
        key = (sample.tool, sample.client, sample.deep)
        last = self._reported.get(key)
        if last is not None and now - last < REPORT_INTERVAL_SECONDS:
            return
        for row in self.verdicts(now=now, client=sample.client):
            if row["tool"] != sample.tool or row["deep"] != sample.deep or not row["breach"]:
                continue
            self._reported[key] = now
            from .log_events import log_event

            log_event(
                log,
                logging.WARNING,
                EVENT,
                fields={
                    "tool": row["tool"],
                    "client": row["client"],
                    "deep": row["deep"],
                    "samples": row["samples"],
                    "p50_ms": row["p50_ms"],
                    "p90_ms": row["p90_ms"],
                    "ceiling_ms": row["ceiling_ms"],
                    "dominant_spans": row["dominant_spans"],
                },
            )
            return


def _true_argument_sha() -> str:
    """The digest the ledger writes for a `deep: true` argument, so a row's deep
    flag is recoverable from its shape without any value ever being stored."""
    import hashlib

    from .call_ledger import canonical_json

    return hashlib.sha256(canonical_json({"v": True})).hexdigest()


def _row_sample(row: Any, *, true_sha: str) -> Sample | None:
    if not isinstance(row, dict):
        return None
    tool = row.get("tool")
    if not isinstance(tool, str) or tool not in WATCHED_TOOLS:
        return None
    try:
        from datetime import datetime

        at = datetime.fromisoformat(str(row.get("ts_utc"))).timestamp()
        total_ms = float(row.get("total_ms") if row.get("total_ms") is not None else row.get("duration_ms"))
    except (TypeError, ValueError):
        return None
    args = row.get("args")
    deep_shape = args.get("deep") if isinstance(args, dict) else None
    deep = isinstance(deep_shape, dict) and deep_shape.get("sha256") == true_sha
    return make_sample(
        at=at,
        tool=tool,
        client=row.get("client_name"),
        deep=deep,
        total_ms=total_ms,
        spans=row.get("spans"),
    )


def samples_from_ledger(
    path: Path,
    *,
    archive_dir: Path | None,
    now: float,
    window_seconds: float = WINDOW_SECONDS,
) -> list[Sample]:
    """Recall samples from the ledger on disk for the trailing window.

    Reads the active file, then archive generations newest first until one
    holds nothing inside the window. Malformed lines are skipped, never
    raised: this feeds a doctor check, and a half-written row is not a
    finding. Nothing but the content-free fields of a row is retained.
    """
    import json

    true_sha = _true_argument_sha()
    floor = now - window_seconds
    out: list[Sample] = []

    def read(file: Path) -> bool:
        """Append this file's in-window samples; True when any row was inside the window."""
        inside = False
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            sample = _row_sample(row, true_sha=true_sha)
            if sample is None:
                continue
            if sample.at >= floor:
                inside = True
                out.append(sample)
        return inside

    read(path)
    if archive_dir is not None and archive_dir.is_dir():
        try:
            generations = sorted(archive_dir.glob("ledger-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        except OSError:
            generations = []
        for generation in generations:
            if not read(generation):
                break
    return out


_WATCH = Watch()


def observe(**kwargs: Any) -> None:
    """Module entry point used beside the ledger write. Never raises."""
    try:
        _WATCH.observe(**kwargs)
    except Exception:  # noqa: BLE001 - belt and braces: even a bad kwarg must not escape
        pass


def bootstrap_block(client: str | None, *, now: float | None = None) -> list[dict[str, Any]] | None:
    """The `latency` block for one client's bootstrap, or None when nothing breaches."""
    try:
        rows = _WATCH.breaches(client=client, now=now)
    except Exception:  # noqa: BLE001 - a watch failure never breaks a bootstrap
        return None
    if not rows:
        return None
    return [
        {key: row[key] for key in ("tool", "deep", "samples", "p50_ms", "p90_ms", "ceiling_ms", "dominant_spans")}
        for row in rows
    ]


def reset(*, started_at: float | None = None, clock=time.time) -> Watch:
    """Replace the process watch (tests, and a service that re-seeds its clock)."""
    global _WATCH
    _WATCH = Watch(started_at=started_at, clock=clock)
    return _WATCH


def current() -> Watch:
    return _WATCH
