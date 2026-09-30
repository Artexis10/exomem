"""Freed memory goes back to the OS after reaps and drain batches (bound-cell-memory D2).

glibc keeps freed heap in its arenas; a cell that loaded a model or drained a
batch holds that high-water until something asks for it back. The trim is a
guarded ctypes lookup: off glibc, or when anything about the call fails, it is
a no-op that never raises, because both call sites run on background threads
whose only job is to keep going.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import derived_drain, model_reaper, process_memory, readiness


@pytest.fixture(autouse=True)
def _fresh_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_memory, "_MALLOC_TRIM", process_memory._UNRESOLVED)
    monkeypatch.setattr(process_memory, "_LAST_TRIM", None)
    monkeypatch.setattr(process_memory, "_TRIM_PENDING", False)


def test_trim_is_a_no_op_when_libc_cannot_be_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_args, **_kwargs):
        raise OSError("libc.so.6: cannot open shared object file")

    monkeypatch.setattr(ctypes, "CDLL", missing)
    assert process_memory.trim_allocator() is False


def test_trim_is_a_no_op_when_libc_has_no_malloc_trim(monkeypatch: pytest.MonkeyPatch) -> None:
    """musl and other non-glibc C libraries load but carry no malloc_trim."""
    monkeypatch.setattr(ctypes, "CDLL", lambda *_a, **_k: SimpleNamespace())
    assert process_memory.trim_allocator() is False


def test_trim_is_a_no_op_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_load(*_args, **_kwargs):
        raise AssertionError("no libc lookup off Linux")

    monkeypatch.setattr(process_memory.sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", must_not_load)
    assert process_memory.trim_allocator() is False


def test_trim_never_raises_when_the_native_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def exploding(_pad):
        raise RuntimeError("native failure")

    monkeypatch.setattr(process_memory, "_MALLOC_TRIM", exploding)
    assert process_memory.trim_allocator() is False


def test_trim_calls_malloc_trim_with_zero_padding(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_trim(pad: int) -> int:
        calls.append(pad)
        return 1

    monkeypatch.setattr(process_memory, "_MALLOC_TRIM", fake_trim)
    assert process_memory.trim_allocator() is True
    assert calls == [0]


def test_trim_runs_at_most_once_per_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(process_memory, "_MALLOC_TRIM", lambda pad: calls.append(pad) or 1)
    interval = process_memory.TRIM_INTERVAL_SECONDS
    now = {"t": 1000.0}

    def clock() -> float:
        return now["t"]

    assert process_memory.trim_allocator(clock=clock) is True
    for step in (0.0, 1.0, interval - 0.001):
        now["t"] = 1000.0 + step
        assert process_memory.trim_allocator(clock=clock) is False
    now["t"] = 1000.0 + interval
    assert process_memory.trim_allocator(clock=clock) is True
    now["t"] += interval / 2
    assert process_memory.trim_allocator(clock=clock) is False
    assert calls == [0, 0]


def test_the_reaper_and_the_drain_share_one_allowance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both call sites go through the one throttled helper."""
    calls: list[int] = []
    monkeypatch.setattr(process_memory, "_MALLOC_TRIM", lambda pad: calls.append(pad) or 1)
    monkeypatch.setattr(readiness, "is_warming", lambda: False)
    _fake_claims(monkeypatch, completed=["batch-1"])
    model_reaper._reap_once([_slot("a", released=True)], now=10_000.0, threshold=1.0)
    derived_drain.drain_once(tmp_path, dispatch=None, limit=1, now=1.0)
    assert calls == [0]


