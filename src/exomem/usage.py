"""Usage-activation primitives (OpenSpec: usage-aware-find-ranking).

Shared by two consumers with deliberately different weight profiles:

- `audit`'s stale-review queue (the original home of this code — extracted
  verbatim; a parity test guards the move) sorts most-dormant-first with its
  own env-tunable weights.
- The opt-in `find(prefer_used=true)` boost: ACT-R base-level activation
  B = ln(Σ wⱼ·Δtⱼ^(−d)) over the JSONL access logs the server already
  writes (queries/reads/writes under the repo `logs/` dir — local, never
  Obsidian-synced), mapped through a bounded logistic multiplier. Its
  default weights are surfaced=0 / read=1 / cited=2 (RankingConfig knobs):
  being surfaced by `find` is not a choice anyone made — counting it builds
  a rich-get-richer loop — while a read is a selection act and a citation is
  grounded in a vault artifact (the wikilink in the written note).

Strict no-op conditions for the find boost (every multiplier exactly 1.0):
`EXOMEM_DISABLE_USAGE_BOOST`, the suite's `EXOMEM_DISABLE_RELEVANCE_CHECK`
gate, absent/empty logs, and cold start. Deterministic given
(logs, config, today) — the logs are inspectable JSONL on disk, so an
opted-in ranking is still reproducible state, not hidden state. Rotating or
truncating the logs only affects this opt-in boost, nothing else.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import threading
import time
from pathlib import Path

from . import query_log

log = logging.getLogger(__name__)


def canon(path: str) -> str:
    """Canonical page key: forward slashes, no .md, no leading
    `Knowledge Base/`, lowercased."""
    p = (path or "").strip().replace("\\", "/")
    if p.lower().endswith(".md"):
        p = p[:-3]
    if p.startswith("Knowledge Base/"):
        p = p[len("Knowledge Base/"):]
    return p.lower()


def read_jsonl(path: Path) -> list[dict]:
    """Best-effort JSONL reader; malformed lines are skipped."""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def access_events(
    logs_dir: Path | None = None,
    today: dt.date | None = None,
    *,
    w_surfaced: float,
    w_read: float,
    w_cited: float,
    horizon_days: float | None = None,
) -> dict[str, list[tuple[float, float]]] | None:
    """Per-canon-path weighted access events `(delta_days, weight)`.

    find-surfacings (queries.jsonl top_k, w_surfaced), get-reads
    (reads.jsonl, w_read), citations (writes.jsonl cited_sources, w_cited).
    delta_days = max((today - ts).days, 1) (floored to dodge the t^−d
    singularity); events beyond `horizon_days` are ignored (bounds log-parse
    cost and makes log rotation explicitly safe). A zero-weight source is
    skipped entirely — a w=0 event adds nothing to the activation sum but
    would make a page look "accessed".

    Returns None when the signal is UNAVAILABLE — gated for tests
    (`EXOMEM_DISABLE_RELEVANCE_CHECK`) or all three logs empty — so callers
    fall back rather than fabricate activation.
    """
    if os.environ.get("EXOMEM_DISABLE_RELEVANCE_CHECK"):
        return None
    logs_dir = logs_dir or query_log._LOG_DIR
    today = today or dt.date.today()

    queries = read_jsonl(logs_dir / "queries.jsonl")
    reads = read_jsonl(logs_dir / "reads.jsonl")
    writes = read_jsonl(logs_dir / "writes.jsonl")
    if not queries and not reads and not writes:
        return None

    events: dict[str, list[tuple[float, float]]] = {}

    def _delta(ts_raw: object) -> float | None:
        try:
            ts = dt.datetime.fromisoformat(str(ts_raw))
        except (ValueError, TypeError):
            return None
        delta = float(max((today - ts.date()).days, 1))
        if horizon_days is not None and delta > horizon_days:
            return None
        return delta

    if w_surfaced:
        for q in queries:
            delta = _delta(q.get("ts"))
            if delta is None:
                continue
            for t in q.get("top_k") or []:
                p = t.get("path")
                if p:
                    events.setdefault(canon(p), []).append((delta, w_surfaced))

    if w_read:
        for r in reads:
            delta = _delta(r.get("ts"))
            if delta is None:
                continue
            p = r.get("read_path")
            if p:
                events.setdefault(canon(p), []).append((delta, w_read))

    if w_cited:
        for w in writes:
            delta = _delta(w.get("ts"))
            if delta is None:
                continue
            for c in w.get("cited_sources") or []:
                if c:
                    events.setdefault(canon(c), []).append((delta, w_cited))

    return events


def activation(events: list[tuple[float, float]] | None, d: float) -> float | None:
    """ACT-R base-level activation B = ln(Σ wⱼ·Δtⱼ^(−d)) over weighted access
    events. None when there are no events (never accessed)."""
    if not events:
        return None
    return math.log(sum(w * (dt_days ** (-d)) for dt_days, w in events))


def usage_multiplier(b: float | None, config) -> float:
    """Bounded, positive-only boost: 1.0 for never-used pages, else
    1 + (usage_boost − 1)·σ(B) with σ the logistic — range strictly
    (1.0, usage_boost]. Never a penalty: non-use never demotes ("the vault
    doesn't rot because you didn't query it"). No per-query normalization,
    so the same page always gets the same multiplier — explainable."""
    if b is None:
        return 1.0
    boost = float(config.usage_boost)
    if boost <= 1.0:
        return 1.0
    sigma = 1.0 / (1.0 + math.exp(-b))
    return 1.0 + (boost - 1.0) * sigma


def boost_disabled() -> bool:
    """The find-boost no-op gates: explicit kill-switch, or the suite's
    relevance-log gate (which also keeps the default logs out of tests)."""
    return bool(
        os.environ.get("EXOMEM_DISABLE_USAGE_BOOST")
        or os.environ.get("EXOMEM_DISABLE_RELEVANCE_CHECK")
    )


# ---- Memoized activation snapshot for the find boost ----
# Rebuilding costs a full parse of three JSONL logs; a find burst shouldn't
# pay that per call. Memoized on (weight/decay/horizon params, logs dir,
# today); the log files' (size, mtime_ns) signature is re-checked at most
# every EXOMEM_USAGE_REFRESH_S seconds (default 300) and any change rebuilds.

_DEFAULT_REFRESH_S = 300.0
_SNAPSHOT_LOCK = threading.Lock()
_SNAPSHOT: dict | None = None


def _refresh_seconds() -> float:
    raw = os.environ.get("EXOMEM_USAGE_REFRESH_S")
    if raw is None or not raw.strip():
        return _DEFAULT_REFRESH_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("EXOMEM_USAGE_REFRESH_S=%r is not a number; using default", raw)
        return _DEFAULT_REFRESH_S


def _logs_signature(logs_dir: Path) -> tuple:
    sig = []
    for name in ("queries.jsonl", "reads.jsonl", "writes.jsonl"):
        try:
            st = (logs_dir / name).stat()
            sig.append((name, st.st_size, st.st_mtime_ns))
        except OSError:
            sig.append((name, 0, 0))
    return tuple(sig)


def activation_map(
    config, logs_dir: Path | None = None, today: dt.date | None = None
) -> dict[str, float]:
    """Memoized {canon_path: B} for `find(prefer_used=true)`.

    An empty dict is the strict no-op (cold start, gated, or no logs) —
    every multiplier is exactly 1.0 and default ranking is untouched.
    """
    global _SNAPSHOT
    if boost_disabled():
        return {}
    logs_dir = logs_dir or query_log._LOG_DIR
    today = today or dt.date.today()
    params = (
        str(logs_dir), today.toordinal(), float(config.usage_decay),
        float(config.usage_horizon_days), float(config.usage_w_surfaced),
        float(config.usage_w_read), float(config.usage_w_cited),
    )
    now = time.monotonic()
    with _SNAPSHOT_LOCK:
        snap = _SNAPSHOT
        if snap is not None and snap["params"] == params:
            if now - snap["checked"] < _refresh_seconds():
                return snap["activations"]
            if _logs_signature(logs_dir) == snap["sig"]:
                snap["checked"] = now
                return snap["activations"]

    events = access_events(
        logs_dir, today,
        w_surfaced=config.usage_w_surfaced,
        w_read=config.usage_w_read,
        w_cited=config.usage_w_cited,
        horizon_days=config.usage_horizon_days,
    )
    activations: dict[str, float] = {}
    if events:
        for key, evs in events.items():
            b = activation(evs, config.usage_decay)
            if b is not None:
                activations[key] = b
    with _SNAPSHOT_LOCK:
        _SNAPSHOT = {
            "params": params,
            "sig": _logs_signature(logs_dir),
            "checked": time.monotonic(),
            "activations": activations,
        }
    return activations


def reset_usage_cache() -> None:
    """Test hook: drop the memoized activation snapshot."""
    global _SNAPSHOT
    with _SNAPSHOT_LOCK:
        _SNAPSHOT = None
