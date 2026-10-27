"""Advisory latency observations from the existing public boundaries.

The bounded content-free ring and disk readers share one summarizer. Bars come
from the packaged contract; retained or unreadable traffic is never a census.
Observation failures cannot change the request that produced them.
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .request_latency_contract import LatencyBar, LatencyContract, load_contract

log = logging.getLogger("exomem.latency_watch")


def _bar(bar_id: str, contract: LatencyContract | None = None) -> LatencyBar | None:
    try:
        return (contract or load_contract()).bar(bar_id)
    except Exception:  # noqa: BLE001 - malformed contract disables observation only
        return None


# Compatibility exports resolve the old watch values from their sole source.
_ORDINARY = _bar("watch-ordinary")
_DEEP = _bar("watch-deep")
RECALL_P90_CEILING_MS = _ORDINARY.threshold_ms if _ORDINARY else None
DEEP_RECALL_P90_CEILING_MS = _DEEP.threshold_ms if _DEEP else None
MIN_SAMPLES = _ORDINARY.min_samples if _ORDINARY else None
WINDOW_SECONDS = _ORDINARY.window_seconds if _ORDINARY else None
STARTUP_GRACE_SECONDS = 600.0
REPORT_INTERVAL_SECONDS = 3600.0
SWEEP_SECONDS = 600.0
"""How long a key may go without a verdict when only fast calls arrive."""
RING_SIZE = 2048
DOMINANT_SPANS = 5
WATCHED_TOOLS = frozenset(_ORDINARY.operations if _ORDINARY else ())
EVENT = "latency_ceiling_exceeded"


@dataclass(frozen=True, slots=True)
class Sample:
    """One observed call. Content-free by construction: no argument value, path,
    query or excerpt can be stored here because no field exists to hold one."""

    at: float
    tool: str
    client: str
    deep: bool
    total_ms: float | None
    spans: tuple[tuple[str, float], ...]

    bar_id: str | None
    surface: str
    boundary: str
    profile: str
    population: str
    reference: str
    outcome: str
    source: str
    correlated: bool
    invalid_timing: bool
    excluded: bool


#: Every value of `deep` that means "yes". The live path reads it from the real
#: arguments; the doctor recovers it from the ledger's digest of the same value,
#: so the two must agree on exactly this set (`_deep_argument_digests`).
DEEP_TRUE_VALUES: tuple[Any, ...] = (True, "1", "true", "True", "TRUE", "yes", "Yes", "YES")


def deep_flag(arguments: Any) -> bool:
    """Whether a call's real arguments asked for a deep recall.

    Read from the arguments while the call still has them: the ledger reduces
    every value to a length and a hash, so this is the one point where `deep`
    is a boolean and not a digest. The string spellings in `DEEP_TRUE_VALUES`
    count, since a connector may send one; anything else is not deep.
    """
    if not isinstance(arguments, dict):
        return False
    value = arguments.get("deep")
    if value is True:
        return True
    return isinstance(value, str) and value in DEEP_TRUE_VALUES


def ceiling_for(tool: str, deep: bool) -> float | None:
    bar = _bar("watch-deep" if tool == "ask_memory" and deep else "watch-ordinary")
    return bar.threshold_ms if bar else None


def _timing(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        milliseconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return milliseconds if math.isfinite(milliseconds) and milliseconds >= 0 else None


def _top_spans(spans: Iterable[Any] | None) -> tuple[tuple[str, float], ...]:
    out: list[tuple[str, float]] = []
    for span in spans or ():
        if not isinstance(span, dict):
            continue
        name = span.get("name")
        if not isinstance(name, str) or not name:
            continue
        ms = _timing(span.get("ms"))
        if ms is not None:
            out.append((name, ms))
    out.sort(key=lambda item: -item[1])
    return tuple(out[:DOMINANT_SPANS])


@lru_cache(maxsize=1)
def _registered_operations() -> frozenset[str]:
    from .commands import COMMANDS, PRODUCT_COMMANDS

    return frozenset(command.name for command in (*COMMANDS, *PRODUCT_COMMANDS))


def make_sample(
    *,
    at: float,
    tool: str,
    client: str | None,
    deep: bool,
    total_ms: Any,
    spans: Iterable[Any] | None,
    bar_id: str | None = None,
    surface: str | None = None,
    boundary: str | None = None,
    profile: str | None = None,
    population: str | None = None,
    reference: str | None = None,
    outcome: str = "ok",
    source: str = "live-ring",
    correlated: bool = False,
    excluded: bool = False,
    contract: LatencyContract | None = None,
    trusted_tool: bool = False,
) -> Sample:
    # Registry names are trusted metadata; an unknown request name can contain content.
    # The canary sets `trusted_tool`: its case loader admits only a closed set of tool tokens.
    if not trusted_tool and tool not in _registered_operations():
        tool = "unknown-operation"
    if bar_id is None:
        try:
            bar = (contract or load_contract()).observation_bar(surface=surface or "mcp", tool=tool, deep=deep)
        except Exception:  # noqa: BLE001 - unavailable contract is not a caller failure
            bar = None
    else:
        bar = _bar(bar_id, contract)
    # Producer aliases are closed codes from the existing ledger and activation log.
    canonical_outcome = (
        {"success": "ok", "served": "ok"}.get(outcome, outcome)
        if isinstance(outcome, str)
        else "unknown"
    )
    return Sample(
        at=float(at),
        tool=str(tool),
        client=str(client or "unknown"),
        deep=bool(deep),
        total_ms=_timing(total_ms),
        spans=_top_spans(spans),
        bar_id=bar.id if bar else bar_id,
        surface=surface or (bar.surface if bar else "unknown"),
        boundary=boundary or (bar.boundary.id if bar else "unknown"),
        profile=profile or (bar.profile if bar else "unknown"),
        population=population or (bar.population if bar else "unknown"),
        reference=reference or (bar.reference if bar else "unknown"),
        outcome=canonical_outcome,
        source=source,
        correlated=bool(correlated),
        invalid_timing=total_ms is not None and _timing(total_ms) is None,
        excluded=excluded,
    )


def _key(sample: Sample) -> tuple:
    return (
        sample.tool,
        sample.client,
        sample.deep,
        sample.bar_id,
        sample.surface,
        sample.boundary,
        sample.profile,
        sample.population,
        sample.reference,
        sample.source,
    )


def nearest_rank(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending list (non-empty); every latency report and gate uses it."""
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[rank - 1]