def test_a_throttled_release_is_trimmed_by_a_later_idle_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Drain trims at t=0, the reaper releases at t=30 (throttled): an idle tick
    after the interval still trims, exactly once, and the next idle tick does not."""
    calls: list[int] = []
    now = {"t": 0.0}
    monkeypatch.setattr(process_memory, "_MALLOC_TRIM", lambda pad: calls.append(pad) or 1)
    monkeypatch.setattr(process_memory, "_clock", lambda: now["t"])
    monkeypatch.setattr(readiness, "is_warming", lambda: False)
    interval = process_memory.TRIM_INTERVAL_SECONDS

    _fake_claims(monkeypatch, completed=["batch-1"])
    derived_drain.drain_once(tmp_path, dispatch=None, limit=1, now=1.0)
    assert calls == [0]

    now["t"] = 30.0
    assert model_reaper._reap_once([_slot("a", released=True)], now=10_000.0, threshold=1.0)
    assert calls == [0]
    assert process_memory.trim_pending()

    idle = [_slot("b", released=False)]
    now["t"] = 45.0  # still inside the interval: stays pending
    assert model_reaper._reap_once(idle, now=10_000.0, threshold=1.0) == []
    assert calls == [0]
    now["t"] = interval + 1.0
    assert model_reaper._reap_once(idle, now=10_000.0, threshold=1.0) == []
    assert calls == [0, 0]
    assert not process_memory.trim_pending()
    now["t"] = 3 * interval
    model_reaper._reap_once(idle, now=10_000.0, threshold=1.0)
    assert calls == [0, 0]


def test_trim_runs_against_the_real_libc_without_raising() -> None:
    assert process_memory.trim_allocator() in (True, False)


# --------------------------------------------------------------- call sites


@pytest.fixture
def trims(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(process_memory, "trim_allocator", lambda: calls.append(1) or True)
    return calls


def _slot(name: str, *, released: bool) -> model_reaper.ResourceSlot:
    return model_reaper.ResourceSlot(
        name=name,
        is_loaded=lambda: True,
        inflight=lambda: 0,
        last_activity=lambda: 0.0,
        unload=lambda: released,
    )


def test_a_reap_that_releases_something_trims_once(
    monkeypatch: pytest.MonkeyPatch, trims: list[int]
) -> None:
    monkeypatch.setattr(readiness, "is_warming", lambda: False)
    slots = [_slot("a", released=True), _slot("b", released=True)]
    assert model_reaper._reap_once(slots, now=10_000.0, threshold=1.0) == ["a", "b"]
    assert trims == [1]


def test_a_reap_that_releases_nothing_does_not_trim(
    monkeypatch: pytest.MonkeyPatch, trims: list[int]
) -> None:
    monkeypatch.setattr(readiness, "is_warming", lambda: False)
    assert model_reaper._reap_once([_slot("a", released=False)], now=10_000.0, threshold=1.0) == []
    assert trims == []


def _fake_claims(monkeypatch: pytest.MonkeyPatch, *, completed: list[str]) -> None:
    claim = SimpleNamespace(attempt_count=0, next_attempt_at=0.0)
    batches = [(claim,)]
    monkeypatch.setattr(
        derived_drain.derived_receipts, "recover_prepared_batches", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(
        derived_drain.derived_receipts,
        "claim_ready_components",
        lambda *_a, **_k: batches.pop() if batches else (),
    )
    monkeypatch.setattr(derived_drain, "_dispatch_claims", lambda *_a, **_k: list(completed))


def test_a_drain_pass_that_completes_a_batch_trims_once(
    monkeypatch: pytest.MonkeyPatch, trims: list[int], tmp_path: Path
) -> None:
    _fake_claims(monkeypatch, completed=["batch-1"])
    assert derived_drain.drain_once(tmp_path, dispatch=None, limit=1, now=1.0) == 1
    assert trims == [1]


def test_a_drain_pass_that_completes_nothing_does_not_trim(
    monkeypatch: pytest.MonkeyPatch, trims: list[int], tmp_path: Path
) -> None:
    _fake_claims(monkeypatch, completed=[])
    assert derived_drain.drain_once(tmp_path, dispatch=None, limit=1, now=1.0) == 0
    assert trims == []


def test_sensor_worker_children_inherit_the_arena_bound() -> None:
    from exomem import sensor_worker

    env = sensor_worker.child_env({"MALLOC_ARENA_MAX": "2", "PATH": "/usr/bin"})
    assert env["MALLOC_ARENA_MAX"] == "2"


def test_hosted_and_cloud_images_bound_glibc_arenas() -> None:
    """The cloud stage derives from hosted, so it inherits the bound unless it overrides it."""
    text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")

    def stage(header: str) -> str:
        return text.split(header, 1)[1].split("\nFROM ", 1)[0]

    assert "MALLOC_ARENA_MAX=2" in stage("FROM python:3.12-slim AS hosted")
    cloud = stage("FROM hosted AS cloud")
    assert "MALLOC_ARENA_MAX" not in cloud or "MALLOC_ARENA_MAX=2" in cloud
