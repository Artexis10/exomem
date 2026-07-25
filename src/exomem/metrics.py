"""One process-wide metrics registry: counters + fixed-bucket histograms.

Every public function soft-fails: a defect here must never break the tool
call, HTTP request, or mutation it is describing. The registry is
snapshotted atomically (temp file + `os.replace`) to the writer-lease state
directory on an interval, and restored from that snapshot at process start,
so counts survive a restart instead of resetting to zero. No network
endpoint is exposed by this module; `/metrics.json` is a later, separate
change built on top of this same registry.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Upper bounds (milliseconds) for the fixed-bucket duration histograms. A
# value greater than the last bound lands in the implicit "+Inf" overflow
# bucket (one more slot than there are bounds).
DEFAULT_DURATION_BUCKETS_MS: tuple[float, ...] = (
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1000,
    2500,
    5000,
    10000,
    30000,
    60000,
)

SNAPSHOT_FILENAME = "metrics-snapshot.json"

_Key = tuple[str, tuple[tuple[str, str], ...]]


def _label_key(labels: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    if not isinstance(labels, Mapping):
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


class MetricsRegistry:
    """Counters and fixed-bucket histograms behind one lock."""

    def __init__(self, *, duration_buckets_ms: tuple[float, ...] = DEFAULT_DURATION_BUCKETS_MS):
        self._lock = threading.Lock()
        self._buckets: tuple[float, ...] = tuple(duration_buckets_ms)
        self._counters: dict[_Key, int] = {}
        self._hist_buckets: dict[_Key, list[int]] = {}
        self._hist_sum: dict[_Key, float] = {}
        self._hist_count: dict[_Key, int] = {}

    def inc(self, name: str, labels: Mapping[str, Any] | None = None, value: int = 1) -> None:
        try:
            key = (name, _label_key(labels))
            with self._lock:
                self._counters[key] = self._counters.get(key, 0) + int(value)
        except Exception:  # noqa: BLE001 - metrics must never break the caller
            log.debug("metrics inc_counter failed", exc_info=True)

    def observe(self, name: str, value: Any, labels: Mapping[str, Any] | None = None) -> None:
        try:
            numeric = float(value)
            key = (name, _label_key(labels))
            with self._lock:
                buckets = self._hist_buckets.setdefault(key, [0] * (len(self._buckets) + 1))
                buckets[self._bucket_index(numeric)] += 1
                self._hist_sum[key] = self._hist_sum.get(key, 0.0) + numeric
                self._hist_count[key] = self._hist_count.get(key, 0) + 1
        except Exception:  # noqa: BLE001 - metrics must never break the caller
            log.debug("metrics observe failed", exc_info=True)

    def _bucket_index(self, value: float) -> int:
        for index, bound in enumerate(self._buckets):
            if value <= bound:
                return index
        return len(self._buckets)  # overflow ("+Inf") bucket

    def snapshot(self) -> dict[str, Any]:
        try:
            with self._lock:
                return {
                    "counters": [
                        {"name": key[0], "labels": dict(key[1]), "value": value}
                        for key, value in self._counters.items()
                    ],
                    "histograms": [
                        {
                            "name": key[0],
                            "labels": dict(key[1]),
                            "buckets": list(buckets),
                            "sum": self._hist_sum.get(key, 0.0),
                            "count": self._hist_count.get(key, 0),
                        }
                        for key, buckets in self._hist_buckets.items()
                    ],
                    "bucket_bounds_ms": list(self._buckets),
                }
        except Exception:  # noqa: BLE001 - metrics must never break the caller
            log.debug("metrics snapshot failed", exc_info=True)
            return {"counters": [], "histograms": [], "bucket_bounds_ms": list(self._buckets)}

    def restore(self, data: Any) -> None:
        try:
            if not isinstance(data, Mapping):
                return
            with self._lock:
                for entry in data.get("counters") or []:
                    if not isinstance(entry, Mapping):
                        continue
                    key = (str(entry.get("name")), _label_key(entry.get("labels")))
                    self._counters[key] = int(entry.get("value", 0))
                bounds = data.get("bucket_bounds_ms")
                if isinstance(bounds, list) and tuple(bounds) == self._buckets:
                    for entry in data.get("histograms") or []:
                        if not isinstance(entry, Mapping):
                            continue
                        key = (str(entry.get("name")), _label_key(entry.get("labels")))
                        buckets = entry.get("buckets")
                        if isinstance(buckets, list) and len(buckets) == len(self._buckets) + 1:
                            self._hist_buckets[key] = [int(x) for x in buckets]
                            self._hist_sum[key] = float(entry.get("sum", 0.0))
                            self._hist_count[key] = int(entry.get("count", 0))
        except Exception:  # noqa: BLE001 - metrics must never break the caller
            log.debug("metrics restore failed", exc_info=True)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._hist_buckets.clear()
            self._hist_sum.clear()
            self._hist_count.clear()


_REGISTRY = MetricsRegistry()


def metrics_disabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get("EXOMEM_DISABLE_METRICS", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def inc_counter(name: str, labels: Mapping[str, Any] | None = None, value: int = 1) -> None:
    if metrics_disabled():
        return
    _REGISTRY.inc(name, labels, value)


def observe_duration_ms(name: str, value_ms: Any, labels: Mapping[str, Any] | None = None) -> None:
    if metrics_disabled():
        return
    _REGISTRY.observe(name, value_ms, labels)


def snapshot() -> dict[str, Any]:
    return _REGISTRY.snapshot()


def render_json() -> dict[str, Any]:
    """The registry's current snapshot, ready for JSON serialization (the
    `/metrics.json` route body)."""
    return snapshot()


def reset() -> None:
    _REGISTRY.reset()


def snapshot_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / SNAPSHOT_FILENAME


def save_snapshot(state_dir: Path | str) -> None:
    """Atomically persist the current snapshot (temp file + `os.replace`)."""
    try:
        path = snapshot_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(
            json.dumps(_REGISTRY.snapshot(), sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp_path, path)
    except Exception:  # noqa: BLE001 - metrics must never break the caller
        log.debug("metrics save_snapshot failed", exc_info=True)


def load_snapshot(state_dir: Path | str) -> None:
    """Restore from a prior snapshot; a missing or corrupt file is a no-op."""
    try:
        path = snapshot_path(state_dir)
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        _REGISTRY.restore(data)
    except Exception:  # noqa: BLE001 - metrics must never break the caller
        log.debug("metrics load_snapshot failed", exc_info=True)


def snapshot_interval_seconds_from_env(env: Mapping[str, str] | None = None) -> float:
    """`EXOMEM_METRICS_SNAPSHOT_SECONDS`, default 60.0; `0` disables the thread."""
    values = os.environ if env is None else env
    raw = str(values.get("EXOMEM_METRICS_SNAPSHOT_SECONDS", "")).strip()
    if not raw:
        return 60.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


_SNAPSHOTTER_LOCK = threading.Lock()
_snapshotter_thread: threading.Thread | None = None
_snapshotter_stop: threading.Event | None = None


def start_snapshotter(state_dir: Path | str, interval_seconds: float) -> threading.Thread | None:
    """Start the background snapshotter; `interval_seconds <= 0` disables it."""
    global _snapshotter_thread, _snapshotter_stop
    if interval_seconds is None or interval_seconds <= 0:
        return None
    with _SNAPSHOTTER_LOCK:
        if _snapshotter_thread is not None and _snapshotter_thread.is_alive():
            return _snapshotter_thread
        stop_event = threading.Event()

        def _loop() -> None:
            while not stop_event.wait(interval_seconds):
                save_snapshot(state_dir)

        thread = threading.Thread(
            target=_loop, name="exomem-metrics-snapshotter", daemon=True
        )
        _snapshotter_thread = thread
        _snapshotter_stop = stop_event
        thread.start()
        return thread


def stop_snapshotter() -> None:
    global _snapshotter_thread, _snapshotter_stop
    with _SNAPSHOTTER_LOCK:
        if _snapshotter_stop is not None:
            _snapshotter_stop.set()
        _snapshotter_thread = None
        _snapshotter_stop = None