def summarize(
    samples: Iterable[Sample],
    *,
    now: float,
    window_seconds: float | None = None,
    min_samples: int | None = None,
    coverage: dict[str, Any] | None = None,
    contract: LatencyContract | None = None,
) -> list[dict[str, Any]]:
    """Independent bar populations, with explicit unknown and partial evidence."""
    source_coverage = coverage or getattr(samples, "coverage", {})
    grouped: dict[tuple, list[Sample]] = collections.defaultdict(list)
    for sample in samples:
        bar = _bar(sample.bar_id, contract) if sample.bar_id else None
        window = window_seconds or (bar.window_seconds if bar else WINDOW_SECONDS)
        if window is None or not math.isfinite(sample.at) or not now - window <= sample.at <= now:
            continue
        grouped[_key(sample)].append(sample)
    out = []
    for _group_key, group in sorted(grouped.items(), key=lambda item: str(item[0])):
        sample = group[0]
        bar = _bar(sample.bar_id, contract) if sample.bar_id else None
        included = [
            item
            for item in group
            if not item.excluded
            and bar
            and item.outcome in bar.outcome_inclusion
            and item.total_ms is not None
        ]
        values = sorted(item.total_ms for item in included)
        unknown = sum(
            item.total_ms is None and not item.invalid_timing and not item.excluded
            for item in group
        )
        invalid = sum(item.invalid_timing for item in group)
        unknown_outcomes = sum(
            item.outcome not in (bar.outcome_inclusion if bar else ()) for item in group
        )
        matches = bool(
            bar
            and sample.surface == bar.surface
            and sample.boundary == bar.boundary.id
            and sample.profile == bar.profile
            and sample.population == bar.population
            and sample.reference == bar.reference
            and bar.covers(sample.tool, sample.deep)
        )
        ceiling = bar.threshold_ms if bar else None
        percentile = nearest_rank(values, bar.percentile) if values and bar else None
        breach = bool(
            matches
            and ceiling is not None
            and percentile is not None
            and len(values) >= (min_samples or bar.min_samples)
            and (percentile >= ceiling if bar.comparison == "<" else percentile > ceiling)
        )
        info = dict(source_coverage)
        info.setdefault("population", "retained" if sample.source == "live-ring" else "read-source")
        info.setdefault("oldest_retained_at", min(item.at for item in group))
        info.setdefault("evictions", None if sample.source == "live-ring" else 0)
        partial = bool(
            info.get("partial_reads")
            or info.get("failed_reads")
            or info.get("malformed_rows")
            or info.get("unknown_window_rows")
            or info.get("observation_failures")
            or info.get("evictions") is None
            or info.get("evictions", 0) > 0
        )
        if not matches:
            verdict = "unavailable"
        elif ceiling is None:
            verdict = "pending"
        elif breach:
            verdict = "failed"
        elif unknown or invalid or unknown_outcomes:
            verdict = "unavailable"
        elif len(values) < (min_samples or bar.min_samples):
            verdict = "insufficient"
        elif partial:
            verdict = "partial"
        else:
            verdict = "observed-within-bar"
        by_name: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0])
        for item in included:
            if ceiling is None or item.total_ms <= ceiling:
                continue
            for name, ms in item.spans:
                by_name[name][0] += ms
                by_name[name][1] += 1
        dominant = sorted(by_name.items(), key=lambda kv: (-kv[1][0], kv[0]))[:DOMINANT_SPANS]
        out.append(
            {
                "tool": sample.tool,
                "client": sample.client,
                "deep": sample.deep,
                "contract_revision": contract.revision if contract else contract_revision(),
                "bar_id": sample.bar_id,
                "surface": sample.surface,
                "boundary": sample.boundary,
                "boundary_start": bar.boundary.start if bar else None,
                "boundary_end": bar.boundary.end if bar else None,
                "profile": sample.profile,
                "population": sample.population,
                "reference": sample.reference,
                "source": sample.source,
                "window_seconds": window_seconds or (bar.window_seconds if bar else None),
                "min_samples": min_samples or (bar.min_samples if bar else None),
                "outcome_inclusion": list(bar.outcome_inclusion) if bar else [],
                "requirement": bar.requirement if bar else None,
                "establishment": bar.establishment if bar else "unavailable",
                "samples": len(values),
                "eligible_known_timings": len(values),
                "observed": len(group),
                "errors": sum(item.outcome == "error" for item in group),
                "refusals": sum(item.outcome == "refused" for item in group),
                "abstentions": sum(item.outcome == "abstained" for item in group),
                "unknown_timings": unknown,
                "invalid_timings": invalid,
                "unknown_outcomes": unknown_outcomes,
                "excluded": sum(
                    item.excluded or bar is not None and item.outcome not in bar.outcome_inclusion
                    for item in group
                ),
                "unavailable_joins": sum(not item.correlated for item in group),
                "p50_ms": round(nearest_rank(values, 50), 3) if values else None,
                "p90_ms": round(nearest_rank(values, 90), 3) if values else None,
                "p95_ms": round(nearest_rank(values, 95), 3) if values else None,
                "percentile": bar.percentile if bar else None,
                "percentile_ms": round(percentile, 3) if percentile is not None else None,
                "comparison": bar.comparison if bar else None,
                "ceiling_ms": ceiling,
                "breach": breach,
                "verdict": verdict,
                "coverage": info,
                "dominant_spans": [
                    {"name": name, "ms": round(ms), "calls": int(calls)}
                    for name, (ms, calls) in dominant
                ],
            }
        )
    return out


