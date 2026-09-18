"""`latency_watch`: the rolling recall-latency verdict fed beside the ledger row.

Torch-free and file-free: the ring is driven with a fake clock."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from exomem import call_ledger, latency_watch, server

T0 = 1_800_000_000.0
AFTER_GRACE = T0 + latency_watch.STARTUP_GRACE_SECONDS + 1.0


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _watch(now: float = AFTER_GRACE) -> tuple[latency_watch.Watch, _Clock]:
    clock = _Clock(now)
    return latency_watch.Watch(started_at=T0, clock=clock), clock


def _fill(watch, *, n, total_ms, tool="ask_memory", client="openai-mcp/1.0.0", deep=False, spans=None):
    for _ in range(n):
        watch.observe(tool=tool, client=client, deep=deep, total_ms=total_ms, spans=spans)


def test_samples_are_content_free_and_the_ring_is_bounded() -> None:
    watch, _clock = _watch()
    _fill(watch, n=latency_watch.RING_SIZE + 100, total_ms=120, spans=[
        {"name": "recall.keyword", "ms": 40, "count": 1, "fields": {"texts": 3}},
    ])
    ring = list(watch._ring)
    assert len(ring) == latency_watch.RING_SIZE
    sample = ring[-1]
    assert set(sample.__slots__) == {"at", "tool", "client", "deep", "total_ms", "spans"}
    assert sample.spans == (("recall.keyword", 40.0),)


def test_a_breach_is_named_by_its_dominant_stage() -> None:
    watch, _clock = _watch()
    _fill(watch, n=20, total_ms=200, spans=[{"name": "recall.keyword", "ms": 150}])
    _fill(watch, n=5, total_ms=3000, spans=[
        {"name": "embeddings.matrix_load", "ms": 2500, "fields": {"reason": "cold"}},
        {"name": "recall.pack", "ms": 300},
    ])
    [row] = watch.verdicts(client="openai-mcp/1.0.0")
    assert row["tool"] == "ask_memory" and row["deep"] is False
    assert row["samples"] == 25 and row["p50_ms"] == 200 and row["p90_ms"] == 3000
    assert row["ceiling_ms"] == 1000 and row["breach"] is True
    assert row["dominant_spans"][0] == {"name": "embeddings.matrix_load", "ms": 12500, "calls": 5}
    assert row["dominant_spans"][1] == {"name": "recall.pack", "ms": 1500, "calls": 5}


def test_deep_recalls_have_their_own_ceiling() -> None:
    watch, _clock = _watch()
    _fill(watch, n=25, total_ms=4000, deep=True)
    _fill(watch, n=25, total_ms=4000, deep=False)
    rows = {(r["deep"]): r for r in watch.verdicts(client="openai-mcp/1.0.0")}
    assert rows[True]["ceiling_ms"] == 5000 and rows[True]["breach"] is False
    assert rows[False]["ceiling_ms"] == 1000 and rows[False]["breach"] is True


def test_too_few_samples_is_not_a_verdict() -> None:
    watch, _clock = _watch()
    _fill(watch, n=latency_watch.MIN_SAMPLES - 1, total_ms=9000)
    [row] = watch.verdicts()
    assert row["samples"] == latency_watch.MIN_SAMPLES - 1 and row["breach"] is False
    _fill(watch, n=1, total_ms=9000)
    assert watch.verdicts()[0]["breach"] is True


def test_the_cold_window_after_a_start_does_not_count() -> None:
    watch, clock = _watch(now=T0 + 30.0)
    _fill(watch, n=30, total_ms=12000)  # the drain after a promotion
    clock.now = AFTER_GRACE
    assert watch.verdicts() == []
    _fill(watch, n=30, total_ms=150)
    [row] = watch.verdicts()
    assert row["samples"] == 30 and row["breach"] is False


def test_samples_outside_the_window_fall_away() -> None:
    watch, clock = _watch()
    _fill(watch, n=30, total_ms=5000)
    clock.now = AFTER_GRACE + latency_watch.WINDOW_SECONDS + 1
    assert watch.verdicts() == []


def test_unwatched_tools_are_ignored() -> None:
    watch, _clock = _watch()
    _fill(watch, n=30, total_ms=30000, tool="remember")
    assert watch.verdicts() == []


def test_a_fast_call_never_pays_for_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call under its ceiling cannot raise the p90, so it must not scan the
    ring; only a slow call or the one completing the minimum sample count does."""
    watch, _clock = _watch()
    scans: list[int] = []
    real = watch.verdicts

    def counted(**kwargs):
        scans.append(1)
        return real(**kwargs)

    monkeypatch.setattr(watch, "verdicts", counted)
    _fill(watch, n=latency_watch.MIN_SAMPLES - 1, total_ms=120)
    assert scans == []
    _fill(watch, n=1, total_ms=120)  # completes MIN_SAMPLES: may make a breach reportable
    assert len(scans) == 1
    _fill(watch, n=50, total_ms=120)
    assert len(scans) == 1
    _fill(watch, n=1, total_ms=5000)
    assert len(scans) == 2


