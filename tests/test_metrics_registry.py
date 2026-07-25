"""One process-wide metrics registry: counters + fixed-bucket histograms under
one lock, atomic snapshot/restore, and soft-fail everywhere — a metrics bug
must never break the calling tool call, HTTP request, or mutation.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from exomem import metrics


@pytest.fixture(autouse=True)
def _reset_registry():
    # `start_snapshotter` is a process-wide singleton: another test module in
    # the same pytest run (anything that calls `server.build_server()`, which
    # starts the real snapshotter via `server_runtime._start_metrics_persistence`)
    # may already occupy it with its own interval/state_dir, silently
    # starving this file's own snapshotter tests of a fresh thread. Force a
    # clean slate on both sides of every test.
    metrics.stop_snapshotter()
    metrics.reset()
    yield
    metrics.stop_snapshotter()
    metrics.reset()


def test_inc_counter_accumulates_by_label_set() -> None:
    metrics.inc_counter("exomem_tool_calls_total", {"tool": "ask_memory", "outcome": "success"})
    metrics.inc_counter("exomem_tool_calls_total", {"tool": "ask_memory", "outcome": "success"})
    metrics.inc_counter("exomem_tool_calls_total", {"tool": "ask_memory", "outcome": "failure"})

    snap = metrics.snapshot()
    counters = {
        (c["name"], tuple(sorted(c["labels"].items()))): c["value"] for c in snap["counters"]
    }
    assert counters[("exomem_tool_calls_total", (("outcome", "success"), ("tool", "ask_memory")))] == 2
    assert counters[("exomem_tool_calls_total", (("outcome", "failure"), ("tool", "ask_memory")))] == 1


def test_observe_duration_buckets_by_upper_bound() -> None:
    metrics.observe_duration_ms("exomem_tool_duration_ms", 3, {"tool": "ask_memory"})
    metrics.observe_duration_ms("exomem_tool_duration_ms", 999999, {"tool": "ask_memory"})

    snap = metrics.snapshot()
    hist = next(h for h in snap["histograms"] if h["labels"] == {"tool": "ask_memory"})
    assert hist["count"] == 2
    assert hist["sum"] == pytest.approx(1000002)
    assert sum(hist["buckets"]) == 2
    # The tiny observation lands in an early bucket, the huge one in the
    # overflow ("+Inf") bucket — they must not land in the same one.
    assert hist["buckets"][0] >= 1
    assert hist["buckets"][-1] >= 1


def test_registry_is_thread_safe_under_concurrent_increments() -> None:
    def bump():
        for _ in range(200):
            metrics.inc_counter("exomem_http_requests_total", {"status": "200"})

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = metrics.snapshot()
    total = sum(c["value"] for c in snap["counters"] if c["name"] == "exomem_http_requests_total")
    assert total == 1600


def test_inc_counter_never_raises_on_bad_labels() -> None:
    metrics.inc_counter("exomem_tool_calls_total", labels="not-a-mapping")  # type: ignore[arg-type]
    metrics.observe_duration_ms("exomem_tool_duration_ms", "not-a-number", {"tool": "x"})  # type: ignore[arg-type]


def test_snapshot_and_restore_round_trip(tmp_path: Path) -> None:
    metrics.inc_counter("exomem_mutation_busy_total", {"code": "MUTATION_BUSY"}, value=3)
    metrics.observe_duration_ms("exomem_boundary_wait_ms", 42, {"scope": "x"})

    metrics.save_snapshot(tmp_path)
    snapshot_file = metrics.snapshot_path(tmp_path)
    assert snapshot_file.exists()
    payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert payload["counters"]

    metrics.reset()
    assert metrics.snapshot()["counters"] == []

    metrics.load_snapshot(tmp_path)
    snap = metrics.snapshot()
    counters = {(c["name"], tuple(sorted(c["labels"].items()))): c["value"] for c in snap["counters"]}
    assert counters[("exomem_mutation_busy_total", (("code", "MUTATION_BUSY"),))] == 3


def test_save_snapshot_is_atomic_no_partial_file_left_behind(tmp_path: Path) -> None:
    metrics.inc_counter("exomem_http_requests_total", {"status": "200"})
    metrics.save_snapshot(tmp_path)
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_load_snapshot_missing_file_is_a_noop(tmp_path: Path) -> None:
    metrics.load_snapshot(tmp_path / "does-not-exist")


def test_load_snapshot_corrupt_file_does_not_raise(tmp_path: Path) -> None:
    path = metrics.snapshot_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{", encoding="utf-8")
    metrics.load_snapshot(tmp_path)
    assert metrics.snapshot()["counters"] == []


def test_snapshotter_thread_persists_on_interval(tmp_path: Path) -> None:
    metrics.inc_counter("exomem_http_requests_total", {"status": "200"})
    thread = metrics.start_snapshotter(tmp_path, interval_seconds=0.05)
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not metrics.snapshot_path(tmp_path).exists():
            time.sleep(0.02)
        assert metrics.snapshot_path(tmp_path).exists()
    finally:
        metrics.stop_snapshotter()
    assert thread is not None


def test_snapshotter_disabled_when_interval_is_zero(tmp_path: Path) -> None:
    thread = metrics.start_snapshotter(tmp_path, interval_seconds=0)
    assert thread is None
    metrics.stop_snapshotter()


def test_snapshot_interval_seconds_from_env_defaults_to_60(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_METRICS_SNAPSHOT_SECONDS", raising=False)
    assert metrics.snapshot_interval_seconds_from_env() == 60.0


def test_snapshot_interval_seconds_from_env_honors_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_METRICS_SNAPSHOT_SECONDS", "5")
    assert metrics.snapshot_interval_seconds_from_env() == 5.0


def test_snapshot_interval_seconds_from_env_zero_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_METRICS_SNAPSHOT_SECONDS", "0")
    assert metrics.snapshot_interval_seconds_from_env() == 0.0

def test_load_snapshot_once_consumes_the_restore_for_the_whole_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime init can run more than once per process; only the FIRST init may
    restore from disk, or later inits would clobber live in-process counters
    with older persisted values (and, in a shared test process, re-import
    another test's persisted counts over a per-test reset)."""
    monkeypatch.setattr(metrics, "_SNAPSHOT_LOADED", False)
    metrics.inc_counter("exomem_tool_calls_total", {"tool": "x", "outcome": "success"})
    metrics.save_snapshot(tmp_path)
    metrics.reset()

    metrics.load_snapshot_once(tmp_path)
    first = {
        (c["name"], tuple(sorted(c["labels"].items()))): c["value"]
        for c in metrics.snapshot()["counters"]
    }
    assert first[
        ("exomem_tool_calls_total", (("outcome", "success"), ("tool", "x")))
    ] == 1

    metrics.reset()
    metrics.load_snapshot_once(tmp_path)
    assert metrics.snapshot()["counters"] == []
