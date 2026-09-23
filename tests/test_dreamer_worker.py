"""D1-T5: the dreamer worker's lifecycle, budgets, abort and checkpoint.

`run_once` is the test seam: one bounded tick, ignoring the idle gate. The
loop around it (`start`/`stop`) is exercised for its lifecycle only.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from exomem import (
    dreamer,
    dreamer_families,
    dreamer_policy,
    dreamer_store,
    foreground_activity,
    freshness,
)
from exomem import vault as vault_module


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    monkeypatch.delenv("EXOMEM_DREAMER", raising=False)
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()


def _write(vault: Path, rel: str, body: str = "body") -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: insight\nstatus: active\n---\n# Page\n\n{body}\n", encoding="utf-8")
    return path


def _vault(tmp_path: Path, count: int) -> Path:
    vault = tmp_path / "vault"
    for index in range(count):
        _write(vault, f"Knowledge Base/Notes/Insights/page-{index:02d}.md")
    freshness.seed(
        vault,
        "vault",
        [(str(p), freshness.stat_signature(p)) for p in vault_module.walk_vault_md(vault)],
    )
    return vault


class _Recorder:
    """A fake family that records every page it is handed."""

    def __init__(self, hook=None) -> None:
        self.pages: list[str] = []
        self.hook = hook

    def family(self) -> dreamer_families.Family:
        def on_page(ctx, rel):
            self.pages.append(rel)
            if self.hook is not None:
                self.hook(ctx, rel, len(self.pages))

        return dreamer_families.Family(
            name="upkeep_test",
            kinds=("test.kind",),
            on_page=on_page,
            on_delete=lambda ctx, rel: self.pages.append(f"deleted:{rel}"),
            revalidate=lambda ctx, row: None,
        )


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(dreamer_families, "REGISTRY", [rec.family()])
    return rec


def _config(tmp_path: Path, value: str) -> None:
    import os

    Path(os.environ["EXOMEM_CONFIG_PATH"]).write_text(json.dumps({"dreamer": value}), "utf-8")


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == dreamer.THREAD_NAME and t.is_alive()]


def test_start_is_idempotent_and_stop_joins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    vault = tmp_path / "vault"
    vault.mkdir()
    first = dreamer.start(vault)
    assert first is not None and first.name == dreamer.THREAD_NAME
    assert dreamer.start(vault) is first
    assert len(_threads()) == 1
    dreamer.stop(timeout=2.0)
    assert not first.is_alive()
    dreamer.stop(timeout=2.0)
    assert _threads() == []


def test_off_creates_no_thread_and_no_sidecar(tmp_path: Path) -> None:
    vault = _vault(tmp_path, 2)
    assert dreamer.setting() == "off"
    assert dreamer.start(vault) is None
    assert _threads() == []
    assert not dreamer_store.sidecar_path(vault).exists()
    status = dreamer.status(vault)
    assert status["setting"] == "off"
    assert status["state"] == "off"
    assert status["running"] is False
    assert status["sidecar"] is None
    assert not dreamer_store.sidecar_path(vault).exists()


def test_paused_runs_no_tick(tmp_path: Path, monkeypatch, recorder) -> None:
    _config(tmp_path, "paused")
    vault = _vault(tmp_path, 2)
    ticks: list[str] = []
    monkeypatch.setattr(dreamer, "run_once", lambda *a, **k: ticks.append("tick"))
    thread = dreamer.start(vault)
    assert thread is not None
    assert _wait_for(lambda: dreamer._STATE.loops >= 1)
    assert ticks == []
    assert dreamer.status()["state"] == "paused"
    assert not dreamer_store.sidecar_path(vault).exists()
    # Resume is a config change the live loop reads on its next poll.
    _config(tmp_path, "on")
    assert dreamer.setting() == "on"


def test_tick_respects_page_cpu_and_wall_budgets(tmp_path: Path, recorder) -> None:
    vault = _vault(tmp_path, 10)
    by_pages = dreamer.run_once(vault, budget=dreamer.Budget(pages=3, cpu=10.0, wall=10.0))
    assert by_pages.stop_reason == "pages"
    assert len(by_pages.processed) == 3

    cpu = [0.0]

    def thread_time() -> float:
        cpu[0] += 0.07
        return cpu[0]

    by_cpu = dreamer.run_once(
        vault,
        clock=dreamer.Clock(thread_time=thread_time),
        budget=dreamer.Budget(pages=32, cpu=0.2, wall=10.0),
    )
    assert by_cpu.stop_reason == "cpu"
    assert 1 <= len(by_cpu.processed) < 4

    wall = [100.0]

    def monotonic() -> float:
        wall[0] += 0.4
        return wall[0]

    by_wall = dreamer.run_once(
        vault,
        clock=dreamer.Clock(monotonic=monotonic),
        budget=dreamer.Budget(pages=32, cpu=10.0, wall=1.0),
    )
    assert by_wall.stop_reason == "wall"
    assert 1 <= len(by_wall.processed) < 3
    done = by_pages.processed + by_cpu.processed + by_wall.processed
    assert len(set(done)) == len(done)


def test_tick_stops_before_the_next_page_on_foreground_or_generation_move(
    tmp_path: Path, monkeypatch
) -> None:
    vault = _vault(tmp_path, 6)
    entered = threading.Event()
    release = threading.Event()

    holders: list[threading.Thread] = []

    def hold() -> None:
        with foreground_activity.foreground_scope(vault):
            entered.set()
            release.wait(5)

    def on_second_page(ctx, rel, count) -> None:
        if count == 2:
            holder = threading.Thread(target=hold, daemon=True)
            holders.append(holder)
            holder.start()
            assert entered.wait(5)

    rec = _Recorder(on_second_page)
    monkeypatch.setattr(dreamer_families, "REGISTRY", [rec.family()])
    try:
        result = dreamer.run_once(vault)
    finally:
        release.set()
        for holder in holders:
            holder.join(5)
    assert result.stop_reason == "foreground"
    assert len(result.processed) == 2
    assert rec.pages == list(result.processed)

    # A request that came and went between two pages stops the tick too.
    def request_between_pages(ctx, rel, count) -> None:
        if count == 1:
            with foreground_activity.foreground_scope(vault):
                pass

    rec = _Recorder(request_between_pages)
    monkeypatch.setattr(dreamer_families, "REGISTRY", [rec.family()])
    between = dreamer.run_once(vault)
    assert between.stop_reason == "foreground"
    assert len(between.processed) == 1

    # And so does a vault change.
    def change_on_first_page(ctx, rel, count) -> None:
        if count == 1:
            edited = _write(vault, "Knowledge Base/Notes/Insights/page-05.md", "moved")
            freshness.on_files_changed(vault, changed=[edited])

    rec = _Recorder(change_on_first_page)
    monkeypatch.setattr(dreamer_families, "REGISTRY", [rec.family()])
    moved = dreamer.run_once(vault)
    assert moved.stop_reason == "generation"
    assert len(moved.processed) == 1


def test_a_killed_tick_resumes_with_only_the_remainder(tmp_path: Path, monkeypatch) -> None:
    vault = _vault(tmp_path, 7)

    def crash_on_fifth(ctx, rel, count) -> None:
        if count == 5:
            raise RuntimeError("killed mid-page")

    rec = _Recorder(crash_on_fifth)
    monkeypatch.setattr(dreamer_families, "REGISTRY", [rec.family()])
    killed = dreamer.run_once(vault)
    assert killed.stop_reason == "error"
    assert killed.error_code == "RuntimeError"
    assert len(killed.processed) == 4

    rec = _Recorder()
    monkeypatch.setattr(dreamer_families, "REGISTRY", [rec.family()])
    resumed = dreamer.run_once(vault)
    assert resumed.stop_reason == "drained"
    assert list(resumed.processed) == [
        f"Knowledge Base/Notes/Insights/page-{i:02d}.md" for i in range(4, 7)
    ]
    assert set(killed.processed).isdisjoint(resumed.processed)


def test_three_failures_back_off_and_report_failed(tmp_path: Path, monkeypatch) -> None:
    vault = _vault(tmp_path, 3)

    def always(ctx, rel, count) -> None:
        raise RuntimeError("broken family")

    rec = _Recorder(always)
    monkeypatch.setattr(dreamer_families, "REGISTRY", [rec.family()])
    monkeypatch.setenv("EXOMEM_DREAMER", "on")
    for attempt in range(1, 4):
        result = dreamer.run_once(vault)
        assert result.stop_reason == "error"
        assert dreamer.status()["consecutive_failures"] == attempt
    status = dreamer.status(vault)
    assert status["state"] == "failed"
    assert status["failed_since"] is not None
    assert status["sidecar"]["state"] == "failed"
    assert status["sidecar"]["last_error_code"] == "RuntimeError"
    signals = dreamer.gather_signals(vault)
    assert signals.consecutive_failures == 3
    import dataclasses

    ready = dataclasses.replace(
        signals, idle_seconds=3600.0, vault_quiet_seconds=3600.0, graph_debt=False
    )
    assert dreamer_policy.decide(ready).reason == "backoff"

    rec = _Recorder()
    monkeypatch.setattr(dreamer_families, "REGISTRY", [rec.family()])
    healed = dreamer.run_once(vault)
    assert healed.stop_reason == "drained"
    assert dreamer.status()["consecutive_failures"] == 0
    assert dreamer.status()["state"] != "failed"


def test_hourly_cpu_ledger_rolls() -> None:
    ledger = dreamer.CpuLedger(window=3600.0)
    ledger.add(0.0, 50.0)
    ledger.add(1800.0, 20.0)
    assert ledger.used(1800.0) == pytest.approx(70.0)
    assert ledger.used(1800.0) >= dreamer_policy.HOURLY_CPU_SECONDS
    assert ledger.used(3600.0) == pytest.approx(20.0)
    assert ledger.used(5400.0) == pytest.approx(0.0)