def test_a_breach_reports_once_under_concurrent_crossings(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from exomem import log_events

    events: list[str] = []
    monkeypatch.setattr(
        log_events, "log_event",
        lambda logger, level, event, **_k: events.append(event),
    )
    watch, _clock = _watch()
    _fill(watch, n=19, total_ms=150)
    barrier = threading.Barrier(8)

    def cross():
        barrier.wait()
        watch.observe(tool="ask_memory", client="openai-mcp/1.0.0", deep=False, total_ms=9000)

    threads = [threading.Thread(target=cross) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert events == ["latency_ceiling_exceeded"]


def test_a_failing_watch_is_counted_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    watch, _clock = _watch()

    def boom(**_kwargs):
        raise RuntimeError("watch broke")

    monkeypatch.setattr(watch, "_observe", boom)
    watch.observe(tool="ask_memory", client="c", deep=False, total_ms=1)
    assert watch.failures == 1


def test_a_breach_logs_one_event_per_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import log_events

    events: list[dict] = []
    monkeypatch.setattr(
        log_events, "log_event",
        lambda logger, level, event, *, fields=None, content=None, exc_info=None: events.append(
            {"event": event, "level": level, "fields": dict(fields or {})}
        ),
    )
    watch, clock = _watch()
    _fill(watch, n=25, total_ms=3000, spans=[{"name": "embeddings.matrix_load", "ms": 2500}])
    assert [e["event"] for e in events] == ["latency_ceiling_exceeded"]
    assert events[0]["level"] == logging.WARNING
    assert events[0]["fields"]["p90_ms"] == 3000
    assert events[0]["fields"]["dominant_spans"][0]["name"] == "embeddings.matrix_load"
    _fill(watch, n=10, total_ms=3000)
    assert len(events) == 1  # still inside the interval
    clock.now += latency_watch.REPORT_INTERVAL_SECONDS
    _fill(watch, n=1, total_ms=3000)
    assert len(events) == 2


def test_bootstrap_block_reports_only_the_calling_client() -> None:
    watch = latency_watch.reset(started_at=T0, clock=_Clock(AFTER_GRACE))
    try:
        _fill(watch, n=25, total_ms=3000, client="openai-mcp/1.0.0")
        _fill(watch, n=25, total_ms=150, client="claude-code")
        block = latency_watch.bootstrap_block("openai-mcp/1.0.0")
        assert block is not None and len(block) == 1
        assert set(block[0]) == {"tool", "deep", "samples", "p50_ms", "p90_ms", "ceiling_ms", "dominant_spans"}
        assert latency_watch.bootstrap_block("claude-code") is None
        assert latency_watch.bootstrap_block(None) is None
    finally:
        latency_watch.reset()


def test_deep_flag_reads_the_real_arguments() -> None:
    assert latency_watch.deep_flag({"deep": True}) is True
    assert latency_watch.deep_flag({"deep": "true"}) is True
    assert latency_watch.deep_flag({"deep": "yes"}) is True
    assert latency_watch.deep_flag({"deep": "1"}) is True
    assert latency_watch.deep_flag({"deep": False}) is False
    assert latency_watch.deep_flag({"deep": "false"}) is False
    assert latency_watch.deep_flag({}) is False
    assert latency_watch.deep_flag(None) is False


# --- the ledger write site feeds the watch, and a failing watch costs the row nothing


@pytest.fixture
def ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "ledger"
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(directory))
    monkeypatch.delenv("EXOMEM_DISABLE_CALL_LEDGER", raising=False)
    call_ledger.reset_chain_cache()
    yield directory
    call_ledger.reset_chain_cache()


def _rows(directory: Path) -> list[dict]:
    path = directory / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _record(**overrides):
    kwargs = dict(
        request_id="req-1", tool="ask_memory", outcome="ok", duration_ms=1400.0, total_ms=1500.0,
        error_code=None, arguments={"query": "secret text", "deep": True},
        spans=[{"name": "recall.pack", "ms": 1200, "count": 1}],
    )
    kwargs.update(overrides)
    server._record_ledger_row(**kwargs)


def test_the_ledger_site_feeds_the_watch_with_the_real_deep_flag(ledger_dir: Path) -> None:
    watch = latency_watch.reset(started_at=T0, clock=_Clock(AFTER_GRACE))
    try:
        _record()
        assert len(_rows(ledger_dir)) == 1
        [sample] = list(watch._ring)
        assert (sample.tool, sample.deep, sample.total_ms) == ("ask_memory", True, 1500.0)
        assert sample.spans == (("recall.pack", 1200.0),)
        assert "secret" not in repr(sample)
    finally:
        latency_watch.reset()


def test_a_failing_watch_leaves_the_ledger_row_untouched(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**_kwargs):
        raise RuntimeError("watch broke")

    monkeypatch.setattr(latency_watch, "observe", boom)
    _record()
    rows = _rows(ledger_dir)
    assert len(rows) == 1 and rows[0]["tool"] == "ask_memory" and rows[0]["total_ms"] == 1500.0