def contract_revision() -> str | None:
    try:
        return load_contract().revision
    except Exception:  # noqa: BLE001 - observation must stay advisory
        return None


class Watch:
    """The bounded ring plus the once-per-interval breach event."""

    def __init__(self, *, started_at: float | None = None, clock=time.time) -> None:
        self._clock = clock
        self._started_at = float(clock() if started_at is None else started_at)
        self._ring: collections.deque[Sample] = collections.deque(maxlen=RING_SIZE)
        self._lock = threading.Lock()
        self._reported: dict[tuple[str, str, bool], float] = {}
        self._seen: dict[tuple[str, str, bool], int] = {}
        self._swept: dict[tuple, float] = {}
        self._evicted: dict[tuple, list[float]] = {}
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
        total_ms: Any,
        spans: Iterable[Any] | None = None,
        at: float | None = None,
        **metadata,
    ) -> None:
        """Record one terminal. Never raises; unknown timing remains unknown."""
        try:
            self._observe(
                tool=tool,
                client=client,
                deep=deep,
                total_ms=total_ms,
                spans=spans,
                at=at,
                **metadata,
            )
        except Exception:  # noqa: BLE001 - the watch must never break a call
            self.record_failure()

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1

    def _observe(self, *, tool, client, deep, total_ms, spans, at, **metadata) -> None:
        if not metadata and tool not in WATCHED_TOOLS:
            return
        now = float(self._clock() if at is None else at)
        if not math.isfinite(now):
            raise ValueError("Invalid observation timestamp")
        sample = make_sample(
            at=now, tool=tool, client=client, deep=deep, total_ms=total_ms, spans=spans, **metadata
        )
        key = _key(sample)
        bar = _bar(sample.bar_id) if sample.bar_id else None
        with self._lock:
            if len(self._ring) == RING_SIZE:
                lost = self._ring[0]
                entry = self._evicted.setdefault(_key(lost), [0, lost.at, lost.at])
                entry[0] += 1
                entry[1] = min(entry[1], lost.at)
                entry[2] = max(entry[2], lost.at)
            self._ring.append(sample)
            seen = self._seen[key] = self._seen.get(key, 0) + 1
            swept = self._swept.get(key)
            due = swept is not None and now - swept >= SWEEP_SECONDS
            if swept is None or due:
                self._swept[key] = now
        # Preserve the cheap conditional sweep: fast calls do not scan the ring.
        slow = bool(
            bar
            and bar.threshold_ms is not None
            and sample.total_ms is not None
            and (
                sample.total_ms >= bar.threshold_ms
                if bar.comparison == "<"
                else sample.total_ms > bar.threshold_ms
            )
        )
        if slow or bar and seen == bar.min_samples or due:
            self._maybe_report(sample, now)

    def _eligible(self, now: float) -> list[Sample]:
        grace_until = self._started_at + STARTUP_GRACE_SECONDS
        with self._lock:
            return [sample for sample in self._ring if sample.at >= grace_until]

    def verdicts(
        self, *, now: float | None = None, client: str | None = None
    ) -> list[dict[str, Any]]:
        """Figures per (tool, client, deep) for the window, startup grace applied."""
        current = float(self._clock() if now is None else now)
        eligible = self._eligible(current)
        with self._lock:
            evicted = {key: list(value) for key, value in self._evicted.items()}
        rows = []
        groups: dict[tuple, list[Sample]] = collections.defaultdict(list)
        for sample in eligible:
            if client is None or sample.client == str(client or "unknown"):
                groups[_key(sample)].append(sample)
        for key, samples in groups.items():
            bar = _bar(samples[0].bar_id) if samples[0].bar_id else None
            floor = (
                max(self._started_at + STARTUP_GRACE_SECONDS, current - bar.window_seconds)
                if bar
                else current
            )
            losses = evicted.get(key)
            count = (
                0
                if losses is None or losses[2] < floor
                else (int(losses[0]) if losses[1] >= floor else None)
            )
            rows.extend(
                summarize(
                    samples,
                    now=current,
                    coverage={
                        "population": "retained",
                        "evictions": count,
                        "evictions_since_start": int(losses[0]) if losses else 0,
                        "eviction_window_known": count is not None,
                        "observation_failures": self.failures if client is None else None,
                    },
                )
            )
        return sorted(
            rows, key=lambda row: (row["tool"], row["client"], row["deep"], str(row["bar_id"]))
        )

    def breaches(self, *, client: str | None, now: float | None = None) -> list[dict[str, Any]]:
        """This one client's breaches. An unnamed client is the `unknown` bucket,
        never every client: a bootstrap must not report another client's slowness."""
        wanted = str(client or "unknown")
        return [row for row in self.verdicts(now=now, client=wanted) if row["breach"]]

    def _maybe_report(self, sample: Sample, now: float) -> None:
        key = _key(sample)
        with self._lock:
            last = self._reported.get(key)
        if last is not None and now - last < REPORT_INTERVAL_SECONDS:
            return
        for row in self.verdicts(now=now, client=sample.client):
            if (
                row["tool"] != sample.tool
                or row["bar_id"] != sample.bar_id
                or row["boundary"] != sample.boundary
                or not row["breach"]
            ):
                continue
            with self._lock:
                # Check-and-set under the lock: two request threads crossing
                # the ceiling together must produce one event, not two.
                last = self._reported.get(key)
                if last is not None and now - last < REPORT_INTERVAL_SECONDS:
                    return
                self._reported[key] = now
            from .log_events import log_event

            log_event(
                log,
                logging.WARNING,
                EVENT,
                fields={key: value for key, value in row.items() if key != "client"},
            )
            return


def _deep_argument_digests() -> frozenset[str]:
    """The digests the ledger writes for every `deep` value that means yes, so a
    row's deep flag is recoverable from its shape without any value ever being
    stored, and classified exactly as the live path classified the call."""
    import hashlib

    from .call_ledger import canonical_json

    return frozenset(
        hashlib.sha256(canonical_json({"v": value})).hexdigest() for value in DEEP_TRUE_VALUES
    )


class SourceRead(list[Sample]):
    """Samples and read coverage from one independent persisted boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.coverage = {
            "population": "read-source",
            "evictions": 0,
            "read_segments": 0,
            "failed_reads": 0,
            "partial_reads": 0,
            "malformed_rows": 0,
            "unknown_window_rows": 0,
            "rows_read": 0,
        }


def _row_sample(
    row: Any, *, deep_digests: frozenset[str], activation: bool = False
) -> Sample | None:
    if not isinstance(row, dict):
        return None
    tool = "activate_context" if activation else row.get("tool")
    if not isinstance(tool, str):
        return None
    try:
        from datetime import datetime

        at = datetime.fromisoformat(str(row.get("ts_utc"))).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    args = row.get("args")
    deep_shape = args.get("deep") if isinstance(args, dict) else None
    deep = isinstance(deep_shape, dict) and deep_shape.get("sha256") in deep_digests
    return make_sample(
        at=at,
        tool=tool,
        client=row.get("client") if activation else row.get("client_name"),
        deep=deep,
        total_ms=row.get("duration_ms") if activation else row.get("total_ms"),
        spans=row.get("spans"),
        surface="activation" if activation else "mcp",
        outcome=row.get("outcome", "unknown"),
        source="activation-log" if activation else "call-ledger",
        correlated=bool(row.get("request_id") and row.get("request_id") != "unknown"),
        boundary=row.get("latency_boundary", "activation-leaf-legacy") if activation else None,
    )


def _read_sources(
    paths: Iterable[Path], *, now: float, window_seconds: float | None, activation: bool = False
) -> SourceRead:
    import json

    out = SourceRead()
    deep_digests = _deep_argument_digests()
    window = window_seconds or WINDOW_SECONDS
    if window is None:
        out.coverage["failed_reads"] += 1
        return out
    for file in paths:
        try:
            # Stream each segment independently; one unreadable segment cannot hide older rows.
            with file.open("r", encoding="utf-8") as handle:
                out.coverage["read_segments"] += 1
                for line in handle:
                    if not line.strip():
                        continue
                    out.coverage["rows_read"] += 1
                    try:
                        row = json.loads(line)
                    except ValueError:
                        out.coverage["malformed_rows"] += 1
                        continue
                    try:
                        sample = _row_sample(row, deep_digests=deep_digests, activation=activation)
                    except (TypeError, ValueError, OverflowError):
                        out.coverage["malformed_rows"] += 1
                        continue
                    if sample is None:
                        out.coverage["unknown_window_rows"] += 1
                    elif now - window <= sample.at <= now:
                        out.append(sample)
        except (OSError, UnicodeError):
            out.coverage["failed_reads"] += 1
            out.coverage["partial_reads"] += 1
    return out


def samples_from_ledger(
    path: Path, *, archive_dir: Path | None, now: float, window_seconds: float | None = None
) -> SourceRead:
    paths = [path]
    failed_scan = False
    try:
        paths.extend(_archive_generations_newest_first(archive_dir))
    except OSError:
        failed_scan = True
    out = _read_sources(paths, now=now, window_seconds=window_seconds)
    if failed_scan:
        out.coverage["failed_reads"] += 1
    out.coverage["archive_scan_failed"] = failed_scan
    return out


def samples_from_activations(path: Path, *, now: float) -> SourceRead:
    paths = [path]
    rotated = path.with_name(path.name + ".1")
    if rotated.exists():
        paths.append(rotated)
    out = _read_sources(paths, now=now, window_seconds=None, activation=True)
    out.coverage["retention_loss"] = (
        None  # Only one prior generation exists; older loss is unknown.
    )
    return out


def _archive_generations_newest_first(archive_dir: Path | None) -> list[Path]:
    """Archived ledger segments, newest first, placed by the `sequence` their
    first row carries.

    The archive is content-addressed, so neither the filename nor the file
    mtime says anything about age (a restore, a copy without `-p`, or a
    container rebuild rewrites every mtime). The rows carry the order, and
    the first row of a segment is enough to place it: the same rule
    `obs_cli._ledger_archive_generations` applies for `exomem logs`.
    """
    import json

    if archive_dir is None:
        return []
    if not archive_dir.exists():
        return []
    placed: list[tuple[int, Path]] = []
    # These filenames are the ledger rotation protocol, not inferred vocabulary.
    for candidate in archive_dir.iterdir():
        if not candidate.name.startswith("ledger-") or not candidate.name.endswith(".jsonl"):
            continue
        try:
            with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline().strip()
            sequence = int(json.loads(first)["sequence"]) if first else 0
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            sequence = 0
        placed.append((sequence, candidate))
    return [path for _sequence, path in sorted(placed, key=lambda item: -item[0])]


_WATCH = Watch()


def observe(**kwargs: Any) -> None:
    """Module entry point used beside the ledger write. Never raises."""
    try:
        _WATCH.observe(**kwargs)
    except Exception:  # noqa: BLE001 - belt and braces: even a bad kwarg must not escape
        _WATCH.record_failure()


def bootstrap_block(
    client: str | None, *, now: float | None = None, full: bool = False
) -> list[dict[str, Any]] | None:
    """One client's retained-ring advisory, or full detail; None when nothing breaches."""
    try:
        rows = _WATCH.breaches(client=client, now=now)
    except Exception:  # noqa: BLE001 - a watch failure never breaks a bootstrap
        return None
    if not rows:
        return None
    if full:
        return [{key: value for key, value in row.items() if key != "client"} for row in rows]
    # Advisory fields are closed protocol fields in the agent-bootstrap contract.
    return [
        {key: row[key] for key in (
            "tool", "deep", "samples", "p50_ms", "p90_ms", "ceiling_ms",
            "bar_id", "contract_revision", "source",
        )}
        for row in rows
    ]


def reset(*, started_at: float | None = None, clock=time.time) -> Watch:
    """Replace the process watch (tests, and a service that re-seeds its clock)."""
    global _WATCH
    _WATCH = Watch(started_at=started_at, clock=clock)
    return _WATCH


def current() -> Watch:
    return _WATCH
